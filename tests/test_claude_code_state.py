"""Exact Claude Code agent states: per-prompt checkpoints, rewind/restore into a resumable
session, screening on restore, and the negotiated merge between two agent branches."""
import json
import re
import uuid

import pytest

from conftest import FIX, PID, agent, ingest, run, task
from server import cc_state as cc


class Transcript:
    """Writes entries the way Claude Code does: a jsonl tree linked by uuid/parentUuid."""

    def __init__(self, path, sid="sess-A"):
        self.path, self.sid, self.leaf = path, sid, None
        path.write_text("", encoding="utf-8")

    def add(self, typ, content, **extra):
        u = str(uuid.uuid4())
        e = {"type": typ, "uuid": u, "parentUuid": self.leaf, "sessionId": self.sid, "isSidechain": False, "cwd": "x",
             "timestamp": "2026-10-17T00:00:00Z", "message": {"role": typ, "content": content}, **extra}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(e) + "\n")
        self.leaf = u
        return u

    def turn(self, prompt, reply, tool=None, result=None):
        self.add("user", prompt)
        if tool:
            tid = "toolu_" + uuid.uuid4().hex[:8]
            self.add("assistant", [{"type": "text", "text": "Working."}, {"type": "tool_use", "id": tid, "name": tool, "input": {}}])
            self.add("user", [{"type": "tool_result", "tool_use_id": tid, "content": result or "ok"}], toolUseResult={"stdout": result or "ok"})
        self.add("assistant", [{"type": "thinking", "thinking": "hmm", "signature": "sig"}, {"type": "text", "text": reply}])


@pytest.fixture
def setup(core, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home"))
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "greet.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    tr = Transcript(tmp_path / "sess-A.jsonl")
    cur = {"tr": tr}

    def hook(name, **kw):
        t = cur["tr"]
        ev = {"hook_event_name": name, "session_id": t.sid, "cwd": str(proj), "antibody_project_dir": str(proj),
              "transcript_path": str(t.path), **kw}
        return core.claude_code.handle(PID, ev)
    prompt_holder = cur

    def prompt(text):
        return hook("UserPromptSubmit", prompt=text)

    def ab(line):
        out = json.loads(prompt(f"ab: {line}")["stdout"])
        assert out["decision"] == "block"
        return out["reason"]
    hook("SessionStart")
    core._test_use_transcript = lambda t: (cur.__setitem__("tr", t), hook("SessionStart"))
    return proj, tr, prompt, ab


def sessions(tmp_path):
    return sorted((tmp_path / "claude_home" / "projects").rglob("*.jsonl"))


def test_checkpoint_every_prompt_and_rewind_to_exact_state(setup, tmp_path, core):
    proj, tr, prompt, ab = setup
    prompt("make greet friendlier")                     # checkpoint 1: original files, empty conversation
    tr.turn("make greet friendlier", "Done: now says hello.", tool="Edit")
    (proj / "greet.py").write_text("def greet():\n    return 'hello!'\n", encoding="utf-8")
    prompt("add a farewell function")                   # checkpoint 2: hello version + first turn
    tr.turn("add a farewell function", "Added farewell.", tool="Write")
    (proj / "farewell.py").write_text("def bye():\n    return 'bye'\n", encoding="utf-8")
    log = ab("log")
    assert "auto: add a farewell function" in log and "auto: make greet friendlier" in log and "~0" in log

    out = ab("undo")                                     # back to just before "add a farewell function"
    assert "Rewound 1 prompt" in out and "claude --resume" in out
    assert (proj / "greet.py").read_text(encoding="utf-8") == "def greet():\n    return 'hello!'\n"
    assert not (proj / "farewell.py").exists()
    sid = re.search(r"claude --resume ([0-9a-f-]{36})", out).group(1)
    path = [p for p in sessions(tmp_path) if p.stem == sid][0]
    entries = cc.parse_lines(path.read_bytes())
    conv = [e for e in entries if e.get("type") in ("user", "assistant")]
    # exactly the conversation as it was at that checkpoint: first turn only, linked as one chain under the new id
    texts = [cc.message_text(e) for e in conv]
    assert texts[0] == "make greet friendlier" and texts[-1] == "Done: now says hello." and "Added farewell." not in texts
    assert all(e["sessionId"] == sid for e in entries)
    assert [e["parentUuid"] for e in conv[1:]] == [e["uuid"] for e in conv[:-1]] and conv[0]["parentUuid"] is None
    assert conv[-1]["message"]["content"][0]["type"] == "thinking"  # content kept verbatim (signatures included)
    # the newer work is still there on the original branch
    tree = ab("tree")
    assert "main" in tree and "restore-" in tree
    ab("switch main")
    assert (proj / "farewell.py").exists()


def test_rewind_keeps_unsaved_work_by_checkpointing_first(setup, core):
    proj, tr, prompt, ab = setup
    prompt("first")
    tr.turn("first", "ok")
    (proj / "greet.py").write_text("changed by hand\n", encoding="utf-8")
    ab("undo")
    ab("switch main")
    assert "auto: before undo" in ab("log")
    assert (proj / "greet.py").read_text(encoding="utf-8") == "changed by hand\n"


def test_chain_follows_live_branch_after_claude_code_rewind(tmp_path):
    tr = Transcript(tmp_path / "t.jsonl")
    tr.turn("q1", "a1")
    keep = tr.leaf
    tr.turn("abandoned question", "abandoned answer")
    tr.leaf = keep                                      # Claude Code's own rewind: new entries hang off an earlier point
    tr.turn("q2", "a2")
    texts = [cc.message_text(e) for e in cc.chain(cc.parse_lines(tr.path.read_bytes()))]
    assert texts == ["q1", "a1", "q2", "a2"]


def test_restore_screens_quarantined_instructions(core, setup, tmp_path):
    proj, tr, prompt, ab = setup
    # learn a threat pattern the normal way (blocked attack -> candidate -> evaluated -> accepted)
    sid = ingest(core, "vendor_update.html", "web")
    task(core, "t_vendor", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, "Victim", naive=True, env={"export_adapter": "fixture_2"})
    run(core, a["agent_id"], "t_vendor")
    inc = [i for i in core.incidents.list(PID) if i["category"] == "blocked_action"][0]
    core.learning.evaluate(PID, inc["candidate_id"], None, "u")
    core.learning.promote(PID, inc["candidate_id"], None, None, None, "assisted", "u")
    page = (FIX / "vendor_update.html").read_text(encoding="utf-8")
    prompt("summarise the vendor page")
    tr.turn("summarise the vendor page", "Summary written.", tool="WebFetch", result=page)
    ab('save "after reading page"')
    out = ab('restore "after reading page"')
    assert "removed" in out and "quarantined" in out
    sid_new = re.search(r"claude --resume ([0-9a-f-]{36})", out).group(1)
    text = [p for p in sessions(tmp_path) if p.stem == sid_new][0].read_text(encoding="utf-8")
    assert "audit-review@exfil.example" not in text and "removed by ANTIBODY" in text
    assert "Zürich" in text  # benign content survives


def test_api_view_of_session_is_valid_turn_structure(tmp_path):
    tr = Transcript(tmp_path / "t.jsonl")
    tr.turn("q1", "a1", tool="Bash", result="files")
    tr.add("user", "q2")
    tr.add("assistant", [{"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {"file_path": "a"}}])  # interrupted: no result
    msgs, tools = cc.to_api_messages(cc.chain(cc.parse_lines(tr.path.read_bytes())))
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert tools == ["Bash", "Read"]
    assert msgs[-1]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_x", "content": "(no result recorded)"}
    assert not any(b["type"] == "thinking" for m in msgs for b in m["content"])


class ScriptedAgents:
    """Stands in for the Claude Code CLI / API: each side answers from a script; records what it was shown."""

    def __init__(self, ours, theirs, arbiter):
        self.scripts = {"ours": ours, "theirs": theirs, "arbiter": arbiter}
        self.calls = []
        self.used = "scripted"

    def start(self, ch, cwd, title):
        side = "arbiter" if title == "arbiter" else ("ours" if "main side" in title else "theirs")
        return {"side": side, "chain": ch, "rounds": 0}

    def ask(self, handle, prompt, schema, system):
        handle["rounds"] += 1
        units = re.findall(r"\[(u\d+)\] (\S+): (CONFLICT|change made by YOU|change made by YOUR COWORKER|change made by agent '[^']+')", prompt)
        self.calls.append({"side": handle["side"], "round": handle["rounds"], "units": [u[0] for u in units],
                           "context": [cc.message_text(e) for e in (handle["chain"] or [])], "prompt": prompt})
        return self.scripts[handle["side"]](units, handle["rounds"], prompt)


def test_negotiated_merge_uses_each_agents_own_memory(setup, core, tmp_path):
    proj, tr, prompt, ab = setup
    prompt("start")
    ab('save "common start"')
    # branch "feat" (theirs): the user told this agent greet must stay ASCII-only; it added tests
    ab('branch feat')
    tr.turn("greet must stay ASCII for the legacy terminal; add tests", "Kept ASCII, added test_greet.py.", tool="Write")
    (proj / "greet.py").write_text("def greet():\n    return 'Hello'\n", encoding="utf-8")
    (proj / "test_greet.py").write_text("assert greet() == 'Hello'\n", encoding="utf-8")
    ab('save "feat work"')
    ab("switch main")
    # main (ours) continues in its own Claude Code session
    tr = Transcript(tmp_path / "sess-B.jsonl", sid="sess-B")
    core._test_use_transcript(tr)
    tr.turn("make greet fun with an emoji", "Added an emoji and a README.", tool="Edit")
    (proj / "greet.py").write_text("def greet():\n    return 'Hi 👋'\n", encoding="utf-8")
    (proj / "README.md").write_text("Greeter\n", encoding="utf-8")
    ab('save "main work"')

    def ours(units, rnd, prompt):
        votes = []
        for uid, path, kind in units:
            if kind == "CONFLICT":
                votes.append({"unit": uid, "decision": "mine" if rnd == 1 else "mine", "reason": "user asked me for an emoji"})
            else:
                votes.append({"unit": uid, "decision": "keep", "reason": "fine"})
        return {"votes": votes, "handoff": {"summary": "emoji greeting + README", "facts": ["user wants it fun"]}}

    def theirs(units, rnd, prompt):
        votes = []
        for uid, path, kind in units:
            if kind == "CONFLICT":
                votes.append({"unit": uid, "decision": "mine", "reason": "user said ASCII only for the legacy terminal"})
            elif path == "README.md":
                votes.append({"unit": uid, "decision": "keep", "reason": "docs are good"})
            else:
                votes.append({"unit": uid, "decision": "keep", "reason": "my tests"})
        return {"votes": votes, "handoff": {"summary": "ASCII constraint, tests", "facts": ["legacy terminal cannot show emoji"]}}

    def arbiter(units, rnd, prompt):
        assert "legacy terminal cannot show emoji" in prompt  # sees both handoffs
        return {"rulings": [{"unit": u[0], "decision": "custom", "custom_text": "    return 'Hi :)'\n",
                             "reason": "fun but ASCII-safe"} for u in units]}

    agents = ScriptedAgents(ours, theirs, arbiter)
    core.claude_code.runner_factory = lambda: agents
    out = ab("merge feat")
    # each side voted with its own exact conversation, not a summary
    first = {c["side"]: c for c in agents.calls if c["round"] == 1}
    assert "make greet fun with an emoji" in first["ours"]["context"] and "greet must stay ASCII" not in " ".join(first["ours"]["context"])
    assert any("greet must stay ASCII" in t for t in first["theirs"]["context"])
    assert {c["side"] for c in agents.calls} == {"ours", "theirs", "arbiter"}
    assert any(c["round"] == 2 for c in agents.calls if c["side"] == "ours")  # disputed unit went back for a second round
    assert (proj / "greet.py").read_text(encoding="utf-8") == "def greet():\n    return 'Hi :)'\n"
    assert (proj / "README.md").exists() and (proj / "test_greet.py").exists()
    assert "arbiter" in out and "agreed (round 1)" in out
    # merge version has both parents; merged agent state = our conversation + merge record with their handoff
    vid = re.search(r"as ([0-9a-f]{10})", out).group(1)
    v = core.db.one("SELECT * FROM versions WHERE version_id LIKE ?", (f"sha256:{vid}%",))
    assert len(json.loads(v["parents_json"])) == 2
    sid_new = re.search(r"claude --resume ([0-9a-f-]{36})", out).group(1)
    merged = [cc.message_text(e) for e in cc.parse_lines([p for p in sessions(tmp_path) if p.stem == sid_new][0].read_bytes())
              if e.get("type") in ("user", "assistant")]
    assert "make greet fun with an emoji" in merged and any("legacy terminal cannot show emoji" in t for t in merged)
    assert merged[-1].startswith("Understood.")
    aid = core.db.scalar("SELECT agent_id FROM host_sessions LIMIT 1")
    assert "merge.negotiated" in [e["kind"] for e in core.journal.events(f"agent:{aid}")]


def test_merge_without_any_backend_explains_how_to_enable(setup, core, monkeypatch):
    proj, tr, prompt, ab = setup
    ab('save "s"')
    ab("branch other")
    (proj / "x.txt").write_text("x\n", encoding="utf-8")
    ab('save "x"')
    ab("switch main")
    monkeypatch.setattr(cc, "find_claude_bin", lambda: None)
    tr.turn("hi", "hello")
    out = ab("merge other")
    assert "sign in to the Claude Code CLI" in out or "Connect Claude" in out
