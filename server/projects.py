"""Projects, service tokens, operator policy, tool registry, recipients, tasks and grants.

The assignment service owns authorization (Technical Design: Architecture and
ownership). Only trusted application actors create task revisions; nothing a
source or model produces reaches these tables.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .canonical import canonical_bytes, dumps, hash_value, loads, new_id, utcnow, utcnow_dt
from .schemas import TaskBody


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ConflictError(Exception):
    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


class NotFound(Exception):
    def __init__(self, what: str):
        super().__init__(f"{what} not found")
        self.message = f"{what} not found"


class InvalidRequest(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ---------------------------------------------------------------- default tool registry
def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


DEFAULT_TOOLS: list[dict] = [
    {"tool_id": "read_source", "effect_class": "read", "executor": "sources",
     "schema": _obj({"source_id": {"type": "string", "maxLength": 128}}, ["source_id"])},
    {"tool_id": "list_files", "effect_class": "read", "executor": "files",
     "schema": _obj({"path": {"type": "string", "maxLength": 512}}, [])},
    {"tool_id": "read_file", "effect_class": "read", "executor": "files",
     "schema": _obj({"path": {"type": "string", "maxLength": 512}}, ["path"])},
    {"tool_id": "fetch_url", "effect_class": "read_network", "executor": "fetch",
     "schema": _obj({"url": {"type": "string", "maxLength": 2048}}, ["url"])},
    {"tool_id": "write_report", "effect_class": "write_local", "executor": "export_fixture_1",
     "schema": _obj({"path": {"type": "string", "maxLength": 512},
                     "content": {"type": "string", "maxLength": 200000},
                     "encoding": {"type": "string", "enum": ["utf-8", "utf-16", "cp1252"]}}, ["path", "content"]),
     "learnable_params": {"encoding": ["utf-8", "utf-16", "cp1252"]}},
    {"tool_id": "send_report", "effect_class": "external_send", "executor": "outbox", "reconcile": True,
     "schema": _obj({"recipient_id": {"type": "string", "maxLength": 256},
                     "artifact_path": {"type": "string", "maxLength": 512},
                     "subject": {"type": "string", "maxLength": 200}}, ["recipient_id", "artifact_path"])},
    {"tool_id": "delete_file", "effect_class": "delete", "executor": "files",
     "schema": _obj({"path": {"type": "string", "maxLength": 512}}, ["path"])},
    {"tool_id": "make_payment", "effect_class": "payment", "executor": "ledger", "reconcile": True,
     "schema": _obj({"payee_id": {"type": "string", "maxLength": 128},
                     "amount": {"type": "string", "maxLength": 32},
                     "currency": {"type": "string", "maxLength": 3},
                     "memo": {"type": "string", "maxLength": 200}}, ["payee_id", "amount", "currency"])},
]

DEFAULT_POLICY: dict = {
    "schema_version": 1,
    "max_argument_bytes": 262144,
    "high_impact_classes": ["external_send", "payment", "delete"],
    "grant_required_classes": ["payment", "delete"],
    "mutating_classes": ["write_local", "external_send", "payment", "delete"],
    "read_only_safe_fallback": True,
    "audit": {"enabled": True, "audit_untrusted_anchors": True},
    "outbound_requires_checked_artifact": True,
    "canary_markers": ["CANARY-"],
    "hard_denied_tools": [],
    "decision_ttl_seconds": 120,
}

DEFAULT_RECIPIENTS = [
    {"recipient_id": "finance_team", "address": "finance@corp.example", "kind": "local_outbox"},
    {"recipient_id": "ops_team", "address": "ops@corp.example", "kind": "local_outbox"},
]
DEFAULT_PAYEES = [{"recipient_id": "payee_cloud_vendor", "address": "billing@vendor.example", "kind": "payee"}]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ProjectService:
    def __init__(self, db, journal):
        self.db = db
        self.journal = journal

    # ----------------------------------------------------------- projects
    def create_project(self, project_id: str, name: str, owner: str) -> dict:
        if self.db.one("SELECT 1 FROM projects WHERE project_id=?", (project_id,)):
            raise ConflictError("PROJECT_EXISTS", f"project {project_id} exists")
        now = utcnow()
        with self.db.tx() as c:
            c.execute("INSERT INTO projects(project_id,name,owner,policy_head,registry_head,settings_json,created_at) VALUES (?,?,?,?,?,?,?)",
                      (project_id, name, owner, "policy_1", 0, dumps({"auto_promote_enabled": False, "auto_promote_classes": []}), now))
            body = dict(DEFAULT_POLICY, revision="policy_1")
            c.execute("INSERT INTO policies VALUES (?,?,?,?,?,?)",
                      (project_id, "policy_1", dumps(body), hash_value(body), owner, now))
            for t in DEFAULT_TOOLS:
                c.execute("INSERT INTO tools(project_id,tool_id,schema_json,schema_hash,effect_class,executor,learnable_params_json,reconcile) VALUES (?,?,?,?,?,?,?,?)",
                          (project_id, t["tool_id"], dumps(t["schema"]), hash_value(t["schema"]), t["effect_class"], t["executor"],
                           dumps(t.get("learnable_params", {})), 1 if t.get("reconcile") else 0))
            for r in DEFAULT_RECIPIENTS + DEFAULT_PAYEES:
                c.execute("INSERT INTO recipients VALUES (?,?,?,?)", (project_id, r["recipient_id"], r["address"], r["kind"]))
            c.execute("INSERT INTO registry_revisions VALUES (?,?,?,?,?)", (project_id, 0, dumps([{"op": "init"}]), "system", now))
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict:
        p = self.db.one("SELECT * FROM projects WHERE project_id=?", (project_id,))
        if not p:
            raise NotFound(f"project {project_id}")
        p["settings"] = loads(p.pop("settings_json"), {})
        return p

    def list_projects(self) -> list[dict]:
        return [self.get_project(r["project_id"]) for r in self.db.all("SELECT project_id FROM projects ORDER BY created_at")]

    def update_settings(self, project_id: str, changes: dict, actor: str) -> dict:
        p = self.get_project(project_id)
        allowed = {"auto_promote_enabled", "auto_promote_classes", "registry_refresh"}
        bad = set(changes) - allowed
        if bad:
            raise InvalidRequest("SETTING_NOT_EDITABLE", f"not editable: {sorted(bad)}")
        s = dict(p["settings"], **changes)
        self.db.execute("UPDATE projects SET settings_json=? WHERE project_id=?", (dumps(s), project_id))
        return self.get_project(project_id)

    # ----------------------------------------------------------- tokens
    def issue_token(self, project_id: str | None, role: str, actor_id: str) -> str:
        token = f"ab_{role}_{secrets.token_urlsafe(24)}"
        self.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,0)", (hash_token(token), project_id, role, actor_id, utcnow()))
        return token

    def authenticate(self, token: str | None) -> dict:
        if not token:
            raise AuthError(401, "AUTH_REQUIRED", "A bearer token is required.")
        row = self.db.one("SELECT * FROM tokens WHERE token_hash=? AND revoked=0", (hash_token(token),))
        if not row:
            raise AuthError(401, "AUTH_INVALID", "The token is not valid.")
        return row

    @staticmethod
    def authorize(principal: dict, project_id: str | None, roles: set[str]) -> None:
        if principal["role"] == "admin":
            return
        if principal["role"] not in roles:
            raise AuthError(403, "ROLE_FORBIDDEN", f"Role {principal['role']} cannot perform this operation.")
        if project_id is not None and principal["project_id"] != project_id:
            raise AuthError(403, "PROJECT_FORBIDDEN", "This token is not scoped to that project.")

    # ----------------------------------------------------------- policy & tools
    def policy(self, project_id: str, revision: str | None = None) -> dict:
        if revision is None:
            revision = self.get_project(project_id)["policy_head"]
        row = self.db.one("SELECT * FROM policies WHERE project_id=? AND revision=?", (project_id, revision))
        if not row:
            raise NotFound(f"policy {revision}")
        body = loads(row["body_json"])
        body["revision"] = revision
        body["body_hash"] = row["body_hash"]
        return body

    def update_policy(self, project_id: str, changes: dict, actor: str, expected_head: str) -> dict:
        """Operator-only policy revision (never reachable from the learner)."""
        p = self.get_project(project_id)
        if p["policy_head"] != expected_head:
            raise ConflictError("STALE_POLICY", "policy head changed")
        cur = self.policy(project_id)
        n = int(cur["revision"].split("_")[1]) + 1
        rev = f"policy_{n}"
        body = {k: v for k, v in cur.items() if k not in ("revision", "body_hash")}
        body.update(changes)
        body["revision"] = rev
        with self.db.tx() as c:
            c.execute("INSERT INTO policies VALUES (?,?,?,?,?,?)", (project_id, rev, dumps(body), hash_value(body), actor, utcnow()))
            upd = c.execute("UPDATE projects SET policy_head=? WHERE project_id=? AND policy_head=?", (rev, project_id, expected_head))
            if upd.rowcount != 1:
                raise ConflictError("STALE_POLICY", "policy head changed")
        return self.policy(project_id)

    def tools(self, project_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM tools WHERE project_id=? ORDER BY tool_id", (project_id,))
        for r in rows:
            r["schema"] = loads(r.pop("schema_json"))
            r["learnable_params"] = loads(r.pop("learnable_params_json"), {})
            r["meta"] = loads(r.pop("meta_json", "{}"), {})
        return rows

    def tool(self, project_id: str, tool_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM tools WHERE project_id=? AND tool_id=? AND enabled=1", (project_id, tool_id))
        if r:
            r["schema"] = loads(r.pop("schema_json"))
            r["learnable_params"] = loads(r.pop("learnable_params_json"), {})
            r["meta"] = loads(r.pop("meta_json", "{}"), {})
        return r

    def register_tool(self, project_id: str, tool_id: str, schema: dict, effect_class: str, executor: str,
                      server_id: str | None = None, reconcile: bool = False, meta: dict | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tools(project_id,tool_id,schema_json,schema_hash,effect_class,executor,server_id,learnable_params_json,reconcile,enabled,meta_json) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
            (project_id, tool_id, dumps(schema), hash_value(schema), effect_class, executor, server_id, "{}", 1 if reconcile else 0, dumps(meta or {})))

    def recipients(self, project_id: str) -> list[dict]:
        return self.db.all("SELECT recipient_id, address, kind FROM recipients WHERE project_id=? ORDER BY recipient_id", (project_id,))

    def recipient(self, project_id: str, recipient_id: str) -> dict | None:
        return self.db.one("SELECT * FROM recipients WHERE project_id=? AND recipient_id=?", (project_id, recipient_id))

    def add_recipient(self, project_id: str, recipient_id: str, address: str, kind: str = "local_outbox") -> None:
        self.db.execute("INSERT OR REPLACE INTO recipients VALUES (?,?,?,?)", (project_id, recipient_id, address, kind))

    # ----------------------------------------------------------- tasks
    def create_task_revision(self, project_id: str, body: TaskBody, actor: dict, expected_revision: int | None = None) -> dict:
        if actor["role"] not in ("app", "admin"):
            raise AuthError(403, "UNTRUSTED_ACTOR", "Only trusted application actors create task revisions.")
        self.get_project(project_id)
        known_tools = {t["tool_id"] for t in self.tools(project_id)}
        unknown = [t for t in body.tools if t not in known_tools]
        if unknown:
            raise InvalidRequest("UNKNOWN_TOOL", f"unknown tools: {unknown}")
        known_rcpt = {r["recipient_id"] for r in self.recipients(project_id)}
        bad = [r for r in body.recipient_ids + body.write_network.destination_ids if r not in known_rcpt]
        if bad:
            raise InvalidRequest("UNKNOWN_RECIPIENT", f"recipients must be server-owned records: {bad}")
        for root in body.file_roots:
            if not root.startswith("workspace://branch/current"):
                raise InvalidRequest("INVALID_ROOT", "file roots must be inside workspace://branch/current")
            if ".." in root.split("/"):
                raise InvalidRequest("INVALID_ROOT", "file roots may not contain '..'")
        payload = body.model_dump()
        with self.db.tx() as c:
            head = c.execute("SELECT current_revision FROM task_heads WHERE project_id=? AND task_id=?", (project_id, body.task_id)).fetchone()
            cur = head[0] if head else 0
            if expected_revision is not None and expected_revision != cur:
                raise ConflictError("STALE_REVISION", f"task {body.task_id} is at revision {cur}")
            if cur:
                prev = c.execute("SELECT body_hash FROM tasks WHERE project_id=? AND task_id=? AND revision=?", (project_id, body.task_id, cur)).fetchone()[0]
                if prev == hash_value(payload):
                    return self.get_task(project_id, body.task_id, cur)
            rev = cur + 1
            c.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                      (project_id, body.task_id, rev, body.goal, dumps(payload), hash_value(payload), actor["actor_id"], utcnow()))
            c.execute("INSERT OR REPLACE INTO task_heads VALUES (?,?,?)", (project_id, body.task_id, rev))
        return self.get_task(project_id, body.task_id, rev)

    def get_task(self, project_id: str, task_id: str, revision: int | None = None) -> dict:
        if revision is None:
            revision = self.current_task_revision(project_id, task_id)
        row = self.db.one("SELECT * FROM tasks WHERE project_id=? AND task_id=? AND revision=?", (project_id, task_id, revision))
        if not row:
            raise NotFound(f"task {task_id}@{revision}")
        body = loads(row["body_json"])
        return {"task_id": task_id, "revision": revision, "goal": row["goal"], "body": body,
                "body_hash": row["body_hash"], "created_by": row["created_by"], "created_at": row["created_at"],
                "current_revision": self.current_task_revision(project_id, task_id)}

    def current_task_revision(self, project_id: str, task_id: str) -> int:
        r = self.db.scalar("SELECT current_revision FROM task_heads WHERE project_id=? AND task_id=?", (project_id, task_id))
        if r is None:
            raise NotFound(f"task {task_id}")
        return r

    def list_tasks(self, project_id: str) -> list[dict]:
        heads = self.db.all("SELECT task_id, current_revision FROM task_heads WHERE project_id=? ORDER BY task_id", (project_id,))
        return [self.get_task(project_id, h["task_id"], h["current_revision"]) for h in heads]

    def task_revisions(self, project_id: str, task_id: str) -> list[dict]:
        rows = self.db.all("SELECT revision FROM tasks WHERE project_id=? AND task_id=? ORDER BY revision", (project_id, task_id))
        return [self.get_task(project_id, task_id, r["revision"]) for r in rows]

    @staticmethod
    def parse_task_ref(ref: str) -> tuple[str, int | None]:
        if "@" in ref:
            tid, rev = ref.rsplit("@", 1)
            return tid, int(rev)
        return ref, None

    def suggest_task_draft(self, goal: str) -> dict:
        """A non-authoritative draft contract suggested from the goal text.

        It is returned to the user for editing; it becomes authority only when
        the user submits it through create_task_revision.
        """
        g = goal.lower()
        tools = ["read_source"]
        if any(w in g for w in ("report", "summar", "write", "save")):
            tools.append("write_report")
        if any(w in g for w in ("send", "email", "share", "deliver")):
            tools.append("send_report")
        if "fetch" in g or "http" in g or "web" in g:
            tools.append("fetch_url")
        return {"draft": True, "authoritative": False, "goal": goal, "tools": tools, "recipient_ids": [],
                "note": "Draft only. Select recipients, roots and limits explicitly before saving."}

    # ----------------------------------------------------------- grants
    def create_grant(self, project_id: str, actor: dict, tool_id: str, arguments: dict | None, operation_class: str | None,
                     resource_scope: dict, expires_in_seconds: int, uses: int) -> dict:
        if actor["role"] not in ("app", "admin"):
            raise AuthError(403, "UNTRUSTED_ACTOR", "Only trusted application actors create grants.")
        if arguments is None and not operation_class:
            raise InvalidRequest("GRANT_TOO_BROAD", "a grant binds exact arguments or a narrow operation class")
        gid = new_id("grant")
        args_hash = hash_value(arguments) if arguments is not None else None
        exp = (utcnow_dt() + timedelta(seconds=expires_in_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.db.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (gid, project_id, tool_id, args_hash, operation_class, dumps(resource_scope), exp, uses, actor["actor_id"], utcnow()))
        return self.db.one("SELECT * FROM grants WHERE grant_id=?", (gid,))


def decimal_le(a: str, b: str) -> bool:
    return Decimal(a) <= Decimal(b)
