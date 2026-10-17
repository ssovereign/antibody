"""Project knowledge registry: monotonic revisions, retrieval of accepted
records at a pinned revision, authoritative global revocation, and run
acknowledgements.

Only the publication service (learning.promote / revoke) calls `publish`.
"""
from __future__ import annotations

from ..canonical import dumps, loads, utcnow
from ..projects import ConflictError


class Registry:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.journal = core.journal

    def head(self, project_id: str) -> int:
        return self.db.scalar("SELECT registry_head FROM projects WHERE project_id=?", (project_id,)) or 0

    def publish(self, c, project_id: str, expected: int, changes: list[dict], publisher: str) -> int:
        """Must be called inside the caller's transaction (connection `c`)."""
        cur = c.execute("SELECT registry_head FROM projects WHERE project_id=?", (project_id,)).fetchone()[0]
        if cur != expected:
            raise ConflictError("STALE_REGISTRY", f"registry head is {cur}, expected {expected}")
        rev = cur + 1
        upd = c.execute("UPDATE projects SET registry_head=? WHERE project_id=? AND registry_head=?", (rev, project_id, cur))
        if upd.rowcount != 1:
            raise ConflictError("STALE_REGISTRY", "registry head changed concurrently")
        c.execute("INSERT INTO registry_revisions VALUES (?,?,?,?,?)", (project_id, rev, dumps(changes), publisher, utcnow()))
        return rev

    def is_revoked(self, project_id: str, lesson_id: str, revision: int | None = None) -> bool:
        if revision is None:
            return bool(self.db.scalar("SELECT 1 FROM revocations WHERE project_id=? AND lesson_id=? AND revision IS NULL", (project_id, lesson_id)))
        return bool(self.db.scalar("SELECT 1 FROM revocations WHERE project_id=? AND lesson_id=? AND (revision IS NULL OR revision=?)",
                                   (project_id, lesson_id, revision)))

    def revoked_refs(self, project_id: str) -> set[str]:
        out = set()
        for r in self.db.all("SELECT lesson_id, revision FROM revocations WHERE project_id=?", (project_id,)):
            out.add(f"{r['lesson_id']}@*" if r["revision"] is None else f"{r['lesson_id']}@{r['revision']}")
        return out

    def ref_revoked(self, project_id: str, ref: str) -> bool:
        lid, rev = ref.split("@")
        return self.is_revoked(project_id, lid, int(rev))

    def lesson(self, project_id: str, lesson_id: str, revision: int) -> dict | None:
        r = self.db.one("SELECT * FROM lessons WHERE project_id=? AND lesson_id=? AND revision=?", (project_id, lesson_id, revision))
        return _lesson(r) if r else None

    def active(self, project_id: str, at_revision: int | None = None, kind: str | None = None) -> list[dict]:
        """Accepted lesson revisions active at a registry revision, minus current revocations."""
        if at_revision is None:
            at_revision = self.head(project_id)
        rows = self.db.all("SELECT * FROM lessons WHERE project_id=? AND registry_revision<=?" + (" AND kind=?" if kind else "") +
                           " ORDER BY lesson_id, revision", (project_id, at_revision, kind) if kind else (project_id, at_revision))
        latest: dict[str, dict] = {}
        for r in rows:
            latest[r["lesson_id"]] = r  # highest revision accepted by at_revision supersedes earlier ones
        out = []
        now = utcnow()
        for r in latest.values():
            if self.is_revoked(project_id, r["lesson_id"], r["revision"]):
                continue
            l = _lesson(r)
            if l["body"].get("expires_at") and l["body"]["expires_at"] <= now:
                continue  # expired records stop being retrieved; revalidate to renew
            out.append(l)
        return out

    def all_lessons(self, project_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM lessons WHERE project_id=? ORDER BY lesson_id, revision", (project_id,))
        out = []
        for r in rows:
            l = _lesson(r)
            l["revoked"] = self.is_revoked(project_id, r["lesson_id"], r["revision"])
            l["loaded_by"] = self.db.all("SELECT DISTINCT a.run_id, a.agent_id, a.revision FROM registry_acks a WHERE a.project_id=? AND a.revision>=?",
                                         (project_id, r["registry_revision"]))
            l["uses"] = self.db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE project_id=? AND lesson_ref=?", (project_id, f"{r['lesson_id']}@{r['revision']}"))
            out.append(l)
        return out

    def changes(self, project_id: str, since: int = 0) -> list[dict]:
        rows = self.db.all("SELECT * FROM registry_revisions WHERE project_id=? AND revision>? ORDER BY revision", (project_id, since))
        return [{"revision": r["revision"], "changes": loads(r["changes_json"]), "publisher": r["publisher"], "created_at": r["created_at"]} for r in rows]

    def snapshot(self, project_id: str, since: int = 0) -> dict:
        head = self.head(project_id)
        return {"project_id": project_id, "revision": head, "records": self.active(project_id, head),
                "revocations": sorted(self.revoked_refs(project_id)), "changes": self.changes(project_id, since)}

    def ack(self, run: dict, revision: int, boundary: str) -> None:
        with self.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO registry_acks VALUES (?,?,?,?,?,?)",
                      (run["run_id"], run["agent_id"], run["project_id"], revision, boundary, utcnow()))
        self.journal.append(run["project_id"], run["run_id"], "registry.loaded",
                            {"revision": revision, "agent_id": run["agent_id"], "boundary": boundary,
                             "records": [f"{l['lesson_id']}@{l['revision']}" for l in self.active(run["project_id"], revision)]},
                            actor_type="runtime", registry_revision=revision)

    def acks(self, project_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM registry_acks WHERE project_id=? ORDER BY acked_at DESC", (project_id,))


def _lesson(r: dict) -> dict:
    body = loads(r["body_json"])
    return {"lesson_id": r["lesson_id"], "revision": r["revision"], "kind": r["kind"], "status": r["status"],
            "ref": f"{r['lesson_id']}@{r['revision']}", "body": body, "body_hash": r["body_hash"],
            "candidate_id": r["candidate_id"], "candidate_revision": r["candidate_revision"],
            "evaluation_id": r["evaluation_id"], "acceptance_mode": r["acceptance_mode"],
            "registry_revision": r["registry_revision"], "accepted_by": r["accepted_by"], "created_at": r["created_at"],
            "scope_tool": r["scope_tool"]}
