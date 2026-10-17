"""T08 (R08): branch files and memory are isolated; conflicting merges fail."""
import os
from pathlib import Path

import pytest

from conftest import PID, agent, ingest, run, task
from server.projects import ConflictError, InvalidRequest

REPORT = "reports/q3 summary.md"


def _setup(core):
    sid = ingest(core, "ops_ascii.md")
    task(core, "t", [sid], phrases=("Uptime",))
    a = agent(core, env={"export_adapter": "fixture_2"})
    run(core, a["agent_id"], "t")
    main = a["accepted_branch_id"]
    v1 = core.versions.save(PID, main, None, "v1", "u")
    return a, main, v1


def test_fork_isolates_files_and_memory(core):
    a, main, v1 = _setup(core)
    f = core.versions.fork(PID, v1["version_id"], "experiment", "work", "u")
    x = f["branch"]
    main_ws, x_ws = Path(core.versions.branch(main)["workspace_dir"]), Path(x["workspace_dir"])
    assert main_ws != x_ws
    p_main, p_x = main_ws / REPORT, x_ws / REPORT
    assert os.stat(p_x).st_nlink == 1 and (os.stat(p_main).st_ino != os.stat(p_x).st_ino or os.stat(p_main).st_dev != os.stat(p_x).st_dev)
    before = p_main.read_bytes()
    p_x.write_text("branch-only edit\n", encoding="utf-8")
    (x_ws / "notes.md").write_text("new branch file", encoding="utf-8")
    core.versions.edit_memory(x["branch_id"], "audience", "board")
    assert p_main.read_bytes() == before and not (main_ws / "notes.md").exists()
    assert "audience" not in core.versions.branch(main)["state"]["memory_facts"]
    vx = core.versions.save(PID, x["branch_id"], None, "exp", "u")
    d = core.versions.compare(PID, v1["version_id"], vx["version_id"])
    paths = {f["path"]: f["status"] for f in d["changes"]["files"]}
    assert paths == {REPORT: "modified", "notes.md": "added"}
    assert d["changes"]["memory"][0]["key"] == "audience"
    assert any(s["path"] == "memory.audience" for s in d["summary"])
    assert {"file:notes.md", "memory:audience", f"file:{REPORT}"} <= set(d["change_keys"])
    # selected merge of one record
    res = core.merges.merge(PID, main, None, vx["version_id"], ["memory:audience", "file:notes.md"], {}, None, "u")
    assert res["version"]["parents"] == [v1["version_id"], vx["version_id"]]
    assert core.versions.branch(main)["state"]["memory_facts"]["audience"]["value"] == "board"
    assert (main_ws / "notes.md").exists() and p_main.read_bytes() == before  # the unselected report edit stayed out
    assert res["ancestor"] == v1["version_id"]


def test_conflicts_and_guards(core):
    a, main, v1 = _setup(core)
    x = core.versions.fork(PID, v1["version_id"], "exp", "work", "u")["branch"]
    core.versions.edit_memory(main, "tone", "formal")
    vm = core.versions.save(PID, main, None, "main tone", "u")
    core.versions.edit_memory(x["branch_id"], "tone", "casual")
    (Path(x["workspace_dir"]) / REPORT).unlink()
    vx = core.versions.save(PID, x["branch_id"], None, "x", "u")
    (Path(core.versions.branch(main)["workspace_dir"]) / REPORT).write_text("main edit", encoding="utf-8")
    vm = core.versions.save(PID, main, None, "main edit", "u")
    with pytest.raises(ConflictError) as e:
        core.merges.merge(PID, main, None, vx["version_id"], ["memory:tone"], {}, None, "u")
    assert e.value.code == "MERGE_CONFLICT" and e.value.detail["conflicts"][0]["kind"] == "both_modified"
    with pytest.raises(ConflictError) as e:
        core.merges.merge(PID, main, None, vx["version_id"], [f"file:{REPORT}"], {}, None, "u")
    assert e.value.detail["conflicts"][0]["kind"] == "delete_vs_modify"
    assert core.versions.branch(main)["head_version"] == vm["version_id"]  # head unchanged after failed merges
    with pytest.raises(ConflictError) as e:
        core.merges.merge(PID, main, v1["version_id"], vx["version_id"], ["memory:tone"], {}, None, "u")
    assert e.value.code == "STALE_HEAD"
    for bad in ("grant:g1", "effects", "policy", "credential:x"):
        with pytest.raises(InvalidRequest):
            core.merges.merge(PID, main, None, vx["version_id"], [bad], {}, None, "u")
    ok = core.merges.merge(PID, main, None, vx["version_id"], ["memory:tone"], {"memory:tone": "candidate"}, None, "u")
    assert core.versions.branch(main)["state"]["memory_facts"]["tone"]["value"] == "casual"
    assert ok["applied"][0]["result"] == "resolved_candidate"


def test_unsaved_target_and_failed_checks_keep_head(core):
    a, main, v1 = _setup(core)
    x = core.versions.fork(PID, v1["version_id"], "exp", "work", "u")["branch"]
    core.versions.edit_memory(x["branch_id"], "preferred_report_encoding", "utf-16", seeded=True)
    vx = core.versions.save(PID, x["branch_id"], None, "bad memory", "u")
    core.versions.edit_memory(main, "dirty", True)
    with pytest.raises(ConflictError) as e:
        core.merges.merge(PID, main, None, vx["version_id"], ["memory:preferred_report_encoding"], {}, None, "u")
    assert e.value.code == "UNSAVED_CHANGES"
    vm = core.versions.save(PID, main, None, "clean", "u")
    with pytest.raises(ConflictError) as e:
        core.merges.merge(PID, main, None, vx["version_id"], ["memory:preferred_report_encoding"], {}, None, "u")
    assert e.value.code == "MERGE_CHECKS_FAILED"
    assert core.versions.branch(main)["head_version"] == vm["version_id"]


def test_branches_have_separate_threads_and_runs(core):
    a, main, v1 = _setup(core)
    x = core.versions.fork(PID, v1["version_id"], "exp", "work", "u")["branch"]
    r = run(core, a["agent_id"], "t", branch_id=x["branch_id"])
    main_run = core.db.one("SELECT thread_id FROM runs WHERE branch_id=?", (main,))
    assert r["thread_id"].split(":")[0] != main_run["thread_id"].split(":")[0]
