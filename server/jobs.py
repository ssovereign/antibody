"""Single durable job coordinator with leased workers.

A worker atomically claims a queued job (or one whose lease expired) with
BEGIN IMMEDIATE; completion records the claim revision so a stale worker
cannot overwrite a replacement's result.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import timedelta

from .canonical import dumps, loads, new_id, utcnow, utcnow_dt


class JobQueue:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.handlers = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()

    def register(self, kind: str, fn) -> None:
        self.handlers[kind] = fn

    def enqueue(self, project_id: str, kind: str, payload: dict) -> str:
        jid = new_id("job")
        now = utcnow()
        self.db.execute("INSERT INTO jobs(job_id,project_id,kind,status,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                        (jid, project_id, kind, "queued", dumps(payload), now, now))
        self._wake.set()
        return jid

    def get(self, job_id: str) -> dict | None:
        j = self.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if j:
            j["payload"] = loads(j.pop("payload_json"))
            j["result"] = loads(j.pop("result_json"), None)
            j["error"] = loads(j.pop("error_json"), None)
        return j

    def list(self, project_id: str, limit: int = 50) -> list[dict]:
        return [self.get(r["job_id"]) for r in self.db.all("SELECT job_id FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))]

    def claim(self, worker: str) -> dict | None:
        now = utcnow()
        lease = (utcnow_dt() + timedelta(seconds=self.core.config.JOB_LEASE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self.db.tx() as c:
            row = c.execute("SELECT job_id, claim_revision FROM jobs WHERE status='queued' OR (status='running' AND lease_expires < ?) ORDER BY created_at LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running', lease_owner=?, lease_expires=?, claim_revision=?, updated_at=? WHERE job_id=? AND claim_revision=?",
                      (worker, lease, row["claim_revision"] + 1, now, row["job_id"], row["claim_revision"]))
        return self.get(row["job_id"])

    def complete(self, job: dict, ok: bool, result=None, error=None) -> bool:
        with self.db.tx() as c:
            upd = c.execute("UPDATE jobs SET status=?, result_json=?, error_json=?, updated_at=? WHERE job_id=? AND claim_revision=? AND status='running'",
                            ("succeeded" if ok else "failed", dumps(result) if result is not None else None,
                             dumps(error) if error is not None else None, utcnow(), job["job_id"], job["claim_revision"]))
            return upd.rowcount == 1

    def cancel(self, job_id: str) -> None:
        self.db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE job_id=? AND status='queued'", (utcnow(), job_id))

    def run_one(self, worker: str) -> bool:
        job = self.claim(worker)
        if not job:
            return False
        fn = self.handlers.get(job["kind"])
        try:
            if fn is None:
                raise RuntimeError(f"no handler for {job['kind']}")
            res = fn(job["payload"])
            self.complete(job, True, res)
        except Exception as e:
            self.complete(job, False, error={"type": type(e).__name__, "message": str(e)[:500], "trace": traceback.format_exc(limit=4)[-1500:]})
        return True

    def drain(self, worker: str = "inline") -> int:
        n = 0
        while self.run_one(worker):
            n += 1
        return n

    def start(self, n: int) -> None:
        for i in range(n):
            t = threading.Thread(target=self._loop, args=(f"worker-{i}",), daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self, worker: str) -> None:
        while not self._stop.is_set():
            try:
                did = self.run_one(worker)
            except Exception:
                did = False
            if not did:
                self._wake.wait(0.5)
                self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=5)
