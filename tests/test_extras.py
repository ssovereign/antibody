"""Additional behaviours: subagents, preferences, job leases, controlled attribution, lesson conflicts, expiry, cost budget."""
import json

from conftest import APP, PID, agent, ingest, ingest_text, run, task
from server.canonical import utcnow


def test_subagent_gets_own_context_and_hands_back_evidence(core):
    sid = ingest(core, "ops_ascii.md")
    task(core, "t_parent", [sid])
    task(core, "t_child", [sid], out="child/out.md")
    a = agent(core, env={"export_adapter": "fixture_2"})
    parent = run(core, a["agent_id"], "t_parent")
    res = core.coordinator.spawn_subagent(parent["run_id"], "t_child", "user")
    child = res["child"]
    assert child["parent_run_id"] == parent["run_id"] and child["branch_id"] != parent["branch_id"]
    assert child["thread_id"] != parent["thread_id"]
    h = res["handoff_source"]
    assert h["channel"] == "subagent" and h["origin"] in ("EXTERNAL_DATA", "AGENT_DERIVED") and h["deps"]
    assert h["metadata"]["child_run_id"] == child["run_id"]


def test_user_preferences_are_scoped(core):
    sid = ingest(core, "ops_ascii.md")
    task(core, "t", [sid])
    a = agent(core, "A", env={"export_adapter": "fixture_2"})
    b = agent(core, "B", env={"export_adapter": "fixture_2"})
    core.db.execute("INSERT INTO preferences(project_id,scope,scope_id,key,value_json,confirmed_by,created_at) VALUES (?,?,?,?,?,?,?)",
                    (PID, "agent", a["agent_id"], "tone", '"formal"', "user:test", utcnow()))
    ra = run(core, a["agent_id"], "t")
    rb = run(core, b["agent_id"], "t")
    pa = json.loads(core.objects.get(PID, core.db.scalar("SELECT packet_ref FROM context_packets WHERE run_id=? LIMIT 1", (ra["run_id"],))))
    pb = json.loads(core.objects.get(PID, core.db.scalar("SELECT packet_ref FROM context_packets WHERE run_id=? LIMIT 1", (rb["run_id"],))))
    assert any(m["key"] == "tone" for m in pa["memory"]) and not any(m["key"] == "tone" for m in pb["memory"])


def test_job_lease_claim_and_stale_completion(core):
    jid = core.jobs.enqueue(PID, "noop", {})
    core.jobs.register("noop", lambda p: {"ok": True})
    j1 = core.jobs.claim("w1")
    assert j1["job_id"] == jid and core.jobs.claim("w2") is None
    core.db.execute("UPDATE jobs SET lease_expires='2000-01-01T00:00:00.000000Z' WHERE job_id=?", (jid,))
    j2 = core.jobs.claim("w2")  # replacement worker claims the expired lease
    assert j2["claim_revision"] == j1["claim_revision"] + 1
    assert core.jobs.complete(j1, True, {"stale": True}) is False  # the stale worker cannot overwrite
    assert core.jobs.complete(j2, True, {"ok": True}) is True
    assert core.jobs.get(jid)["result"] == {"ok": True}


def test_counterfactual_attribution_supports_hypothesis(core):
    sid = ingest(core, "vendor_update.html", "web")
    task(core, "t_v", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, naive=True, env={"export_adapter": "fixture_2"})
    run(core, a["agent_id"], "t_v")
    inc = [i for i in core.incidents.list(PID) if i["category"] == "blocked_action"][0]
    assert any(o.get("level") == "verified_outcome" and o["executor_invocations"] == 0 for o in inc["observed"])
    res = core.attribution.counterfactual(inc["incident_id"], repeats=2)
    assert res["reproduced_with_original"] and res["absent_when_masked"] and res["conclusion"] == "intervention_supported"
    assert core.incidents.get(inc["incident_id"])["attribution_level"] == "intervention_supported"
    assert core.objects.exists(PID, res["evidence_ref"])


def test_conflicting_lessons_open_incident_instead_of_silent_merge(core):
    from server.learning.service import LearningService
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid])
    a = agent(core)
    run(core, a["agent_id"], "t_enc")
    cand = core.learning.candidate(core.incidents.list(PID)[0]["candidate_id"])
    core.learning.evaluate(PID, cand["candidate_id"], None, "u")
    core.learning.promote(PID, cand["candidate_id"], None, None, None, "assisted", "u")
    # an operator-inserted conflicting record (bypassing evaluation) is detected at compile time
    body = dict(cand["body"], procedure={"encoding": "utf-16"}, lesson_id="lesson_other", revision=1)
    reg = core.registry.head(PID)
    core.db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (PID, "lesson_other", 1, "procedure", "accepted", "write_report", json.dumps(body), "x", "c", 1, "e", "assisted", reg, "op", utcnow()))
    sid2 = ingest_text(core, "Oslo → Bergen ✓", "oslo.md")
    task(core, "t2", [sid2], out="o.md")
    r = run(core, a["agent_id"], "t2")
    incs = [i for i in core.incidents.list(PID) if i["run_id"] == r["run_id"] and i["category"] == "lesson_conflict"]
    assert len(incs) == 1
    assert core.coordinator.run(r["run_id"])["status_reason"] == "TASK_CHECK_PASSED"


def test_expired_pattern_not_retrieved(core):
    reg = core.registry.head(PID)
    for lid, exp in (("p_old", "2000-01-01T00:00:00.000000Z"), ("p_new", "2999-01-01T00:00:00.000000Z")):
        core.db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (PID, lid, 1, "threat_pattern", "accepted", None, json.dumps({"expires_at": exp}), "x", "c", 1, "e", "assisted", reg, "op", utcnow()))
    assert [l["lesson_id"] for l in core.registry.active(PID)] == ["p_new"]


def test_run_cost_budget(make_core, tmp_path):
    price = tmp_path / "price.json"
    price.write_text(json.dumps({"models": {"reference-planner-1": {"input_per_mtok": "1000000", "output_per_mtok": "0"}}}))
    c = make_core(PRICE_CONFIG_PATH=str(price), MAX_RUN_COST=0.0005)
    c.projects.create_project(PID, "x", "o")
    sid = ingest(c, "ops_ascii.md")
    task(c, "t", [sid])
    a = agent(c, env={"export_adapter": "fixture_2"})
    r = run(c, a["agent_id"], "t")
    r = c.coordinator.run(r["run_id"])
    assert (r["status"], r["status_reason"]) == ("paused", "BUDGET_EXCEEDED")
    assert json.loads(r["cost_json"])["price_source"] == "fixtures/price_config.json"
