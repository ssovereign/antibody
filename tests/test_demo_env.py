"""Fresh, identical demo environments: nothing learned yet, the story can be shown from the start, and --reset rebuilds."""
import json
import sys

import pytest

from server import demo_env


def test_demo_env_starts_unlearned_and_resets(make_core, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_home"))
    core = make_core()
    res = demo_env.create(core, "stage", str(tmp_path / "stage"), False, sys.executable)
    pid = res["project_id"]
    folder = tmp_path / "stage"
    assert pid == "demo_stage"
    assert {p.name for p in folder.iterdir()} >= {"greet.py", "test_greet.py", "README.md", "inbox", ".claude"}
    assert "claude_hook.py" in (folder / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    assert not core.registry.active(pid)                                 # nothing learned yet
    agents = {a["name"]: a for a in core.coordinator.agents(pid)}
    assert {"Report assistant A", "Report assistant B", "Export assistant C", "Claude Code: stage"} <= set(agents)

    # the live story works from scratch: the first vendor run is blocked and produces a lesson to evaluate
    run = core.coordinator.start_run(pid, agents["Report assistant A"]["agent_id"], "task_vendor_report", wait=True)
    assert core.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=? AND outcome='DENY'", (run["run_id"],)) == 1
    inc = [i for i in core.incidents.list(pid) if i["category"] == "blocked_action"][0]
    assert inc["candidate_id"]
    # the failure-learning story: the export agent's report fails twice the same way and a lesson is drafted
    core.coordinator.start_run(pid, agents["Export assistant C"]["agent_id"], "task_q3_report", wait=True)
    assert any(i["category"] == "execution_failure" and i["candidate_id"] for i in core.incidents.list(pid))

    with pytest.raises(ValueError):
        demo_env.create(core, "stage", str(folder), False, sys.executable)    # refuses to overwrite without --reset
    (folder / "greet.py").write_text("changed\n", encoding="utf-8")
    demo_env.create(core, "stage", str(folder), True, sys.executable)
    assert (folder / "greet.py").read_text(encoding="utf-8").startswith("def greet")
    assert not core.incidents.list(pid) and not core.registry.active(pid)

    # a second identical environment is independent
    other = demo_env.create(core, "rehearsal", str(tmp_path / "rehearsal"), False, sys.executable)
    assert other["project_id"] == "demo_rehearsal"
    token = json.loads(open(core.config.SECRETS_PATH, encoding="utf-8").read())["claude_code_hook_tokens"]["demo_stage"]
    assert core.projects.authenticate(token)["project_id"] == "demo_stage"   # rebuilt project got a working hook token
