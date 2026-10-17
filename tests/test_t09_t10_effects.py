"""T09 (R09): a restored run cannot duplicate the same completed operation.
T10 (R09): unknown effects pause until reconciliation."""
import pytest

from conftest import PID, agent, ingest, run, task
from server.projects import ConflictError

pytestmark = pytest.mark.security


def _send_task(core):
    sid = ingest(core, "ops_ascii.md")
    task(core, "t_send", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",), phrases=("Uptime",))
    return agent(core, env={"export_adapter": "fixture_2"})


def test_restore_after_completed_send_does_not_send_twice(core):
    a = _send_task(core)
    core.agent.crash_after = "act_before_verify:send_report"
    with pytest.raises(RuntimeError):
        core.coordinator.start_run(PID, a["agent_id"], "t_send", wait=True)
    run_id = core.db.scalar("SELECT run_id FROM runs WHERE purpose='task'")
    core.coordinator.set_status(run_id, "paused", "SIMULATED_CRASH")
    assert core.executors.outbox.count(PID) == 1
    send_op = core.db.scalar("SELECT operation_id FROM effects WHERE tool_id='send_report'")
    v = core.versions.save(PID, a["accepted_branch_id"], None, "mid-send", "u")
    cursor = core.objects.get_json(PID, v["manifest"]["adapter_state_ref"])
    assert cursor["state"]["operation_id"] == send_op and cursor["state"]["pending"]["tool_id"] == "send_report"
    # restore into a new branch and resume from the saved cursor
    f = core.versions.fork(PID, v["version_id"], "restore", "restore", "u")
    assert "Completed external actions remain recorded" in f["message"]
    r = core.coordinator.start_run(PID, a["agent_id"], "t_send", branch_id=f["branch"]["branch_id"], wait=True)
    r = core.coordinator.run(r["run_id"])
    assert (r["status"], r["status_reason"]) == ("completed", "TASK_CHECK_PASSED")  # verified artifact record carried with identical bytes
    assert core.executors.outbox.count(PID) == 1
    assert core.db.scalar("SELECT COUNT(*) FROM executor_invocations WHERE operation_id=?", (send_op,)) == 1
    dedup = [e for e in core.journal.events(r["run_id"]) if e["kind"] == "action.decided" and e["payload"]["reason_code"] == "DEDUPLICATED"]
    assert dedup and dedup[0]["payload"]["operation_id"] == send_op
    # the permanent journal is never rewound by restore
    assert core.db.scalar("SELECT COUNT(*) FROM effect_journal WHERE operation_id=?", (send_op,)) >= 3
    # a fresh run of the same task sees the completed send in verified progress and does not resend
    r2 = run(core, a["agent_id"], "t_send", branch_id=core.versions.create_branch(PID, a["agent_id"], "fresh", core.versions.branch(a["accepted_branch_id"])["state"])["branch_id"])
    assert core.executors.outbox.count(PID) == 1
    assert not core.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=? AND tool_id='send_report'", (r2["run_id"],))


def test_unknown_effect_pauses_until_reconciled(core):
    a = _send_task(core)
    core.executors.outbox.fault = "timeout_after_write"
    r = run(core, a["agent_id"], "t_send")
    core.executors.outbox.fault = None
    r = core.coordinator.run(r["run_id"])
    assert (r["status"], r["health"]) == ("paused", "Paused")
    eff = core.db.one("SELECT * FROM effects WHERE tool_id='send_report'")
    assert eff["state"] == "UNKNOWN"
    trans = [(t["from_state"], t["to_state"]) for t in core.health.transitions(r["run_id"])]
    assert ("Healthy", "Unknown") in trans and ("Unknown", "Paused") in trans
    with pytest.raises(ConflictError) as e:
        core.coordinator.resume(r["run_id"], "u")
    assert e.value.code == "OUTCOME_UNKNOWN"
    v = core.versions.save(PID, a["accepted_branch_id"], None, "while unknown", "u")
    # the save barrier reconciles first; the outbox has the entry, so the effect resolves to COMPLETED
    assert core.db.scalar("SELECT state FROM effects WHERE operation_id=?", (eff["operation_id"],)) == "COMPLETED"
    assert not v["requires_reconciliation"]
    core.coordinator.resume(r["run_id"], "u", wait=True)
    assert core.executors.outbox.count(PID) == 1


def test_unknown_before_write_reconciles_to_failed_then_retries(core):
    a = _send_task(core)
    core.executors.outbox.fault = "timeout_before_write"
    r = run(core, a["agent_id"], "t_send")
    core.executors.outbox.fault = None
    op = core.db.scalar("SELECT operation_id FROM effects WHERE tool_id='send_report'")
    assert core.db.scalar("SELECT state FROM effects WHERE operation_id=?", (op,)) == "UNKNOWN"
    res = core.effects.reconcile(PID, op, "u")
    assert res["state"] == "FAILED" and "no outbox entry" in res["evidence"]
    core.coordinator.resume(r["run_id"], "u", wait=True)
    assert core.executors.outbox.count(PID) == 1
    assert core.coordinator.run(r["run_id"])["status"] == "completed"


def test_reconcile_requires_tool_support(core):
    a = _send_task(core)
    run(core, a["agent_id"], "t_send")
    op = core.db.scalar("SELECT operation_id FROM effects WHERE tool_id='write_report'")
    core.db.execute("UPDATE effects SET state='UNKNOWN' WHERE operation_id=?", (op,))
    with pytest.raises(ConflictError) as e:
        core.effects.reconcile(PID, op, "u")
    assert e.value.code == "NO_RECONCILER"
    assert core.effects.operator_resolve(PID, op, "COMPLETED", "u", "verified file on disk")["state"] == "COMPLETED"
