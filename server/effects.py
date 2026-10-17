"""Permanent effect journal queries and tool-specific reconciliation.

The journal is project-level history: restoring an agent version never
truncates or rewinds it. UNKNOWN becomes COMPLETED or FAILED only through a
tool-specific reconciliation function; a model's claim is not reconciliation.
"""
from __future__ import annotations

from .canonical import dumps, loads, utcnow
from .projects import ConflictError, NotFound


def describe_effect(e: dict) -> str:
    """One plain-language line describing what an effect did (or tried to do)."""
    a = e.get("arguments") or {}
    verb = {"COMPLETED": "", "FAILED": "failed: ", "DENIED": "blocked: ", "UNKNOWN": "maybe (unknown): ",
            "RESERVED": "pending: ", "DISPATCHING": "in progress: "}.get(e["state"], "")
    t = e["tool_id"]
    if t == "send_report" or "recipient_id" in a:
        what = f"send {a.get('artifact_path', 'a message')} to {a.get('recipient_id', '?')}"
    elif "to" in a:
        what = f"send a message to {a['to']}"
    elif t == "write_report":
        what = f"write file {a.get('path', '?')}"
    elif t == "delete_file":
        what = f"delete file {a.get('path', '?')}"
    elif t == "make_payment":
        what = f"pay {a.get('amount', '?')} {a.get('currency', '')} to {a.get('payee_id', '?')}"
    elif t == "fetch_url":
        what = f"fetch {a.get('url', '?')}"
    elif t == "read_source":
        what = f"read source {a.get('source_id', '?')}"
    else:
        what = f"{t} {', '.join(f'{k}={v}' for k, v in list(a.items())[:2])}".strip()
    return verb + what


class EffectService:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.journal = core.journal

    def get(self, project_id: str, operation_id: str) -> dict:
        e = self.db.one("SELECT * FROM effects WHERE project_id=? AND operation_id=?", (project_id, operation_id))
        if not e:
            raise NotFound(f"effect {operation_id}")
        e["error"] = loads(e.pop("error_json"), None)
        e["history"] = self.db.all("SELECT seq, state, detail_json, created_at FROM effect_journal WHERE project_id=? AND operation_id=? ORDER BY seq",
                                   (project_id, operation_id))
        e["invocations"] = self.db.scalar("SELECT COUNT(*) FROM executor_invocations WHERE operation_id=?", (operation_id,))
        return e

    def list(self, project_id: str, run_id: str | None = None, state: str | None = None, agent_id: str | None = None,
             tool_id: str | None = None, effect_class: str | None = None, text: str | None = None,
             include_children: bool = False, external_only: bool = False) -> list[dict]:
        """Effects with the context needed to understand them: which agent and task, and what the (redacted) arguments were."""
        q = ("SELECT e.*, r.agent_id, r.task_id, r.task_revision, r.parent_run_id, r.purpose AS run_purpose, r.status AS run_status, "
             "a.name AS agent_name, x.args_redacted_json, d.outcome, d.reason_code "
             "FROM effects e JOIN runs r ON r.run_id=e.run_id LEFT JOIN agents a ON a.agent_id=r.agent_id "
             "LEFT JOIN decisions d ON d.decision_id=e.decision_id LEFT JOIN actions x ON x.action_id=d.action_id "
             "WHERE e.project_id=?")
        p: list = [project_id]
        if run_id:
            ids = self.run_family(run_id) if include_children else [run_id]
            q += f" AND e.run_id IN ({','.join('?' * len(ids))})"
            p += ids
        for col, val in (("e.state", state), ("r.agent_id", agent_id), ("e.tool_id", tool_id), ("e.effect_class", effect_class)):
            if val:
                q += f" AND {col}=?"
                p.append(val)
        if external_only:
            q += " AND e.effect_class IN ('external_send','payment','delete','write_local')"
        if text:
            like = f"%{text.lower()}%"
            q += (" AND (lower(e.operation_id) LIKE ? OR lower(e.tool_id) LIKE ? OR lower(COALESCE(x.args_redacted_json,'')) LIKE ? "
                  "OR lower(COALESCE(e.external_ref,'')) LIKE ? OR lower(e.run_id) LIKE ? OR lower(r.task_id) LIKE ?)")
            p += [like] * 6
        rows = self.db.all(q + " ORDER BY e.created_at DESC LIMIT 1000", p)
        for r in rows:
            r["error"] = loads(r.pop("error_json"), None)
            r["arguments"] = loads(r.pop("args_redacted_json"), {}) or {}
            r["summary"] = describe_effect(r)
        return rows

    def run_family(self, run_id: str) -> list[str]:
        """A run plus every run it spawned (repairs, continuations, subagents), recursively."""
        out, frontier = [run_id], [run_id]
        while frontier:
            kids = [r["run_id"] for r in self.db.all(
                f"SELECT run_id FROM runs WHERE parent_run_id IN ({','.join('?' * len(frontier))})", frontier)]
            kids = [k for k in kids if k not in out]
            out += kids
            frontier = kids
        return out

    def impact(self, project_id: str, run_id: str, include_children: bool = True) -> dict:
        """Everything a run (and the runs it spawned) did to the outside world, and what can be done about it."""
        run = self.db.one("SELECT r.*, a.name AS agent_name FROM runs r LEFT JOIN agents a ON a.agent_id=r.agent_id WHERE r.run_id=? AND r.project_id=?",
                          (run_id, project_id))
        if not run:
            raise NotFound(f"run {run_id}")
        family = self.run_family(run_id) if include_children else [run_id]
        effects = self.list(project_id, run_id, include_children=include_children)
        marks = ",".join("?" * len(family))
        blocked = self.db.all(f"SELECT d.decision_id, d.run_id, d.tool_id, d.outcome, d.reason_code, d.message, x.args_redacted_json, d.created_at "
                              f"FROM decisions d JOIN actions x ON x.action_id=d.action_id WHERE d.run_id IN ({marks}) AND d.outcome!='ALLOW' "
                              f"ORDER BY d.created_at", family)
        for b in blocked:
            b["arguments"] = loads(b.pop("args_redacted_json"), {}) or {}
        outbox = [e for e in self.core.executors.outbox.entries(project_id) if e.get("run_id") in family]
        payments = [e for e in effects if e["effect_class"] == "payment" and e["state"] == "COMPLETED"]
        files = [e for e in effects if e["tool_id"] in ("write_report", "delete_file") and e["state"] == "COMPLETED"]
        unknown = [e for e in effects if e["state"] in ("UNKNOWN", "DISPATCHING")]
        incidents = self.db.all(f"SELECT incident_id, category, status, attribution_level, created_at FROM incidents WHERE run_id IN ({marks}) ORDER BY created_at",
                                family)
        quarantined = self.db.scalar(f"SELECT COUNT(*) FROM dispositions WHERE run_id IN ({marks}) AND disposition='quarantined'", family)
        branches = sorted({e["branch_id"] for e in effects})
        base = run["base_version"]
        steps = []
        if unknown:
            steps.append({"kind": "reconcile", "text": f"{len(unknown)} action(s) may or may not have happened. Reconcile each one before anything else; "
                          "the run cannot continue until you do.", "operation_ids": [e["operation_id"] for e in unknown]})
        if outbox:
            steps.append({"kind": "contact", "text": f"{len(outbox)} message(s) were delivered. ANTIBODY cannot unsend them; contact these recipients if they "
                          "should not have received them: " + ", ".join(sorted({f"{o['recipient_id']} ({o['address']})" for o in outbox})) + "."})
        if payments:
            steps.append({"kind": "payments", "text": f"{len(payments)} payment(s) completed. Reverse them with the payee; restoring the agent does not refund."})
        if files:
            steps.append({"kind": "files", "text": f"{len(files)} file change(s) happened inside the agent's own workspace. Restore the branch to the version the run "
                          "started from to put the files back.", "base_version": base, "branches": branches})
        if blocked:
            steps.append({"kind": "blocked", "text": f"{len(blocked)} attempt(s) were stopped before they ran. Nothing to undo for these; review the incidents to see why "
                          "the agent tried."})
        if not steps:
            steps.append({"kind": "none", "text": "This run changed nothing outside its own reading. No mitigation needed."})
        return {
            "run": {k: run[k] for k in ("run_id", "agent_id", "agent_name", "task_id", "task_revision", "mode", "status", "status_reason", "health",
                                        "base_version", "branch_id", "parent_run_id", "created_at")},
            "family": family,
            "counts": {"effects": len(effects), "completed": sum(e["state"] == "COMPLETED" for e in effects),
                       "blocked": len(blocked), "unknown": len(unknown), "messages_sent": len(outbox), "payments": len(payments),
                       "file_changes": len(files), "quarantined_spans": quarantined},
            "messages": outbox, "files": files, "payments": payments, "unknown": unknown, "blocked": blocked,
            "incidents": incidents, "mitigation": steps,
            "note": "Effects inside the agent's own workspace can be rolled back with Version history. Effects outside it (messages, payments) are "
                    "permanent and recorded here forever.",
        }

    def reconcile(self, project_id: str, operation_id: str, actor: str) -> dict:
        e = self.db.one("SELECT * FROM effects WHERE project_id=? AND operation_id=?", (project_id, operation_id))
        if not e:
            raise NotFound(f"effect {operation_id}")
        if e["state"] not in ("UNKNOWN", "DISPATCHING"):
            return {"operation_id": operation_id, "state": e["state"], "changed": False}
        if e["state"] == "DISPATCHING" and e["lease_expires"] and e["lease_expires"] > utcnow():
            raise ConflictError("LEASE_ACTIVE", "the effect is still being dispatched")
        res = self.core.executors.reconcile(project_id, e["tool_id"], operation_id)
        if res is None:
            raise ConflictError("NO_RECONCILER", "this tool has no reconciliation function; an explicit operator decision is required")
        now = utcnow()
        with self.db.tx() as c:
            c.execute("UPDATE effects SET state=?, external_ref=COALESCE(?, external_ref), updated_at=? WHERE project_id=? AND operation_id=?",
                      (res["state"], res.get("external_ref"), now, project_id, operation_id))
            c.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                      (project_id, operation_id, res["state"], dumps({"reconciled_by": actor, "evidence": res["evidence"]}), now))
        self.journal.append(project_id, e["run_id"], "effect.reconciled",
                            {"operation_id": operation_id, "from": e["state"], "to": res["state"], "evidence": res["evidence"], "actor": actor},
                            actor_type="reconciler")
        return {"operation_id": operation_id, "state": res["state"], "changed": True, "evidence": res["evidence"]}

    def operator_resolve(self, project_id: str, operation_id: str, state: str, actor: str, note: str) -> dict:
        """Explicit decision for tools without reconciliation support."""
        if state not in ("COMPLETED", "FAILED"):
            raise ConflictError("INVALID_STATE", "resolve to COMPLETED or FAILED")
        e = self.db.one("SELECT * FROM effects WHERE project_id=? AND operation_id=?", (project_id, operation_id))
        if not e or e["state"] != "UNKNOWN":
            raise ConflictError("NOT_UNKNOWN", "only UNKNOWN effects can be resolved explicitly")
        now = utcnow()
        with self.db.tx() as c:
            c.execute("UPDATE effects SET state=?, updated_at=? WHERE project_id=? AND operation_id=?", (state, now, project_id, operation_id))
            c.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                      (project_id, operation_id, state, dumps({"operator_decision": actor, "note": note}), now))
        self.journal.append(project_id, e["run_id"], "effect.reconciled", {"operation_id": operation_id, "from": "UNKNOWN", "to": state,
                                                                          "evidence": f"operator decision: {note}", "actor": actor}, actor_type="operator")
        return {"operation_id": operation_id, "state": state}
