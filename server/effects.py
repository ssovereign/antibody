"""Permanent effect journal queries and tool-specific reconciliation.

The journal is project-level history: restoring an agent version never
truncates or rewinds it. UNKNOWN becomes COMPLETED or FAILED only through a
tool-specific reconciliation function; a model's claim is not reconciliation.
"""
from __future__ import annotations

from .canonical import dumps, loads, utcnow
from .projects import ConflictError, NotFound


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

    def list(self, project_id: str, run_id: str | None = None, state: str | None = None) -> list[dict]:
        q, p = "SELECT * FROM effects WHERE project_id=?", [project_id]
        if run_id:
            q += " AND run_id=?"
            p.append(run_id)
        if state:
            q += " AND state=?"
            p.append(state)
        rows = self.db.all(q + " ORDER BY created_at DESC", p)
        for r in rows:
            r["error"] = loads(r.pop("error_json"), None)
        return rows

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
