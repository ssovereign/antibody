"""Permanent event journal with per-run sequence numbers.

Events are appended inside the caller's transaction (or a new short one) and
announced to subscribers only after commit (Technical Design: Acceptance
transaction - commit before announcing).
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .canonical import canonical_bytes, dumps, loads, new_id, utcnow
from .schemas import SCHEMA_VERSION

CORE_EVENT_KINDS = {
    "observation.received", "action.proposed", "action.decided", "effect.started",
    "effect.completed", "effect.unknown", "check.finished", "incident.opened",
    "candidate.created", "evaluation.finished", "lesson.promoted", "lesson.revoked",
    "version.saved", "registry.loaded",
}
EXTRA_EVENT_KINDS = {
    "run.started", "run.status", "health.changed", "context.compiled", "model.called",
    "lesson.applied", "source.quarantined", "effect.failed", "effect.reconciled",
    "recovery.started", "recovery.finished", "run.restored", "subagent.handoff",
    "observation.excluded", "registry.refreshed",
    "agent_state.opened", "merge.negotiated", "checkpoint.failed",
}

REDACT_KEYS = {"content", "body", "raw", "secret", "token", "password", "api_key"}


def redact(value: Any, depth: int = 0) -> Any:
    """Redacted display view: long strings truncated, secret-like keys masked."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k.lower() in {"secret", "token", "password", "api_key", "authorization"}:
                out[k] = "[redacted]"
            elif k.lower() in REDACT_KEYS and isinstance(v, str) and len(v) > 160:
                out[k] = v[:160] + f"… [{len(v)} chars]"
            else:
                out[k] = redact(v, depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 600:
        return value[:600] + f"… [{len(value)} chars]"
    return value


class Journal:
    def __init__(self, db, objects):
        self.db = db
        self.objects = objects
        self._cond = threading.Condition()
        self._version = 0
        self._pending: list[dict] = []
        self._listeners: list[Callable[[dict], None]] = []

    def append(self, project_id: str, run_id: str, kind: str, payload: dict, *, actor_type: str,
               evidence_refs: list[str] | None = None, policy_revision: str | None = None,
               registry_revision: int | None = None) -> dict:
        if kind not in CORE_EVENT_KINDS and kind not in EXTRA_EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind}")
        full = {"schema_version": SCHEMA_VERSION, **payload}
        try:
            payload_ref = self.objects.put(project_id, canonical_bytes(_jsonable(full)))
        except Exception:
            payload_ref = None
        with self.db.tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            ev = {
                "event_id": new_id("evt"),
                "project_id": project_id,
                "run_id": run_id,
                "sequence": seq,
                "timestamp_utc": utcnow(),
                "kind": kind,
                "actor_type": actor_type,
                "payload_ref": payload_ref,
                "payload": redact(full),
                "evidence_refs": evidence_refs or [],
                "policy_revision": policy_revision,
                "registry_revision": registry_revision,
                "schema_version": SCHEMA_VERSION,
            }
            c.execute(
                "INSERT INTO events(event_id, project_id, run_id, sequence, timestamp_utc, kind, actor_type, payload_ref, payload_json, evidence_refs_json, policy_revision, registry_revision, schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev["event_id"], project_id, run_id, seq, ev["timestamp_utc"], kind, actor_type, payload_ref,
                 dumps(ev["payload"]), dumps(ev["evidence_refs"]), policy_revision, registry_revision, SCHEMA_VERSION),
            )
        self._notify(ev)
        return ev

    def _notify(self, ev: dict) -> None:
        with self._cond:
            self._version += 1
            self._cond.notify_all()
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                pass

    def wait(self, last_version: int, timeout: float) -> int:
        with self._cond:
            if self._version == last_version:
                self._cond.wait(timeout)
            return self._version

    @property
    def version(self) -> int:
        return self._version

    def listen(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def events(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict]:
        rows = self.db.all("SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?", (run_id, after, limit))
        return [row_to_event(r) for r in rows]

    def project_events(self, project_id: str, kinds: list[str] | None = None, limit: int = 200) -> list[dict]:
        if kinds:
            q = ",".join("?" * len(kinds))
            rows = self.db.all(f"SELECT * FROM events WHERE project_id=? AND kind IN ({q}) ORDER BY timestamp_utc DESC LIMIT ?", (project_id, *kinds, limit))
        else:
            rows = self.db.all("SELECT * FROM events WHERE project_id=? ORDER BY timestamp_utc DESC LIMIT ?", (project_id, limit))
        return [row_to_event(r) for r in rows]


def row_to_event(r: dict) -> dict:
    return {
        "event_id": r["event_id"], "project_id": r["project_id"], "run_id": r["run_id"],
        "sequence": r["sequence"], "timestamp_utc": r["timestamp_utc"], "kind": r["kind"],
        "actor_type": r["actor_type"], "payload_ref": r["payload_ref"], "payload": loads(r["payload_json"], {}),
        "evidence_refs": loads(r["evidence_refs_json"], []), "policy_revision": r["policy_revision"],
        "registry_revision": r["registry_revision"], "schema_version": r["schema_version"],
    }


def _jsonable(v: Any) -> Any:
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    return str(v)
