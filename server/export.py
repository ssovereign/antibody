"""Export run manifests, results and honest live / recorded / simulated labels."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .canonical import loads, utcnow
from .config import ROOT


def build_export(core, project_id: str) -> dict:
    db = core.db
    runs = []
    for r in db.all("SELECT run_id FROM runs WHERE project_id=? ORDER BY created_at", (project_id,)):
        v = core.coordinator.run_view(r["run_id"])
        runs.append({k: v[k] for k in ("run_id", "agent_id", "branch_id", "parent_run_id", "task_id", "task_revision", "adapter", "mode",
                                       "status", "status_reason", "health", "policy_revision", "registry_revision", "base_version",
                                       "context_budget_tokens", "start_config", "start_config_hash", "cost", "counts", "capabilities",
                                       "registry_acks", "health_transitions", "created_at")})
    modes = {}
    for r in runs:
        modes[r["mode"]] = modes.get(r["mode"], 0) + 1
    evals = core.evaluator.evaluations(project_id)
    versions = [{"version_id": v["version_id"], "branch_id": v["branch_id"], "label": v["label"], "parents": v["parents"],
                 "manifest": v["manifest"], "created_at": v["created_at"]} for v in core.versions.versions(project_id)]
    decisions = db.all("SELECT decision_id, run_id, tool_id, outcome, reason_code, attribution_status, policy_revision, task_revision, created_at FROM decisions WHERE project_id=? ORDER BY created_at", (project_id,))
    return {
        "schema_version": 1,
        "generated_at": utcnow(),
        "project": core.projects.get_project(project_id),
        "honesty": {
            "run_modes": modes,
            "model_note": "Runs in 'simulated' mode use the deterministic reference planner (a scripted model). The gateway, storage, "
                          "executors, checks, learning, registry and version operations execute real code. 'recorded' runs replay a trace "
                          "and are never counted as fresh live attack successes.",
            "environment_note": "write_report uses a declared legacy-encoding export fixture (fixture_1, cp1252 default); it is an encoding fixture, not a live Windows claim.",
            "destinations_note": "The outbox and payment ledger are local test destinations with synthetic identities.",
            "benchmark_note": "suite_core is a custom controlled suite inspired by AgentDojo; it is not an official benchmark result.",
        },
        "config": core.config.public_view(),
        "policy_head": core.projects.policy(project_id),
        "tools": core.projects.tools(project_id),
        "capabilities": core.coordinator.capability_profiles(),
        "counts": {
            "runs": len(runs),
            "decisions": len(decisions),
            "denied": sum(1 for d in decisions if d["outcome"] == "DENY"),
            "paused": sum(1 for d in decisions if d["outcome"] == "PAUSE"),
            "denied_executor_invocations": db.scalar("SELECT COUNT(*) FROM executor_invocations i JOIN decisions d ON d.decision_id=i.decision_id WHERE d.project_id=? AND d.outcome!='ALLOW'", (project_id,)),
            "effects_by_state": {r["state"]: r["n"] for r in db.all("SELECT state, COUNT(*) n FROM effects WHERE project_id=? GROUP BY state", (project_id,))},
            "outbox_entries": core.executors.outbox.count(project_id),
            "early_interceptions": db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE project_id=? AND use_kind='early_interception'", (project_id,)),
            "lesson_applications": db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE project_id=? AND use_kind='applied_procedure'", (project_id,)),
        },
        "runs": runs,
        "decisions": decisions,
        "effects": core.effects.list(project_id),
        "incidents": core.incidents.list(project_id),
        "candidates": core.learning.candidates(project_id),
        "lessons": core.registry.all_lessons(project_id),
        "registry": core.registry.snapshot(project_id),
        "evaluations": evals,
        "versions": versions,
    }


def write_export(core, project_id: str, out_dir: str | None = None, final_eval: dict | None = None) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = Path(out_dir or ROOT / "exports" / f"{project_id}_{ts}")
    d.mkdir(parents=True, exist_ok=True)
    bundle = build_export(core, project_id)
    (d / "export.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    with open(d / "events.jsonl", "w", encoding="utf-8") as fh:
        for r in core.db.all("SELECT * FROM events WHERE project_id=? ORDER BY timestamp_utc, sequence", (project_id,)):
            fh.write(json.dumps({k: r[k] for k in ("event_id", "run_id", "sequence", "timestamp_utc", "kind", "actor_type", "payload_ref")}
                                | {"payload": loads(r["payload_json"], {}), "evidence_refs": loads(r["evidence_refs_json"], [])}, ensure_ascii=False) + "\n")
    (d / "manifests.json").write_text(json.dumps(bundle["versions"], ensure_ascii=False, indent=1), encoding="utf-8")
    if final_eval:
        (d / "final_evaluation.json").write_text(json.dumps(final_eval, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "SUMMARY.md").write_text(summary_markdown(bundle, final_eval), encoding="utf-8")
    return d


def summary_markdown(b: dict, final_eval: dict | None) -> str:
    c = b["counts"]
    lines = [f"# ANTIBODY export: {b['project']['project_id']}", "", f"Generated {b['generated_at']}.", "",
             "## Labels", "", f"- Run modes: {json.dumps(b['honesty']['run_modes'])}", f"- {b['honesty']['model_note']}",
             f"- {b['honesty']['environment_note']}", f"- {b['honesty']['destinations_note']}", f"- {b['honesty']['benchmark_note']}", "",
             "## Counts (from execution records)", "",
             f"- Runs: {c['runs']}; decisions: {c['decisions']} ({c['denied']} denied, {c['paused']} paused)",
             f"- Executor invocations for non-allowed decisions: {c['denied_executor_invocations']}",
             f"- Effects by state: {json.dumps(c['effects_by_state'])}", f"- Local outbox entries: {c['outbox_entries']}",
             f"- Early interceptions by accepted patterns: {c['early_interceptions']}", f"- Procedure lesson applications: {c['lesson_applications']}", "",
             "## Evaluations", ""]
    for e in b["evaluations"]:
        lines.append(f"- {e['evaluation_id']} ({e['subject'].get('kind')} {e['subject'].get('ref', '')}): **{e['status']}** - "
                     + "; ".join(f"{k}: {v}" for k, v in e["summary"].items() if isinstance(v, str)))
    if final_eval:
        lines += ["", "## Frozen final comparison (baseline ladder)", "", f"Suite {final_eval['suite']}, mode {final_eval['mode']}, {final_eval['repeats']} repeats per case (deterministic planner: repeats are identical).", "",
                  "| Baseline | Family | Kind | Task completion | Unauthorized effects | Unsafe proposals | Early interceptions | False positives | Outcome expectations met | Steps |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in final_eval["table"]:
            lines.append(f"| {r['baseline']} | {r['family']} | {r['kind']} | {r['task_completion']} | {r['unauthorized_effects']} | {r['unsafe_proposals']} | "
                         f"{r['early_quarantines']} | {r['false_positives']} | {r['pass_rate']} | {r['steps']} |")
    lines += ["", "Failing as well as passing cases are included in export.json and final_evaluation.json."]
    return "\n".join(lines) + "\n"
