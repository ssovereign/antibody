"""Effects search and run impact: a user investigating a bad run can see what it did and what to do about it."""
from conftest import PID, agent, ingest, run, task


def _bad_run(core):
    sid = ingest(core, "vendor_update.html", "web")
    task(core, "t_v", [sid], tools=("read_source", "write_report", "send_report"), recipients=("finance_team",))
    a = agent(core, naive=True, env={"export_adapter": "fixture_2"})
    return a, run(core, a["agent_id"], "t_v")


def test_filters_and_descriptions(core):
    a, r = _bad_run(core)
    other = agent(core, "Other", env={"export_adapter": "fixture_2"})
    sid = ingest(core, "ops_ascii.md")
    task(core, "t_o", [sid])
    run(core, other["agent_id"], "t_o")
    mine = core.effects.list(PID, run_id=r["run_id"])
    assert mine and all(e["run_id"] == r["run_id"] for e in mine)
    assert {e["agent_name"] for e in mine} == {"Agent A"}
    denied = core.effects.list(PID, run_id=r["run_id"], state="DENIED")
    assert len(denied) == 1 and denied[0]["summary"].startswith("blocked: send") and "exfil" in denied[0]["summary"]
    assert [e["tool_id"] for e in core.effects.list(PID, effect_class="external_send", state="COMPLETED")] == ["send_report"]
    assert all(e["agent_id"] == other["agent_id"] for e in core.effects.list(PID, agent_id=other["agent_id"]))
    hits = core.effects.list(PID, text="audit-review")
    assert len(hits) == 1 and hits[0]["state"] == "DENIED"
    assert all(e["effect_class"] != "read" for e in core.effects.list(PID, external_only=True))


def test_run_impact_lists_what_happened_and_how_to_mitigate(core):
    a, r = _bad_run(core)
    imp = core.effects.impact(PID, r["run_id"])
    assert imp["counts"]["messages_sent"] == 1 and imp["messages"][0]["recipient_id"] == "finance_team"
    assert imp["counts"]["blocked"] == 1 and imp["blocked"][0]["reason_code"] == "DESTINATION_NOT_AUTHORIZED"
    assert imp["counts"]["file_changes"] == 1 and imp["counts"]["quarantined_spans"] >= 1
    kinds = [s["kind"] for s in imp["mitigation"]]
    assert kinds == ["contact", "files", "blocked"]
    assert imp["incidents"][0]["category"] == "blocked_action"


def test_impact_includes_child_runs(core):
    sid = ingest(core, "q3_notes.md")
    task(core, "t_enc", [sid])
    a = agent(core)
    r = run(core, a["agent_id"], "t_enc")
    imp = core.effects.impact(PID, r["run_id"])
    assert len(imp["family"]) == 2  # the run and its repair branch
    assert imp["counts"]["file_changes"] == 1  # the report was written by the repair run
    assert len(core.effects.impact(PID, r["run_id"], include_children=False)["family"]) == 1
