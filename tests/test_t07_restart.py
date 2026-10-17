"""T07 (R07): a service restart reconstructs the reference agent and supported workspace."""
import pytest

from conftest import PID, agent, ingest, ingest_text, task
from server.canonical import utcnow


def test_restart_resumes_from_checkpoint_without_duplicate_effects(make_core):
    c1 = make_core()
    c1.projects.create_project(PID, "x", "o")
    sids = [ingest_text(c1, f"Section {i}: plain status text.", f"s{i}.md") for i in range(3)]
    task(c1, "t_restart", sids, phrases=("Section 2",))
    a = agent(c1, env={"export_adapter": "fixture_2"})
    c1.agent.crash_after = "act_before_verify"
    with pytest.raises(RuntimeError):
        c1.coordinator.start_run(PID, a["agent_id"], "t_restart", wait=True)
    run_id = c1.db.scalar("SELECT run_id FROM runs")
    first_op = c1.db.scalar("SELECT operation_id FROM effects WHERE run_id=?", (run_id,))
    c1.close()

    c2 = make_core()  # same paths: a restarted service
    assert c2.recovery_report["runs_paused"] == 1
    r = c2.coordinator.run(run_id)
    assert (r["status"], r["status_reason"]) == ("paused", "SERVICE_RESTART")
    st = c2.agent.get_state(r["thread_id"])
    assert st["pending"]["tool_id"] == "read_source" and st["operation_id"] == first_op
    c2.coordinator.resume(run_id, "user", wait=True)
    r = c2.coordinator.run(run_id)
    assert (r["status"], r["status_reason"]) == ("completed", "TASK_CHECK_PASSED")
    assert c2.db.scalar("SELECT COUNT(*) FROM executor_invocations WHERE operation_id=?", (first_op,)) == 1
    seqs = [e["sequence"] for e in c2.journal.events(run_id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert any(e["reason_code"] == "DEDUPLICATED" for e in [ev["payload"] for ev in c2.journal.events(run_id) if ev["kind"] == "action.decided"])


def test_restart_marks_inflight_effects(make_core):
    c1 = make_core()
    c1.projects.create_project(PID, "x", "o")
    sid = ingest(c1, "ops_ascii.md")
    task(c1, "t", [sid])
    a = agent(c1, env={"export_adapter": "fixture_2"})
    c1.coordinator.start_run(PID, a["agent_id"], "t", wait=True)
    ops = [r["operation_id"] for r in c1.db.all("SELECT operation_id FROM effects ORDER BY created_at")]
    # simulate a crash during dispatch: one effect had its executor invoked, one never did
    c1.db.execute("UPDATE effects SET state='DISPATCHING' WHERE operation_id IN (?,?)", (ops[0], ops[1]))
    c1.db.execute("DELETE FROM executor_invocations WHERE operation_id=?", (ops[1],))
    c1.close()
    c2 = make_core()
    states = {r["operation_id"]: r["state"] for r in c2.db.all("SELECT operation_id, state FROM effects")}
    assert states[ops[0]] == "UNKNOWN" and states[ops[1]] == "RESERVED"
    assert c2.recovery_report["effects_unknown"] == 1


def test_saved_version_survives_restart(make_core):
    c1 = make_core()
    c1.projects.create_project(PID, "x", "o")
    sid = ingest(c1, "ops_ascii.md")
    task(c1, "t", [sid])
    a = agent(c1, env={"export_adapter": "fixture_2"})
    c1.coordinator.start_run(PID, a["agent_id"], "t", wait=True)
    c1.versions.edit_memory(a["accepted_branch_id"], "team", "ops")
    v = c1.versions.save(PID, a["accepted_branch_id"], None, "after run", "u")
    c1.close()
    c2 = make_core()
    f = c2.versions.fork(PID, v["version_id"], "restored", "restore", "u")
    b = f["branch"]
    assert b["state"]["memory_facts"]["team"]["value"] == "ops"
    from pathlib import Path
    assert (Path(b["workspace_dir"]) / "reports" / "q3 summary.md").read_text(encoding="utf-8").startswith("# Goal for t")
    r = c2.coordinator.start_run(PID, a["agent_id"], "t", branch_id=b["branch_id"], wait=True)
    assert c2.coordinator.run(r["run_id"])["status"] == "completed"


def test_crash_point_objects_without_manifest_and_failed_head_cas(core):
    sid = ingest(core, "ops_ascii.md")
    a = agent(core)
    b = a["accepted_branch_id"]
    head = core.versions.branch(b)["head_version"]
    core.versions.edit_memory(b, "k", "v")
    core.versions.fail_head_cas = True
    with pytest.raises(RuntimeError):
        core.versions.save(PID, b, None, "x", "u")
    core.versions.fail_head_cas = False
    assert core.versions.branch(b)["head_version"] == head  # prior head kept; partial version not exposed
    assert core.versions.branch(b)["barrier"] == 0
    assert core.db.scalar("SELECT COUNT(*) FROM versions WHERE branch_id=?", (b,)) == 1
    core.objects.crash_after_tmp = True
    with pytest.raises(RuntimeError):
        core.objects.put(PID, b"partial bytes")
    core.objects.crash_after_tmp = False
    assert core.objects.cleanup_tmp(PID, 0) >= 1
    core.config.OBJECT_GC_GRACE_SECONDS = 0
    report = core.versions.gc(PID, dry_run=False)
    assert report["reachable_versions"] >= 1
    v = core.versions.save(PID, b, None, "ok", "u")
    assert core.versions.version(v["version_id"])  # objects of reachable versions survive GC
