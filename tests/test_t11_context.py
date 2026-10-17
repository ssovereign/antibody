"""T11 (R10): the context cap holds; mandatory permissions and source labels survive."""
from conftest import PID, agent, ingest, ingest_text, run, task
from server.context.compiler import presets


def test_cap_respected_and_mandatory_preserved(core):
    sids = [ingest_text(core, f"Region {i} " + ("detail " * 260), f"region{i}.md") for i in range(6)]
    task(core, "t_ctx", sids, tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, env={"export_adapter": "fixture_2"})
    r = run(core, a["agent_id"], "t_ctx", context_budget_tokens=3000)
    packets = core.db.all("SELECT * FROM context_packets WHERE run_id=? ORDER BY created_at", (r["run_id"],))
    import json
    for p in packets:
        assert p["token_count"] <= p["budget_tokens"] and p["count_kind"] == "estimated"
    last = json.loads(core.objects.get(PID, packets[-1]["packet_ref"]))
    assert last["mandatory"]["assignment"]["recipient_ids"] == ["finance_team"]
    assert last["mandatory"]["assignment"]["tools"] == ["read_source", "write_report", "send_report"]
    assert {t["tool_id"] for t in last["mandatory"]["tool_schemas"]} == {"read_source", "write_report", "send_report"}
    assert any(o["reason"] == "budget" for o in last["omitted"])
    assert all(o["origin"] == "EXTERNAL_DATA" and o["ref"].startswith("src_") for o in last["observations"])
    assert last["summaries"] and last["summaries"][0]["origin"] == "AGENT_DERIVED" and last["summaries"][0]["deps"]


def test_mandatory_overflow_pauses_instead_of_truncating(core):
    sid = ingest(core, "ops_ascii.md")
    task(core, "t_tiny", [sid])
    a = agent(core)
    r = run(core, a["agent_id"], "t_tiny", context_budget_tokens=600)
    r = core.coordinator.run(r["run_id"])
    assert (r["status"], r["status_reason"]) == ("paused", "CONTEXT_BUDGET")
    assert core.db.scalar("SELECT COUNT(*) FROM executor_invocations") == 0


def test_revoked_lesson_from_old_manifest_is_not_compiled(core):
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid])
    a = agent(core)
    run(core, a["agent_id"], "t_enc")
    cand = core.learning.candidate(core.incidents.list(PID)[0]["candidate_id"])
    core.learning.evaluate(PID, cand["candidate_id"], None, "u")
    res = core.learning.promote(PID, cand["candidate_id"], None, None, None, "assisted", "u")
    old = res["version"]
    assert old["manifest"]["lesson_revisions"] == [res["lesson"]["ref"]]
    core.learning.revoke(PID, res["lesson"]["lesson_id"], None, "harmful", "u")
    preview = core.versions.preview_restore(PID, old["version_id"])
    assert preview["excluded_knowledge"][0]["ref"] == res["lesson"]["ref"]
    f = core.versions.fork(PID, old["version_id"], "old-state", "restore", "u")
    r = run(core, a["agent_id"], "t_enc", branch_id=f["branch"]["branch_id"])
    pkt = core.db.one("SELECT omitted_json FROM context_packets WHERE run_id=? ORDER BY created_at LIMIT 1", (r["run_id"],))
    assert "globally revoked" in pkt["omitted_json"]
    assert core.db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE run_id=? AND use_kind='applied_procedure'", (r["run_id"],)) == 0


def test_presets_and_quarantine_invalidation(core):
    p = presets(32000)
    assert p["Small"] < p["Standard"] < p["Large"] <= 32000
    sid = ingest(core, "vendor_update.html", "web")
    task(core, "t_v", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, naive=True, env={"export_adapter": "fixture_2"})
    r = run(core, a["agent_id"], "t_v")
    assert core.db.scalar("SELECT COUNT(*) FROM context_packets WHERE run_id=? AND valid=0", (r["run_id"],)) >= 1
    # the clean packet after repair excludes the quarantined span but keeps benign material
    import json
    last = core.db.one("SELECT packet_ref FROM context_packets WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (r["run_id"],))
    pk = json.loads(core.objects.get(PID, last["packet_ref"]))
    text = " ".join(o["text"] for o in pk["observations"])
    assert "audit-review@exfil.example" not in text and "[excluded: quarantined span]" in text and "Zürich" in text
