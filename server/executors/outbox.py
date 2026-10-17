"""Local outbox and local payment ledger: real, countable test destinations.

Both use the server-issued operation ID as a native idempotency key and
provide a tool-specific reconciliation function for UNKNOWN outcomes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..canonical import content_ref, utcnow
from .base import ExecContext, ToolError, ToolOutcomeUnknown
from .files import atomic_write, resolve_in_workspace


class LocalOutbox:
    def __init__(self, root: str):
        self.root = Path(root)
        self.fault: str | None = None  # "timeout_after_write" | "timeout_before_write" (test hooks)

    def _dir(self, project_id: str) -> Path:
        d = self.root / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _entry(self, project_id: str, operation_id: str) -> Path:
        return self._dir(project_id) / f"{operation_id}.json"

    def send(self, ctx: ExecContext, args: dict, recipient: dict) -> dict:
        entry = self._entry(ctx.project_id, ctx.operation_id)
        if entry.exists():  # native idempotency at the destination
            rec = json.loads(entry.read_text(encoding="utf-8"))
            return {"status": "ok", "operation_id": ctx.operation_id, "deduplicated": True, "outbox_entry": rec["entry_id"]}
        p = resolve_in_workspace(ctx.workspace_dir, args["artifact_path"], ctx.file_roots, must_exist=True)
        body = p.read_bytes()
        if self.fault == "timeout_before_write":
            raise ToolOutcomeUnknown("outbox did not respond")
        rec = {"entry_id": f"out_{ctx.operation_id}", "operation_id": ctx.operation_id, "recipient_id": recipient["recipient_id"],
               "address": recipient["address"], "subject": args.get("subject", ""), "artifact_path": args["artifact_path"],
               "body_sha256": content_ref(body), "body_bytes": len(body), "run_id": ctx.run_id, "sent_at": utcnow()}
        atomic_write(entry, json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))
        if self.fault == "timeout_after_write":
            raise ToolOutcomeUnknown("outbox timed out after accepting the message")
        return {"status": "ok", "operation_id": ctx.operation_id, "outbox_entry": rec["entry_id"], "recipient_id": recipient["recipient_id"]}

    def reconcile(self, project_id: str, operation_id: str) -> dict:
        entry = self._entry(project_id, operation_id)
        if entry.exists():
            rec = json.loads(entry.read_text(encoding="utf-8"))
            return {"state": "COMPLETED", "external_ref": rec["entry_id"], "evidence": "outbox entry present"}
        return {"state": "FAILED", "external_ref": None, "evidence": "no outbox entry for this operation"}

    def entries(self, project_id: str) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self._dir(project_id).glob("*.json"))]

    def count(self, project_id: str, recipient_id: str | None = None, address: str | None = None) -> int:
        es = self.entries(project_id)
        if recipient_id:
            es = [e for e in es if e["recipient_id"] == recipient_id]
        if address:
            es = [e for e in es if e["address"] == address]
        return len(es)


class LocalLedger:
    """Local payment ledger used only for spending-limit tests; it moves no money."""

    def __init__(self, root: str):
        self.root = Path(root)

    def _file(self, project_id: str) -> Path:
        d = self.root / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "ledger.jsonl"

    def pay(self, ctx: ExecContext, args: dict, payee: dict) -> dict:
        f = self._file(ctx.project_id)
        for e in self.entries(ctx.project_id):
            if e["operation_id"] == ctx.operation_id:
                return {"status": "ok", "operation_id": ctx.operation_id, "deduplicated": True}
        rec = {"operation_id": ctx.operation_id, "payee_id": payee["recipient_id"], "amount": args["amount"],
               "currency": args["currency"], "memo": args.get("memo", ""), "at": utcnow(), "simulated_ledger": True}
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return {"status": "ok", "operation_id": ctx.operation_id, "ledger_entry": ctx.operation_id}

    def entries(self, project_id: str) -> list[dict]:
        f = self._file(project_id)
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]

    def reconcile(self, project_id: str, operation_id: str) -> dict:
        for e in self.entries(project_id):
            if e["operation_id"] == operation_id:
                return {"state": "COMPLETED", "external_ref": operation_id, "evidence": "ledger entry present"}
        return {"state": "FAILED", "external_ref": None, "evidence": "no ledger entry"}
