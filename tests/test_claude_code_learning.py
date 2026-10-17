"""Learning from Claude Code's failed commands: watch, learn after a repeat, prevent, retire."""
import json

import pytest

from conftest import PID
from server.cc_learning import classify


@pytest.fixture
def cc(core, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    n = {"i": 0}

    def hook(name, **kw):
        return core.claude_code.handle(PID, {"hook_event_name": name, "session_id": kw.pop("sid", "s1"), "cwd": str(proj),
                                             "antibody_project_dir": str(proj), **kw})

    def bash(command, error=None, tool="Bash", sid="s1"):
        n["i"] += 1
        tid = f"t{n['i']}"
        pre = hook("PreToolUse", tool_name=tool, tool_input={"command": command}, tool_use_id=tid, sid=sid)
        if pre["stdout"] and json.loads(pre["stdout"]).get("hookSpecificOutput", {}).get("permissionDecision") in ("deny", "ask"):
            return "blocked", json.loads(pre["stdout"])
        if error is None:
            post = hook("PostToolUse", tool_name=tool, tool_input={"command": command}, tool_use_id=tid, sid=sid,
                        tool_response={"stdout": "ok", "stderr": "", "interrupted": False})
        else:
            post = hook("PostToolUseFailure", tool_name=tool, tool_input={"command": command}, tool_use_id=tid, sid=sid,
                        error=f"Exit code 1\n{error}", is_interrupt=False)
        return "ran", (json.loads(post["stdout"]) if post["stdout"] else None)
    hook("SessionStart")
    return hook, bash


NO_PYTEST = r"C:\Python312\python.exe: No module named pytest"


def test_repeated_failure_becomes_a_lesson_that_prevents_the_next_attempt(core, cc):
    hook, bash = cc
    assert bash("python greet.py")[0] == "ran"                       # a success, part of the history the lesson is checked against
    state, post = bash("python -m pytest -q", NO_PYTEST)
    assert "'pytest' is not installed here" in post["systemMessage"]  # first time: a hint, no lesson yet
    assert not core.learning.candidates(PID)
    state, post = bash("cd proj && python -m pytest tests/ -x", NO_PYTEST, sid="s2")   # another session, same mistake
    ctx = post["hookSpecificOutput"]["additionalContext"]
    assert "failed 2 times the same way" in ctx and "learned it" in ctx
    inc = [i for i in core.incidents.list(PID) if i["category"] == "repeated_failure"][0]
    cand = core.learning.candidate(inc["candidate_id"])
    assert cand["status"] == "Accepted" and cand["body"]["family"] == "module_not_found"
    ev = core.db.one("SELECT * FROM evaluations WHERE candidate_id=?", (cand["candidate_id"],))
    assert ev["status"] == "passed" and ev["suite_id"] == "host_history"

    # prevent: the same mistake is stopped before it runs, with the fix; other commands are untouched
    state, out = bash("python -m pytest -q test_greet.py", sid="s3")
    assert state == "blocked" and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "python -m unittest" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "already failed here" in out["systemMessage"]
    assert bash("python -m pip install pytest", sid="s3")[0] == "ran"
    assert bash("python -m unittest -q", sid="s3")[0] == "ran"
    assert core.db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE use_kind='prevented_failure'") == 1
    assert core.db.scalar("SELECT COUNT(*) FROM incidents WHERE category='blocked_action'") == 0  # a saved attempt is not an incident
    # new sessions are told up front
    notice = hook("SessionStart", sid="s4")["stdout"]
    assert "Learned on this machine" in notice and "pytest" in notice

    # retire: once installed, a marked retry goes through and its success retires the lesson
    state, post = bash("python -m pytest -q  # antibody:retry", sid="s4")
    assert state == "ran" and "retired" in post["systemMessage"]
    assert bash("python -m pytest -q", sid="s4")[0] == "ran"


def test_powershell_command_not_found_suggests_the_powershell_way(core, cc):
    hook, bash = cc
    err = "head : The term 'head' is not recognized as the name of a cmdlet, function, script file, or operable program."
    bash("head -n 5 README.md", err, tool="PowerShell")
    bash("cat log.txt | head -20", err, tool="PowerShell")
    state, out = bash("head README.md", tool="PowerShell")
    assert state == "blocked" and "Get-Content <file> -TotalCount N" in out["hookSpecificOutput"]["permissionDecisionReason"]
    # the lesson is per tool: Git Bash has head
    assert bash("head README.md", tool="Bash")[0] == "ran"


def test_lesson_that_would_block_past_successes_is_not_accepted(core, cc):
    hook, bash = cc
    bash("python -m pytest -q")                                       # this worked once (different environment earlier)
    bash("python -m pytest -q", NO_PYTEST)
    state, post = bash("python -m pytest -q", NO_PYTEST)
    assert "only warns" in post["hookSpecificOutput"]["additionalContext"]
    cand = core.learning.candidates(PID)[0]
    assert cand["status"] == "Rejected"
    assert bash("python -m pytest -q")[0] == "ran"                    # nothing is stopped on an unconfirmed lesson


def test_success_that_printed_the_same_error_does_not_block_learning(core, cc):
    """`python -m pytest | tail -3` exits 0 even when pytest is missing; its output shows the error, so it is no counter-evidence."""
    hook, bash = cc
    n = {"i": 100}

    def ran(command, output):
        n["i"] += 1
        hook("PreToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id=f"m{n['i']}")
        hook("PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id=f"m{n['i']}",
             tool_response={"stdout": output, "stderr": "", "interrupted": False})
    ran("python -m pytest -q 2>&1 | tail -3", NO_PYTEST)
    bash("python -m pytest -q", NO_PYTEST)
    state, post = bash("python -m pytest -q", NO_PYTEST)
    assert "learned it" in post["hookSpecificOutput"]["additionalContext"]
    assert post["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    ev = core.db.one("SELECT summary_json FROM evaluations WHERE suite_id='host_history'")
    assert json.loads(ev["summary_json"])["inconclusive"] == 1


def test_manual_acceptance_when_auto_accept_is_off(core, cc):
    hook, bash = cc
    core.projects.update_settings(PID, {"claude_code_auto_accept": False}, "test")
    s = core.projects.get_project(PID)["settings"]
    if s.get("claude_code_auto_accept", True):
        pytest.skip("project settings API not available")
    bash("npm test", "Error: Cannot find module 'jest'")
    state, post = bash("npm test", "Error: Cannot find module 'jest'")
    assert "accept it in Knowledge" in post["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("tool,cmd,err,family,key", [
    ("Bash", "python -m pytest", "Exit code 1\n/usr/bin/python: No module named pytest", "module_not_found", "pytest"),
    ("Bash", "jq .a f.json", "bash: jq: command not found", "command_not_found", "jq"),
    ("PowerShell", "Get-ChildItem -Recurse -Depth 2 -Filter x", "Get-ChildItem : A parameter cannot be found that matches parameter name 'Depth'.",
     "unknown_parameter", "Get-ChildItem -Depth"),
    ("PowerShell", "cd a && dir", "The token '&&' is not a valid statement separator in this version.", "shell_syntax", "&&"),
    ("Bash", "node x.js", "Error: Cannot find module 'express'", "module_not_found", "express"),
])
def test_classify(tool, cmd, err, family, key):
    c = classify(tool, cmd, err)
    assert (c["family"], c["key"]) == (family, key)


def test_lessons_never_stop_commands_that_only_inspect(core, cc):
    hook, bash = cc
    bash("python -m pytest -q", NO_PYTEST)
    bash("python -m pytest -q", NO_PYTEST, sid="s2")
    assert bash("python -m pytest -q", sid="s3")[0] == "blocked"
    assert bash("python -m pip show pytest 2>&1 | head -3", sid="s3")[0] == "ran"   # checking whether it's installed now
    assert bash("which python", sid="s3")[0] == "ran"
    assert bash("pip show pytest; python -m pytest -q", sid="s3")[0] == "blocked"   # still contains the failing step


def test_compound_inspection_is_allowed_but_a_hidden_repeat_is_not(core, cc):
    hook, bash = cc
    bash("python -m pytest -q", NO_PYTEST)
    bash("python -m pytest -q", NO_PYTEST, sid="s2")
    assert bash('ls -d .venv venv 2>/dev/null; python -m pip show pytest 2>&1 | head -3; which python', sid="s3")[0] == "ran"
    assert bash('cd app && ls && python -m pytest -q 2>&1 | tail -3', sid="s3")[0] == "blocked"


def test_accepting_a_lesson_adds_no_version_to_the_claude_code_history(core, cc):
    hook, bash = cc
    agent_id = core.db.scalar("SELECT agent_id FROM host_sessions LIMIT 1")
    before = core.db.scalar("SELECT COUNT(*) FROM versions v JOIN branches b ON b.branch_id=v.branch_id WHERE b.agent_id=?", (agent_id,))
    bash("python -m pytest -q", NO_PYTEST)
    bash("python -m pytest -q", NO_PYTEST, sid="s2")
    assert any(c["status"] == "Accepted" for c in core.learning.candidates(PID))
    after = core.db.scalar("SELECT COUNT(*) FROM versions v JOIN branches b ON b.branch_id=v.branch_id WHERE b.agent_id=?", (agent_id,))
    assert after == before
