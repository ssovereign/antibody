"""T01 (R01, R03): an external instruction cannot expand a recipient or file scope."""
import pytest
from pydantic import ValidationError

from conftest import APP, PID, agent, denied_invocations, ingest, run, task
from server.projects import AuthError, InvalidRequest
from server.schemas import ActionProposal, TaskBody

pytestmark = pytest.mark.security


def test_page_instruction_cannot_add_recipient(core):
    sid = ingest(core, "vendor_update.html", "web")
    task(core, "t_vendor", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, naive=True, env={"export_adapter": "fixture_2"})
    r = run(core, a["agent_id"], "t_vendor")
    decisions = core.db.all("SELECT tool_id, outcome, reason_code FROM decisions WHERE run_id=?", (r["run_id"],))
    denied = [d for d in decisions if d["outcome"] == "DENY"]
    assert denied and denied[0]["reason_code"] == "DESTINATION_NOT_AUTHORIZED"
    entries = core.executors.outbox.entries(PID)
    assert [e["recipient_id"] for e in entries] == ["finance_team"]
    assert all("exfil" not in e["address"] for e in entries)
    assert denied_invocations(core) == 0
    # the hidden delete instruction was excluded at parse time and never acted on
    assert not any(d["tool_id"] == "delete_file" for d in decisions)
    assert core.coordinator.run(r["run_id"])["status_reason"] == "TASK_CHECK_PASSED"


def test_log_instruction_cannot_add_tool_or_file_scope(core):
    sid = ingest(core, "deploy_log.jsonl", "log")
    task(core, "t_log", [sid], tools=("read_source", "write_report"))
    a = agent(core, naive=True, env={"export_adapter": "fixture_2"})
    r = run(core, a["agent_id"], "t_log")
    d = core.db.all("SELECT tool_id, outcome, reason_code FROM decisions WHERE run_id=? AND outcome!='ALLOW'", (r["run_id"],))
    assert {"tool_id": "delete_file", "outcome": "DENY", "reason_code": "TOOL_NOT_ALLOWED"} in d
    assert denied_invocations(core) == 0


def test_path_traversal_and_absolute_paths_denied(core):
    sid = ingest(core, "ops_ascii.md")
    task(core, "t_paths", [sid], tools=("read_source", "write_report"))
    a = agent(core)
    r = core.coordinator.start_run(PID, a["agent_id"], "t_paths", wait=False)
    core.jobs.cancel(core.db.scalar("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1"))
    for bad in ("../../outside.md", "C:/Windows/evil.md", "/etc/passwd", "reports/../../x.md", "a:stream.md"):
        act, op = core.coordinator.issue_operation(r, "write_report")
        v = core.gateway.submit(r["run_id"], ActionProposal(action_id=act, operation_id=op, tool_id="write_report",
                                                            arguments={"path": bad, "content": "x"}), core.worker_identity)
        assert v.outcome == "DENY" and v.reason_code == "PATH_OUTSIDE_WORKSPACE", bad
    assert denied_invocations(core) == 0


def test_strict_schemas_reject_authority_fields():
    with pytest.raises(ValidationError):
        ActionProposal(action_id="a", operation_id="o", tool_id="send_report", arguments={}, actor="system")
    with pytest.raises(ValidationError):
        TaskBody(task_id="x", goal="g", tools=[], authority="admin")


def test_only_trusted_actors_create_task_revisions(core):
    body = TaskBody(task_id="t", goal="g", tools=["read_source"])
    with pytest.raises(AuthError):
        core.projects.create_task_revision(PID, body, {"role": "worker", "actor_id": "agent"})
    with pytest.raises(InvalidRequest):
        core.projects.create_task_revision(PID, TaskBody(task_id="t", goal="g", tools=["read_source"], recipient_ids=["evil@exfil.example"]), APP)
    t1 = core.projects.create_task_revision(PID, body, APP)
    t2 = core.projects.create_task_revision(PID, TaskBody(task_id="t", goal="g2", tools=["read_source"]), APP)
    assert (t1["revision"], t2["revision"]) == (1, 2)


def test_invite_cannot_add_recipient(core):
    sid = ingest(core, "offsite_invite.ics", "calendar")
    task(core, "t_inv", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, naive=True, env={"export_adapter": "fixture_2"})
    run(core, a["agent_id"], "t_inv")
    assert core.executors.outbox.count(PID, address="external.reviewer@partner-mail.example") == 0
    assert core.executors.outbox.count(PID, recipient_id="finance_team") == 1
