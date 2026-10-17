"""T06 (R06): agent B loads an accepted revision with an acknowledgement and uses compatible knowledge."""
from conftest import PID, agent, ingest, ingest_text, run, task


def test_agent_b_loads_and_uses_procedure(core):
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid])
    a = agent(core, "Agent A")
    run(core, a["agent_id"], "t_enc")
    cand = core.learning.candidate(core.incidents.list(PID)[0]["candidate_id"])
    core.learning.evaluate(PID, cand["candidate_id"], None, "u")
    res = core.learning.promote(PID, cand["candidate_id"], None, None, None, "assisted", "u")
    b = agent(core, "Agent B")
    sid2 = ingest_text(core, "Bodø ✓ → depot open", "bodo.md")
    task(core, "t_b", [sid2], out="b/bodo.md", phrases=("Bodø",))
    rb = run(core, b["agent_id"], "t_b")
    rb = core.coordinator.run_view(rb["run_id"])
    assert rb["status_reason"] == "TASK_CHECK_PASSED"
    assert rb["registry_acks"][0]["revision"] == res["registry_revision"]
    loaded = [e for e in core.journal.events(rb["run_id"]) if e["kind"] == "registry.loaded"][0]
    assert res["lesson"]["ref"] in loaded["payload"]["records"]
    assert core.db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE run_id=?", (rb["run_id"],)) == 1
    # B's own mutable state is separate from A's
    assert b["accepted_branch_id"] != a["accepted_branch_id"]
    # incompatible environment: lesson is filtered by applicability, not applied
    c = agent(core, "Agent C", env={"export_adapter": "fixture_2"})
    rc = run(core, c["agent_id"], "t_b")
    pkt = core.db.one("SELECT omitted_json FROM context_packets WHERE run_id=? ORDER BY created_at LIMIT 1", (rc["run_id"],))
    assert "not applicable" in pkt["omitted_json"]
    assert core.db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE run_id=?", (rc["run_id"],)) == 0


def test_pattern_reaches_agent_b_and_intercepts_earlier(core):
    sid = ingest(core, "vendor_update.html", "web")
    task(core, "t_vendor", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, "Agent A", naive=True, env={"export_adapter": "fixture_2"})
    ra = run(core, a["agent_id"], "t_vendor")
    assert core.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=? AND outcome='DENY'", (ra["run_id"],)) == 1
    inc = [i for i in core.incidents.list(PID) if i["category"] == "blocked_action"][0]
    cand = core.learning.candidate(inc["candidate_id"])
    assert cand["kind"] == "threat_pattern"
    ev = core.learning.evaluate(PID, cand["candidate_id"], None, "u")
    assert ev["status"] == "passed", ev["summary"]
    benign = [r for r in ev["results"] if r["kind"] == "benign_near_match"]
    assert benign and all(r["with_candidate"]["ingress_quarantines"] == 0 for r in benign)
    res = core.learning.promote(PID, cand["candidate_id"], None, None, None, "assisted", "u")
    # Agent B encounters a related held-out variant in a calendar invite
    sid2 = ingest(core, "offsite_invite.ics", "calendar")
    task(core, "t_invite", [sid2], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    b = agent(core, "Agent B", naive=True, env={"export_adapter": "fixture_2"})
    rb = run(core, b["agent_id"], "t_invite")
    rb = core.coordinator.run_view(rb["run_id"])
    assert rb["registry_acks"][0]["revision"] == res["registry_revision"]
    assert rb["counts"]["quarantined_spans"] >= 1
    assert rb["counts"]["denied"] == 0  # caught before model ingestion: no unsafe proposal reached the gateway
    assert rb["status_reason"] == "TASK_CHECK_PASSED"
    assert core.executors.outbox.count(PID, address="external.reviewer@partner-mail.example") == 0
    ev_kinds = [e["kind"] for e in core.journal.events(rb["run_id"])]
    assert ev_kinds.index("source.quarantined") < ev_kinds.index("model.called", ev_kinds.index("source.quarantined"))
    # benign security article is not quarantined by the same pattern
    sid3 = ingest(core, "security_article.html", "web")
    task(core, "t_article", [sid3], tools=("read_source", "write_report"))
    rc = run(core, b["agent_id"], "t_article")
    assert core.db.scalar("SELECT COUNT(*) FROM dispositions WHERE run_id=? AND disposition='quarantined'", (rc["run_id"],)) == 0


def test_registry_refresh_at_safe_boundary_and_revocation_before_dispatch(core):
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid])
    a = agent(core)
    run(core, a["agent_id"], "t_enc")
    cand = core.learning.candidate(core.incidents.list(PID)[0]["candidate_id"])
    core.learning.evaluate(PID, cand["candidate_id"], None, "u")
    res = core.learning.promote(PID, cand["candidate_id"], None, None, None, "assisted", "u")
    rev = core.learning.revoke(PID, res["lesson"]["lesson_id"], None, "test revoke", "u")
    assert rev["registry_revision"] == res["registry_revision"] + 1
    assert core.registry.active(PID) == []
    snap = core.registry.snapshot(PID)
    assert snap["revision"] == rev["registry_revision"] and f"{res['lesson']['lesson_id']}@*" in snap["revocations"]
