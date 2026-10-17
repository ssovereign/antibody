"""Chat with a Claude agent behind ANTIBODY.

A conversation has a standing assignment: the permission slip (tools,
recipients, documents, web access) is set once and reused for every message.
Each user message starts a run of the agent on the conversation's current
branch; Claude's tool calls go through the gateway like any other agent's, and
its final text becomes the reply. Messages starting with "/" are version
history commands (see server/commands.py) and never reach the model.
"""
from __future__ import annotations

from .canonical import dumps, loads, new_id, utcnow
from .projects import ConflictError, InvalidRequest, NotFound
from .schemas import TaskBody

DEFAULT_TOOLS = ["read_source", "list_files", "read_file", "write_report"]
ASSISTANT_INSTRUCTIONS = ("Be concise and concrete. Use the workspace tools to read the selected documents and to save anything the "
                          "user asks you to write. Say plainly when an action was blocked and what permission it would need.")


class ChatService:
    def __init__(self, core):
        self.core = core
        self.db = core.db

    # ------------------------------------------------------------ agents & permissions
    def default_agent(self, project_id: str, actor: str) -> dict:
        row = self.db.one("SELECT agent_id FROM agents WHERE project_id=? AND name='Claude assistant'", (project_id,))
        if row:
            return self.core.coordinator.agent(row["agent_id"])
        return self.core.coordinator.create_agent(project_id, "Claude assistant", config={
            "model": {"provider": "anthropic", "model_id": self.core.claude.settings()["model"]},
            "environment": {"export_adapter": "fixture_2"}, "instructions": ASSISTANT_INSTRUCTIONS}, actor=actor)

    def _body(self, task_id: str, title: str, perms: dict) -> TaskBody:
        tools = perms.get("tools") or DEFAULT_TOOLS
        return TaskBody(
            task_id=task_id, goal=f"Chat: {title}. Help the user with the requests they make in this conversation.",
            tools=tools, recipient_ids=perms.get("recipient_ids", []),
            outbound_data_classes=["approved_report"] if "send_report" in tools else [],
            read_network={"public_https": bool(perms.get("web")), "allowed_hosts": perms.get("allowed_hosts", [])},
            max_tool_steps=int(perms.get("max_tool_steps", 30)), grant_refs=perms.get("grant_refs", []),
            resources={"source_ids": perms.get("source_ids", []), "output_path": None})

    @staticmethod
    def _perms_from(body: dict) -> dict:
        return {"tools": body["tools"], "recipient_ids": body["recipient_ids"], "source_ids": body["resources"]["source_ids"],
                "web": body["read_network"]["public_https"], "allowed_hosts": body["read_network"].get("allowed_hosts", []),
                "max_tool_steps": body["max_tool_steps"], "grant_refs": body.get("grant_refs", [])}

    # ------------------------------------------------------------ conversations
    def create(self, project_id: str, actor: dict, title: str | None = None, agent_id: str | None = None,
               permissions: dict | None = None) -> dict:
        agent = self.core.coordinator.agent(agent_id) if agent_id else self.default_agent(project_id, actor["actor_id"])
        if agent["project_id"] != project_id:
            raise NotFound("agent")
        if agent["adapter"] != "langgraph_reference":
            raise InvalidRequest("CAPABILITY_UNSUPPORTED", "chat needs the built-in reference adapter")
        cid = new_id("chat")
        title = (title or "New conversation").strip()[:80]
        task_id = f"chat_{cid[-10:]}"
        self.core.projects.create_task_revision(project_id, self._body(task_id, title, permissions or {}), actor)
        now = utcnow()
        self.db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, project_id, agent["agent_id"], agent["accepted_branch_id"], task_id, title, actor["actor_id"], now, now))
        self._add(cid, "system", "Conversation started. Type a request, or /help for version history commands.")
        return self.get(cid)

    def get(self, cid: str) -> dict:
        c = self.db.one("SELECT * FROM conversations WHERE conversation_id=?", (cid,))
        if not c:
            raise NotFound(f"conversation {cid}")
        task = self.core.projects.get_task(c["project_id"], c["task_id"])
        b = self.core.versions.branch(c["branch_id"])
        agent = self.core.coordinator.agent(c["agent_id"])
        busy = self.db.one("SELECT run_id, status FROM runs WHERE branch_id=? AND status IN ('running','recovering') ORDER BY created_at DESC LIMIT 1",
                           (c["branch_id"],))
        c.update({"permissions": self._perms_from(task["body"]), "task_revision": task["revision"],
                  "branch": {"branch_id": b["branch_id"], "name": b["name"], "head_version": b["head_version"]},
                  "agent": {"agent_id": agent["agent_id"], "name": agent["name"], "model": agent["config"].get("model", {})},
                  "active_run": busy, "claude_connected": self.core.claude.connected()})
        return c

    def list(self, project_id: str) -> list[dict]:
        rows = self.db.all("SELECT c.*, (SELECT text FROM chat_messages m WHERE m.conversation_id=c.conversation_id AND m.role IN ('user','assistant') "
                           "ORDER BY seq DESC LIMIT 1) AS last_text FROM conversations c WHERE project_id=? ORDER BY updated_at DESC", (project_id,))
        return rows

    def messages(self, cid: str, after: int = 0) -> list[dict]:
        rows = self.db.all("SELECT * FROM chat_messages WHERE conversation_id=? AND seq>? ORDER BY seq", (cid, after))
        for r in rows:
            r["meta"] = loads(r.pop("meta_json"), {})
        return rows

    def set_permissions(self, cid: str, perms: dict, actor: dict) -> dict:
        c = self.get(cid)
        self.core.projects.create_task_revision(c["project_id"], self._body(c["task_id"], c["title"], perms), actor)
        self._add(cid, "system", "Permissions updated (new assignment revision). They apply from the next message.")
        return self.get(cid)

    def _add(self, cid: str, role: str, text: str, run_id: str | None = None, meta: dict | None = None) -> dict:
        with self.db.tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM chat_messages WHERE conversation_id=?", (cid,)).fetchone()[0]
            c.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?)", (cid, seq, role, text, run_id, dumps(meta or {}), utcnow()))
            c.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?", (utcnow(), cid))
        return {"conversation_id": cid, "seq": seq, "role": role, "text": text, "run_id": run_id, "meta": meta or {}}

    # ------------------------------------------------------------ posting
    def post(self, cid: str, text: str, actor: dict, wait: bool = False) -> dict:
        c = self.get(cid)
        text = (text or "").strip()
        if not text:
            raise InvalidRequest("EMPTY_MESSAGE", "type a message")
        if text.startswith("/"):
            res = self.core.commands.run(c["project_id"], c["agent_id"], c["branch_id"], text[1:], actor["actor_id"])
            if res["branch_id"] != c["branch_id"]:
                self.db.execute("UPDATE conversations SET branch_id=? WHERE conversation_id=?", (res["branch_id"], cid))
            msg = self._add(cid, "command", text, meta={"output": res["output"], "ok": res["ok"]})
            return {"message": msg, "command": res, "conversation": self.get(cid)}
        if not self.core.claude.connected():
            raise InvalidRequest("CLAUDE_NOT_CONNECTED", "Claude is not connected. Add your Anthropic API key in Settings → Connect Claude.")
        if c["active_run"]:
            raise ConflictError("REPLY_IN_PROGRESS", "Wait for the current reply to finish (or pause that run).")
        msg = self._add(cid, "user", text)
        model = {"provider": "anthropic", "model_id": self.core.claude.settings()["model"]}
        run = self.core.coordinator.start_run(c["project_id"], c["agent_id"], c["task_id"], branch_id=c["branch_id"], actor=actor["actor_id"],
                                              purpose="chat", wait=wait, context_budget_tokens=60000,
                                              extra={"chat": {"conversation_id": cid, "seq": msg["seq"]}, "model_override": model})
        self.db.execute("UPDATE chat_messages SET run_id=? WHERE conversation_id=? AND seq=?", (run["run_id"], cid, msg["seq"]))
        msg["run_id"] = run["run_id"]
        return {"message": msg, "run": {"run_id": run["run_id"], "status": run["status"]}, "conversation": self.get(cid)}

    # ------------------------------------------------------------ run hooks
    def add_reply(self, run: dict, text: str) -> None:
        chat = loads(run["start_config_json"]).get("chat")
        if not chat:
            return
        counts = {
            "blocked": self.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=? AND outcome!='ALLOW'", (run["run_id"],)),
            "actions": self.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=?", (run["run_id"],)),
        }
        self._add(chat["conversation_id"], "assistant", text, run["run_id"], meta=counts)

    def after_run(self, run: dict) -> None:
        """Tell the conversation when a reply could not be produced (paused, failed)."""
        chat = loads(run["start_config_json"]).get("chat")
        if not chat or run["status"] in ("running", "recovering", "completed"):
            return
        if self.db.scalar("SELECT 1 FROM chat_messages WHERE conversation_id=? AND run_id=? AND role IN ('assistant','system') AND seq>?",
                          (chat["conversation_id"], run["run_id"], chat["seq"])):
            return
        reasons = {
            "OUTCOME_UNKNOWN": "an action's outcome is unknown. Reconcile it in Effects & outbox, then resume the run.",
            "GRANT_REQUIRED": "the next action needs a grant you have not given.",
            "BUDGET_EXCEEDED": "it used its step or cost budget.",
            "MODEL_ERROR": "the call to Claude failed. Check Settings → Connect Claude.",
            "STALE_REVISION": "the permissions changed while it was working. Send the message again.",
            "AUDIT_UNCERTAIN": "a safety check was unsure about the next action and wants a human look.",
            "AUDIT_FAILED": "a safety check could not run, so the action was held back.",
            "CONTEXT_BUDGET": "the conversation no longer fits the context budget.",
        }
        why = reasons.get(run["status_reason"] or "", f"{run['status_reason'] or run['status']}.")
        self._add(chat["conversation_id"], "system", f"The reply paused: {why}", run["run_id"],
                  meta={"status": run["status"], "reason": run["status_reason"]})
