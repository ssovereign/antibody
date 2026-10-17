"""Hosted mode for the Claude Code guard.

The server cannot see a user's disk. Their connector (scripts/antibody_connector.py, also used by
the hook script) sends snapshots of a guarded folder: content hashes of the project files, of
Claude Code's memory for that folder, and of the conversation (as fixed-size parts). The server
asks for any content it does not have yet, then works from those snapshots exactly like the local
guard works from the disk. Anything that must change on the user's computer (going back to a
saved state, switching branches, a merge result, a resumable session file) becomes a queued
operation the connector applies: in the same hook response, or on its next poll.
"""
from __future__ import annotations

import base64
import ntpath
import posixpath
import re

from .canonical import dumps, loads, new_id, utcnow

MAX_INLINE = 25_000_000  # bytes of file content delivered in one response


def client_path_module(sample: str | None):
    s = str(sample or "")
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\"):
        return ntpath
    return posixpath


class RemoteStore:
    def __init__(self, core):
        self.core = core
        self.db = core.db

    # ------------------------------------------------------------ snapshots in
    def missing(self, project_id: str, snap: dict) -> list[str]:
        refs = set((snap.get("files") or {}).values()) | set((snap.get("memory") or {}).values())
        for t in snap.get("transcripts") or []:
            refs |= set(t.get("parts") or [])
        return sorted(r for r in refs if isinstance(r, str) and r.startswith("sha256:") and not self.core.objects.exists(project_id, r))

    def put_blobs(self, project_id: str, blobs: dict) -> int:
        n = 0
        for ref, b64 in (blobs or {}).items():
            data = base64.b64decode(b64)
            got = self.core.objects.put(project_id, data)
            if got != ref:
                raise ValueError(f"content does not match {ref}")
            n += 1
        return n

    def store(self, project_id: str, agent: dict, snap: dict) -> None:
        now = utcnow()
        client = dict(snap.get("client") or {})
        if snap.get("memory_dir"):
            client["memory_dir"] = snap["memory_dir"]
        self.db.execute("INSERT OR REPLACE INTO host_snapshots VALUES (?,?,?,?,?,?)",
                        (agent["agent_id"], project_id, dumps(snap.get("files") or {}), dumps(snap.get("memory") or {}), dumps(client), now))
        for t in snap.get("transcripts") or []:
            if not t.get("session_id"):
                continue
            self.db.execute("INSERT OR REPLACE INTO host_transcripts VALUES (?,?,?,?,?,?)",
                            (t["session_id"], agent["agent_id"], project_id, dumps(t.get("parts") or []), 1 if t.get("has_conversation") else 0, now))

    def snapshot(self, agent: dict) -> dict:
        r = self.db.one("SELECT * FROM host_snapshots WHERE agent_id=?", (agent["agent_id"],))
        if not r:
            return {"files": {}, "memory": {}, "client": {}}
        return {"files": loads(r["files_json"], {}), "memory": loads(r["memory_json"], {}), "client": loads(r["client_json"], {})}

    def transcript(self, agent: dict, session_id: str | None, branch_id: str | None) -> bytes | None:
        pid = agent["project_id"]
        row = None
        if session_id:
            row = self.db.one("SELECT parts_json FROM host_transcripts WHERE session_id=? AND has_conversation=1", (session_id,))
        if not row and branch_id:
            row = self.db.one("SELECT t.parts_json FROM host_transcripts t JOIN host_sessions h ON h.session_id=t.session_id "
                              "JOIN runs r ON r.run_id=h.run_id WHERE t.agent_id=? AND t.has_conversation=1 AND r.branch_id=? "
                              "ORDER BY t.updated_at DESC LIMIT 1", (agent["agent_id"], branch_id))
        if not row:
            row = self.db.one("SELECT parts_json FROM host_transcripts WHERE agent_id=? AND has_conversation=1 ORDER BY updated_at DESC LIMIT 1",
                              (agent["agent_id"],))
        if not row:
            return None
        return b"".join(self.core.objects.get(pid, ref) for ref in loads(row["parts_json"], []))

    # ------------------------------------------------------------ changes out
    def queue(self, agent: dict, op: dict) -> None:
        self.db.execute("INSERT INTO connector_ops(project_id,agent_id,op_json,status,created_at) VALUES (?,?,?,?,?)",
                        (agent["project_id"], agent["agent_id"], dumps(op), "pending", utcnow()))

    def deliver(self, project_id: str, agent_id: str | None = None) -> list[dict]:
        """Pending operations (with their content inline), marked delivered. Writes and deletes are idempotent."""
        q = "SELECT * FROM connector_ops WHERE project_id=? AND status='pending'" + (" AND agent_id=?" if agent_id else "") + " ORDER BY op_id"
        rows = self.db.all(q, (project_id, agent_id) if agent_id else (project_id,))
        out, size = [], 0
        for r in rows:
            op = loads(r["op_json"])
            if op.get("blob"):
                data = self.core.objects.get(project_id, op["blob"])
                size += len(data)
                if size > MAX_INLINE and out:
                    break
                op["data_b64"] = base64.b64encode(data).decode("ascii")
            op["op_id"] = r["op_id"]
            out.append(op)
            self.db.execute("UPDATE connector_ops SET status='delivered', delivered_at=? WHERE op_id=?", (utcnow(), r["op_id"]))
        return out

    # ------------------------------------------------------------ commands from the website
    def enqueue_command(self, agent: dict, line: str, actor: str) -> dict:
        cid = new_id("ccmd")
        now = utcnow()
        self.db.execute("INSERT INTO connector_commands VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, agent["project_id"], agent["agent_id"], line, actor, "queued", None, now, now))
        return self.command(cid)

    def command(self, cid: str) -> dict:
        r = self.db.one("SELECT * FROM connector_commands WHERE command_id=?", (cid,))
        if not r:
            return {}
        r["result"] = loads(r.pop("result_json"), None) if r.get("result_json") else None
        return r

    def queued_commands(self, project_id: str) -> list[dict]:
        rows = self.db.all("SELECT c.command_id, c.agent_id, c.line, a.config_json FROM connector_commands c JOIN agents a ON a.agent_id=c.agent_id "
                           "WHERE c.project_id=? AND c.status='queued' ORDER BY c.created_at", (project_id,))
        return [{"command_id": r["command_id"], "agent_id": r["agent_id"], "line": r["line"],
                 "project_dir": loads(r["config_json"]).get("project_dir")} for r in rows]

    def finish_command(self, cid: str, result: dict) -> None:
        self.db.execute("UPDATE connector_commands SET status='done', result_json=?, updated_at=? WHERE command_id=?",
                        (dumps(result), utcnow(), cid))

    def heartbeat(self, project_id: str, name: str, os_name: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO connectors VALUES (?,?,?,?)", (project_id, name or "computer", os_name, utcnow()))

    def connectors(self, project_id: str) -> list[dict]:
        return self.db.all("SELECT name, os, last_seen FROM connectors WHERE project_id=? ORDER BY last_seen DESC", (project_id,))
