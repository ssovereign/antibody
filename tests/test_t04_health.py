"""T04 (R04): repeated failures and stalled progress trigger bounded recovery or pause."""
from conftest import PID, agent, ingest, ingest_text, kinds, run, task
from server.schemas import ActionProposal


def test_repeated_failure_triggers_bounded_recovery(core):
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid], phrases=("Zürich", "→"))
    a = agent(core)
    r = run(core, a["agent_id"], "t_enc")
    r = core.coordinator.run(r["run_id"])
    assert (r["status"], r["status_reason"], r["health"]) == ("completed", "RECOVERED_IN_BRANCH", "Healthy")
    trans = [(t["from_state"], t["to_state"], t["reason_code"]) for t in core.health.transitions(r["run_id"])]
    assert ("Healthy", "Degraded", "REPEAT_FAILURE") in trans
    assert ("Degraded", "Recovering", "BOUNDED_REPAIR") in trans
    assert ("Recovering", "Healthy", "INDEPENDENT_CHECKS_PASS") in trans
    inc = core.incidents.list(PID)[0]
    assert inc["category"] == "execution_failure" and inc["attribution_level"] == "intervention_supported"
    child = core.coordinator.run(inc["recovery_run_id"])
    assert child["parent_run_id"] == r["run_id"] and child["branch_id"] != r["branch_id"]
    cfg = core.coordinator.run_view(child["run_id"])["start_config"]
    assert cfg["limits"]["max_tool_steps"] == core.config.MAX_REPAIR_STEPS
    # recovery is not acceptance: the candidate is still a draft until the fixed suite runs
    assert core.learning.candidate(inc["candidate_id"])["status"] == "Draft"
    assert core.registry.active(PID) == []


def test_repeat_failure_pauses_without_recovery(make_core):
    c = make_core(AUTO_RECOVERY=False)
    c.projects.create_project(PID, "x", "o")
    sid = ingest(c, "q3_notes.md")
    task(c, "t_enc", [sid])
    a = agent(c)
    r = run(c, a["agent_id"], "t_enc")
    r = c.coordinator.run(r["run_id"])
    assert (r["status"], r["status_reason"], r["health"]) == ("paused", "REPEAT_FAILURE_LIMIT", "Paused")


def test_step_budget_pauses(core):
    sids = [ingest_text(core, f"note {i}", f"n{i}.md") for i in range(6)]
    task(core, "t_budget", sids, max_tool_steps=3)
    a = agent(core)
    r = run(core, a["agent_id"], "t_budget")
    r = core.coordinator.run(r["run_id"])
    assert r["status"] == "paused" and r["status_reason"] == "BUDGET_EXCEEDED"
    assert core.db.scalar("SELECT COUNT(*) FROM executor_invocations") == 3


def test_no_progress_pauses(core):
    sid = ingest(core, "ops_ascii.md")
    task(core, "t_stall", [sid], tools=("read_source", "write_report", "list_files"))
    a = agent(core)
    r = core.coordinator.start_run(PID, a["agent_id"], "t_stall", wait=False)
    core.jobs.cancel(core.db.scalar("SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT 1"))
    for _ in range(3):
        act, op = core.coordinator.issue_operation(r, "read_source")
        v = core.gateway.submit(r["run_id"], ActionProposal(action_id=act, operation_id=op, tool_id="read_file",
                                                            arguments={"path": "missing.md"}), core.worker_identity)
        core.coordinator.process_result(r, {"tool_id": "read_file", "arguments": {"path": "missing.md"}}, v.model_dump(), {}, [])
        core.health.on_step(r["run_id"], False)
    r = core.coordinator.run(r["run_id"])
    assert r["status"] == "paused" and r["status_reason"] == "NO_PROGRESS"
    assert "health.changed" in kinds(core, r["run_id"])


def test_failed_repair_pauses_parent(core):
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid])
    a = agent(core, env={"export_adapter": "fixture_1"})
    # make the generated candidate useless so the repair branch fails too
    orig = core.learning._procedure_candidate

    def bad(run, inc):
        c = orig(run, inc)
        return core.learning.revise_candidate(c["candidate_id"], {"procedure": {"encoding": "cp1252"}}, "test")
    core.learning._procedure_candidate = bad
    r = run(core, a["agent_id"], "t_enc")
    r = core.coordinator.run(r["run_id"])
    assert (r["status"], r["status_reason"], r["health"]) == ("paused", "REPAIR_FAILED", "Paused")
