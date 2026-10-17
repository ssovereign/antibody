"""T05 (R05): a candidate passes fresh target cases and protected counterexamples; it can later be revised."""
import pytest

from conftest import PID, agent, ingest, ingest_text, run, task
from server.projects import ConflictError, InvalidRequest


def _discover(core):
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid], phrases=("Zürich",))
    a = agent(core)
    run(core, a["agent_id"], "t_enc")
    inc = core.incidents.list(PID)[0]
    return a, core.learning.candidate(inc["candidate_id"])


def test_candidate_evaluated_promoted_and_improves_fresh_task(core):
    a, cand = _discover(core)
    ev = core.learning.evaluate(PID, cand["candidate_id"], None, "user:test")
    assert ev["status"] == "passed", ev["summary"]
    kinds = {r["case_id"]: r for r in ev["results"]}
    assert {"enc_val_t1", "enc_val_t2", "enc_val_c1", "enc_val_c2"} <= set(kinds)
    assert all(kinds[c]["improved_over_baseline"] for c in ("enc_val_t1", "enc_val_t2"))
    assert kinds["enc_val_c1"]["with_candidate"]["lesson_applied"] is False
    assert not any(r["split"] == "final" for r in ev["results"])  # hidden final cases never reach the learner
    with pytest.raises(ConflictError):
        core.learning.promote(PID, cand["candidate_id"], 99, None, None, "assisted", "user")  # stale registry expectation
    res = core.learning.promote(PID, cand["candidate_id"], None, None, None, "assisted", "user:test")
    assert res["lesson"]["revision"] == 1 and res["registry_revision"] == 1 and res["acceptance_mode"] == "assisted"
    assert res["version"]["manifest"]["lesson_revisions"] == [res["lesson"]["ref"]]
    # fresh, different task on the same agent: the accepted record changes behaviour
    sid2 = ingest_text(core, "Lisboa → Porto ✓ shipment", "lisboa.md")
    task(core, "t_fresh", [sid2], out="out/lisboa.md", phrases=("Lisboa", "→"))
    r = run(core, a["agent_id"], "t_fresh")
    r = core.coordinator.run(r["run_id"])
    assert r["status_reason"] == "TASK_CHECK_PASSED"
    assert core.db.scalar("SELECT COUNT(*) FROM failures WHERE run_id=?", (r["run_id"],)) == 0
    assert core.db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE run_id=? AND lesson_ref=?", (r["run_id"], res["lesson"]["ref"])) == 1
    # ablation: the same fresh task with the lesson disabled on a branch fails again
    b = core.versions.create_branch(PID, a["agent_id"], "ablation", core.versions.branch(a["accepted_branch_id"])["state"])
    core.learning.disable_on_branch(b["branch_id"], res["lesson"]["ref"])
    r2 = run(core, a["agent_id"], "t_fresh", branch_id=b["branch_id"])
    assert core.db.scalar("SELECT COUNT(*) FROM failures WHERE run_id=?", (r2["run_id"],)) >= 1


def test_revision_supersedes_and_preserves_history(core):
    a, cand = _discover(core)
    core.learning.evaluate(PID, cand["candidate_id"], None, "u")
    first = core.learning.promote(PID, cand["candidate_id"], None, None, None, "assisted", "u")
    rev_c = core.learning.create_candidate(PID, "procedure", first["lesson"]["lesson_id"],
                                           dict(cand["body"], summary="narrower revision", negative_cases=["ascii", "fixture_2", "utf16"]),
                                           "u", parent_revision=1)
    core.learning.evaluate(PID, rev_c["candidate_id"], None, "u")
    second = core.learning.promote(PID, rev_c["candidate_id"], None, None, None, "assisted", "u")
    assert second["lesson"]["revision"] == 2
    all_l = {l["ref"]: l for l in core.registry.all_lessons(PID)}
    assert all_l[first["lesson"]["ref"]]["status"] == "superseded" and all_l[second["lesson"]["ref"]]["status"] == "accepted"
    assert [l["ref"] for l in core.registry.active(PID)] == [second["lesson"]["ref"]]
    with pytest.raises(Exception):  # accepted bodies are immutable at the database level
        core.db.execute("UPDATE lessons SET body_json='{}' WHERE lesson_id=?", (first["lesson"]["lesson_id"],))
    stale = core.learning.create_candidate(PID, "procedure", first["lesson"]["lesson_id"], dict(cand["body"], summary="stale"), "u", parent_revision=1)
    core.learning.evaluate(PID, stale["candidate_id"], None, "u")
    with pytest.raises(ConflictError) as e:
        core.learning.promote(PID, stale["candidate_id"], None, None, None, "assisted", "u")
    assert e.value.code == "STALE_PARENT"


def test_overbroad_candidate_rejected_and_remembered(core):
    body = {"kind": "procedure", "family": "encoding", "scope": {"tool": "write_report"}, "conditions": {}, "procedure": {"encoding": "utf-16"},
            "summary": "always UTF-16 (seeded test condition)", "seeded_test_condition": True}
    c = core.learning.create_candidate(PID, "procedure", "lesson_utf16_everywhere", body, "seed")
    ev = core.learning.evaluate(PID, c["candidate_id"], None, "u")
    assert ev["status"] == "failed" and "protected regression failed" in ev["summary"]["reasons"]
    assert core.learning.candidate(c["candidate_id"])["status"] == "Rejected"
    with pytest.raises(ConflictError):
        core.learning.promote(PID, c["candidate_id"], None, None, None, "assisted", "u")
    with pytest.raises(ConflictError) as e:
        core.learning.create_candidate(PID, "procedure", "lesson_utf16_everywhere", body, "seed")
    assert e.value.code == "PREVIOUSLY_REJECTED"


def test_learner_cannot_touch_permissions_or_checker(core):
    with pytest.raises(InvalidRequest):
        core.learning.create_candidate(PID, "procedure", "x", {"procedure": {"recipient_ids": ["evil"]}}, "learner")
    with pytest.raises(InvalidRequest):
        core.learning.create_candidate(PID, "procedure", "x", {"tools": ["delete_file"]}, "learner")
    bad = core.learning.create_candidate(PID, "procedure", "x2", {"family": "encoding", "scope": {"tool": "write_report"}, "procedure": {"path": "/etc"}}, "learner")
    core.db.execute("UPDATE candidates SET status='Testing' WHERE candidate_id=?", (bad["candidate_id"],))
    with pytest.raises(ConflictError):  # no passing evaluation; cannot self-certify
        core.learning.promote(PID, bad["candidate_id"], None, None, None, "assisted", "learner")


def test_checker_change_refuses_evaluation(core, monkeypatch):
    from server.evaluation import evaluator
    monkeypatch.setattr(evaluator, "checker_hash", lambda: "sha256:tampered")
    with pytest.raises(InvalidRequest) as e:
        core.evaluator.suite("suite_core@1")
    assert e.value.code == "CHECKER_CHANGED"


def test_automatic_acceptance_for_owner_enabled_class(core):
    core.projects.update_settings(PID, {"auto_promote_enabled": True, "auto_promote_classes": ["procedure:write_report"]}, "owner")
    a, cand = _discover(core)
    core.learning.evaluate(PID, cand["candidate_id"], None, "u")
    l = core.registry.active(PID)[0]
    assert l["acceptance_mode"] == "automatic"
