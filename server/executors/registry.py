"""Executor registry. Only the gateway calls `run`, with a bound decision context."""
from __future__ import annotations

from .base import ExecContext, ToolError
from .fetch import FetchExecutor
from .files import FileExecutor
from .outbox import LocalLedger, LocalOutbox


class Executors:
    def __init__(self, core):
        self.core = core
        cfg = core.config
        self.files = FileExecutor()
        self.outbox = LocalOutbox(cfg.OUTBOX_ROOT)
        self.ledger = LocalLedger(cfg.OUTBOX_ROOT)
        self.fetcher = FetchExecutor(cfg.MAX_SOURCE_BYTES, cfg.FETCH_MAX_REDIRECTS, cfg.FETCH_TIMEOUT_SECONDS)
        self.mcp_clients: dict[str, object] = {}

    def run(self, tool_id: str, args: dict, ctx: ExecContext) -> dict:
        tool = self.core.projects.tool(ctx.project_id, tool_id)
        if tool is None:
            raise ToolError("E_NO_TOOL", "ToolNotFound", tool_id)
        ex = tool["executor"]
        if ex == "sources":
            src = self.core.sources.get(args["source_id"])
            if not src or src["project_id"] != ctx.project_id:
                raise ToolError("E_NOT_FOUND", "SourceNotFound", "source not found")
            return {"status": "ok", "source_id": src["source_id"], "observation_source": src["source_id"],
                    "label": src["label"], "origin": src["origin"], "channel": src["channel"]}
        if ex == "files":
            return getattr(self.files, tool_id)(ctx, args)
        if ex.startswith("export"):
            return self.files.write_report(ctx, args)
        if ex == "fetch":
            return self.fetcher.fetch(args["url"], ctx.task.get("read_network", {}).get("allowed_hosts") or None)
        if ex == "outbox":
            rcpt = self.core.projects.recipient(ctx.project_id, args["recipient_id"])
            return self.outbox.send(ctx, args, rcpt)
        if ex == "ledger":
            payee = self.core.projects.recipient(ctx.project_id, args["payee_id"])
            return self.ledger.pay(ctx, args, payee)
        if ex.startswith("mcp:"):
            client = self.mcp_clients.get(ex[4:])
            if client is None:
                raise ToolError("E_MCP_UNAVAILABLE", "MCPServerUnavailable", f"upstream {ex[4:]} not connected")
            return client.call_tool(tool_id.split("__", 2)[-1], args)
        raise ToolError("E_NO_EXECUTOR", "ExecutorNotFound", ex)

    def reconcile(self, project_id: str, tool_id: str, operation_id: str) -> dict | None:
        tool = self.core.projects.tool(project_id, tool_id)
        if not tool or not tool["reconcile"]:
            return None
        if tool["executor"] == "outbox":
            return self.outbox.reconcile(project_id, operation_id)
        if tool["executor"] == "ledger":
            return self.ledger.reconcile(project_id, operation_id)
        return None
