"""Health monitor: observable signals -> explicit states with recorded reasons.

States: Healthy, Degraded, Paused, Quarantined, Recovering, Unknown.
Every transition is persisted with a reason code (Technical Design: Health
signals and state transitions). Thresholds come from the run's immutable
start configuration.
"""
from __future__ import annotations

from ..canonical import dumps, hash_value, loads, new_id, utcnow

ALLOWED = {
    ("Healthy", "Degraded"), ("Healthy", "Paused"), ("Healthy", "Unknown"), ("Healthy", "Quarantined"),
    ("Degraded", "Recovering"), ("Degraded", "Paused"), ("Degraded", "Healthy"), ("Degraded", "Quarantined"), ("Degraded", "Unknown"),
    ("Paused", "Quarantined"), ("Paused", "Healthy"), ("Paused", "Recovering"),
    ("Quarantined", "Recovering"), ("Quarantined", "Paused"),
    ("Recovering", "Healthy"), ("Recovering", "Paused"), ("Recovering", "Unknown"), ("Recovering", "Degraded"),
    ("Unknown", "Paused"),
}

PHASE_OF_TOOL = {"read_source": "gather", "fetch_url": "gather", "read_file": "gather", "list_files": "gather",
                 "write_report": "produce", "send_report": "deliver", "delete_file": "maintain", "make_payment": "pay"}


class Incidents:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.journal = core.journal

    def open(self, run: dict, category: str, severity: str, observed: list[dict], evidence_refs: list[str],
             hypotheses: list[dict], fingerprint: str | None = None, attribution_level: str = "observed") -> dict:
        iid = new_id("incident")
        self.db.execute("INSERT INTO incidents(incident_id,project_id,run_id,category,severity,status,fingerprint,observed_json,evidence_refs_json,hypotheses_json,attribution_level,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (iid, run["project_id"], run["run_id"], category, severity, "open", fingerprint, dumps(observed),
                         dumps(evidence_refs), dumps(hypotheses), attribution_level, utcnow()))
        self.journal.append(run["project_id"], run["run_id"], "incident.opened",
                            {"incident_id": iid, "category": category, "severity": severity, "observed": observed,
                             "hypotheses": hypotheses, "attribution_level": attribution_level},
                            actor_type="health_monitor", evidence_refs=evidence_refs)
        return self.get(iid)

    def get(self, incident_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM incidents WHERE incident_id=?", (incident_id,))
        if not r:
            return None
        r["observed"] = loads(r.pop("observed_json"), [])
        r["evidence_refs"] = loads(r.pop("evidence_refs_json"), [])
        r["hypotheses"] = loads(r.pop("hypotheses_json"), [])
        return r

    def list(self, project_id: str, status: str | None = None) -> list[dict]:
        q = "SELECT incident_id FROM incidents WHERE project_id=?" + (" AND status=?" if status else "") + " ORDER BY created_at DESC"
        return [self.get(r["incident_id"]) for r in self.db.all(q, (project_id, status) if status else (project_id,))]

    def update(self, incident_id: str, **fields) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            col = {"hypotheses": "hypotheses_json"}.get(k, k)
            sets.append(f"{col}=?")
            vals.append(dumps(v) if k == "hypotheses" else v)
        self.db.execute(f"UPDATE incidents SET {', '.join(sets)} WHERE incident_id=?", (*vals, incident_id))


class HealthMonitor:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.journal = core.journal

    # ------------------------------------------------------------ transitions
    def state(self, run_id: str) -> str:
        return self.db.scalar("SELECT health FROM runs WHERE run_id=?", (run_id,))

    def transition(self, run_id: str, to: str, reason: str, detail: dict | None = None) -> bool:
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        frm = run["health"]
        if frm == to:
            return False
        if (frm, to) not in ALLOWED:
            # route through Paused when a direct edge is not part of the state machine
            if (frm, "Paused") in ALLOWED and ("Paused", to) in ALLOWED:
                self.transition(run_id, "Paused", reason, detail)
                frm = "Paused"
            else:
                raise ValueError(f"health transition {frm}->{to} not allowed")
        with self.db.tx() as c:
            c.execute("UPDATE runs SET health=?, updated_at=? WHERE run_id=?", (to, utcnow(), run_id))
            c.execute("INSERT INTO health_transitions(run_id,from_state,to_state,reason_code,detail_json,created_at) VALUES (?,?,?,?,?,?)",
                      (run_id, frm, to, reason, dumps(detail or {}), utcnow()))
        self.journal.append(run["project_id"], run_id, "health.changed", {"from": frm, "to": to, "reason_code": reason, "detail": detail or {}},
                            actor_type="health_monitor")
        return True

    def transitions(self, run_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM health_transitions WHERE run_id=? ORDER BY id", (run_id,))
        for r in rows:
            r["detail"] = loads(r.pop("detail_json"), {})
        return rows

    def limits(self, run: dict) -> dict:
        return loads(run["start_config_json"])["limits"]

    # ------------------------------------------------------------ signals
    def on_decision(self, run: dict, view) -> None:
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (run["run_id"],))
        if view.outcome == "ALLOW":
            return
        if view.reason_code == "DEDUPLICATED":
            return
        if view.outcome == "PAUSE":
            if view.reason_code == "OUTCOME_UNKNOWN":
                if run["health"] != "Unknown":
                    self.transition(run["run_id"], "Unknown", "OUTCOME_UNKNOWN", {"operation_id": view.operation_id})
                self.transition(run["run_id"], "Paused", "RECONCILIATION_REQUIRED", {"operation_id": view.operation_id})
            else:
                self.transition(run["run_id"], "Paused", view.reason_code, {"decision_id": view.decision_id, "message": view.message})
            self.core.coordinator.set_status(run["run_id"], "paused", view.reason_code)
            return
        # DENY: an authorization violation. The action never executes.
        anchored = [r for r in view.evidence_refs if ":chunk_" in r]
        observed = [{"fact": "action denied before execution", "decision_id": view.decision_id, "reason_code": view.reason_code,
                     "executor_invoked": False}]
        hyps = []
        if anchored:
            hyps.append({"status": "suspected", "text": "Content in the linked source may have influenced the action arguments. Exposure and argument overlap are recorded; causal role is not verified.",
                         "evidence_refs": anchored})
        inc = self.core.incidents.open(run, "blocked_action", "high" if anchored else "medium", observed,
                                       [f"decision:{view.decision_id}"] + anchored, hyps,
                                       fingerprint=hash_value({"deny": view.reason_code, "decision": view.decision_id}),
                                       attribution_level="hypothesized" if anchored else "observed")
        if anchored and run["health"] in ("Healthy", "Degraded", "Recovering"):
            self.transition(run["run_id"], "Paused", "AUTHORIZATION_VIOLATION", {"decision_id": view.decision_id})
            self.core.coordinator.context_repair(run, inc, anchored)
        else:
            if run["health"] == "Healthy":
                self.transition(run["run_id"], "Degraded", "ACTION_DENIED",
                                {"decision_id": view.decision_id, "note": "Continuing independent authorized work."})
        self.core.learning.on_incident(inc)

    def on_effect(self, run: dict, effect: dict, result: dict | None, error: dict | None) -> None:
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (run["run_id"],))
        if effect["state"] == "UNKNOWN":
            self.transition(run["run_id"], "Unknown", "OUTCOME_UNKNOWN", {"operation_id": effect["operation_id"]})
            self.transition(run["run_id"], "Paused", "RECONCILIATION_REQUIRED", {"operation_id": effect["operation_id"]})
            self.core.coordinator.set_status(run["run_id"], "paused", "OUTCOME_UNKNOWN")
            self.core.incidents.open(run, "unknown_effect", "high",
                                     [{"fact": "The tool may have completed before a timeout", "operation_id": effect["operation_id"]}],
                                     [f"effect:{effect['operation_id']}"], [], attribution_level="observed")
            return
        if effect["state"] != "FAILED" or not error:
            return
        branch = self.db.one("SELECT state_json FROM branches WHERE branch_id=?", (run["branch_id"],))
        env = loads(branch["state_json"])["environment"]
        fp_parts = {"tool_class": effect["tool_id"], "error_family": error.get("family"),
                    "environment": env.get("export_adapter"), "phase": PHASE_OF_TOOL.get(effect["tool_id"], "other")}
        fp = hash_value(fp_parts)
        ev = self.db.scalar("SELECT event_id FROM events WHERE run_id=? AND kind='effect.failed' ORDER BY sequence DESC LIMIT 1", (run["run_id"],))
        self.db.execute("INSERT INTO failures(project_id,run_id,fingerprint,detail_json,event_ref,created_at) VALUES (?,?,?,?,?,?)",
                        (run["project_id"], run["run_id"], fp, dumps({**fp_parts, "code": error.get("code"), "operation_id": effect["operation_id"]}), ev, utcnow()))
        n = self.db.scalar("SELECT COUNT(*) FROM failures WHERE run_id=? AND fingerprint=?", (run["run_id"], fp))
        limit = self.limits(run)["repeat_failure_limit"]
        if n < limit:
            return
        if self.db.scalar("SELECT 1 FROM incidents WHERE run_id=? AND fingerprint=? AND category='execution_failure'", (run["run_id"], fp)):
            self.transition(run["run_id"], "Paused", "REPEAT_FAILURE_LIMIT", {"fingerprint": fp})
            self.core.coordinator.set_status(run["run_id"], "paused", "REPEAT_FAILURE_LIMIT")
            return
        events = [r["event_ref"] for r in self.db.all("SELECT event_ref FROM failures WHERE run_id=? AND fingerprint=?", (run["run_id"], fp)) if r["event_ref"]]
        observed = [{"fact": f"{n} equivalent failures", "error_family": error.get("family"), "code": error.get("code"),
                     "tool_id": effect["tool_id"], "environment": fp_parts["environment"], "phase": fp_parts["phase"]},
                    {"fact": "artifact check failed", "detail": "no artifact was produced"}]
        if run["health"] in ("Healthy",):
            self.transition(run["run_id"], "Degraded", "REPEAT_FAILURE", {"fingerprint": fp, "count": n})
        inc = self.core.incidents.open(run, "execution_failure", "medium", observed, [f"event:{e}" for e in events], [],
                                       fingerprint=fp, attribution_level="observed")
        cand = self.core.learning.on_incident(inc)
        self.core.coordinator.request_recovery(run["run_id"], inc, cand)

    def on_step(self, run_id: str, progressed: bool) -> None:
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        cfg = loads(run["start_config_json"])
        st = cfg.setdefault("_progress", {})
        if progressed:
            if run["health"] in ("Degraded", "Recovering"):
                self.transition(run_id, "Healthy", "INDEPENDENT_CHECKS_PASS", {})
            return
        stall = self.db.scalar("SELECT COUNT(*) FROM events WHERE run_id=? AND kind='check.finished' AND payload_json LIKE '%\"progress\": false%' AND sequence > COALESCE((SELECT MAX(sequence) FROM events WHERE run_id=? AND kind='check.finished' AND payload_json LIKE '%\"progress\": true%'),0)", (run_id, run_id))
        if stall >= self.limits(run)["no_progress_limit"]:
            if run["health"] == "Healthy":
                self.transition(run_id, "Degraded", "NO_PROGRESS", {"steps_without_progress": stall})
            self.transition(run_id, "Paused", "NO_PROGRESS", {"steps_without_progress": stall})
            self.core.coordinator.set_status(run_id, "paused", "NO_PROGRESS")
