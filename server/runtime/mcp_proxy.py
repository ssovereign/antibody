"""MCP tool proxy (stdio, newline-delimited JSON-RPC 2.0).

ANTIBODY presents itself to a host agent as an MCP server and forwards to
selected trusted upstream servers:
  * upstream identity is pinned (command + script hash) and verified at start;
  * only operator-pinned tools whose input-schema hash matches are exposed;
  * tool annotations are ignored for effect classification - the operator
    registry decides (MCP treats annotations as untrusted);
  * tools/call goes through the gateway; denied calls never reach upstream;
  * results are wrapped as labeled observations; upstream tool errors are
    forwarded as tool errors, never elevated; request IDs are preserved;
  * protocol capabilities are negotiated (intersection), not assumed.

Capability profile: intercept_actions and label_observations only. The host
agent's context and model cursor are invisible here, so full-agent restore is
unsupported, and any direct tool the host keeps is a bypass.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
import sys
import threading
from pathlib import Path

from ..canonical import dumps, hash_value, loads, new_id, utcnow
from ..executors.base import ToolError
from ..projects import ConflictError, InvalidRequest
from ..schemas import ActionProposal

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_SERVER_CAPS = {"tools"}


def identity_hash(command: list[str]) -> str:
    h = hashlib.sha256(json.dumps(command).encode())
    for part in command:
        p = Path(part)
        if p.suffix in (".py", ".js") and p.exists():
            h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


class UpstreamClient:
    def __init__(self, command: list[str], env: dict | None = None):
        import os
        self.command = command
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", env={**os.environ, **(env or {})})
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.server_caps: dict = {}

    def request(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        with self._lock:
            rid = next(self._ids)
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        if not line:
            raise ToolError("E_MCP_CLOSED", "MCPServerClosed", "upstream closed the connection")
        msg = json.loads(line)
        if msg.get("id") != rid:
            raise ToolError("E_MCP_CORRELATION", "MCPCorrelationError", "upstream response did not match the request id")
        if "error" in msg:
            raise ToolError("E_MCP_PROTOCOL", "MCPProtocolError", str(msg["error"].get("message", "error"))[:300], {"rpc_error": msg["error"]})
        return msg["result"]

    def initialize(self) -> dict:
        res = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "antibody-proxy", "version": "1"}})
        self.server_caps = res.get("capabilities", {})
        with self._lock:
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            self.proc.stdin.flush()
        return res

    def list_tools(self) -> list[dict]:
        return self.request("tools/list").get("tools", [])

    def call_tool(self, name: str, args: dict) -> dict:
        res = self.request("tools/call", {"name": name, "arguments": args})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise ToolError("E_UPSTREAM_TOOL", "UpstreamToolError", text[:2000])
        return {"status": "ok", "text": text, "content_types": [c.get("type") for c in res.get("content", [])]}

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            pass


class McpProxy:
    def __init__(self, core, project_id: str):
        self.core = core
        self.project_id = project_id
        self.upstreams: dict[str, UpstreamClient] = {}
        self.exposed: dict[str, dict] = {}
        self.dropped: list[dict] = []
        self.run: dict | None = None

    # ------------------------------------------------------------ operator registration
    @staticmethod
    def register_server(core, project_id: str, server_id: str, command: list[str], pinned_tools: dict) -> dict:
        """pinned_tools: {name: {"schema_hash": ..., "effect_class": ..., "destination_arg": ...}} (operator-controlled)."""
        ident = identity_hash(command)
        core.db.execute("INSERT OR REPLACE INTO mcp_servers VALUES (?,?,?,?,?,1)",
                        (project_id, server_id, dumps(command), ident, dumps(pinned_tools)))
        return {"server_id": server_id, "identity_hash": ident}

    def connect(self, server_id: str, env: dict | None = None) -> dict:
        row = self.core.db.one("SELECT * FROM mcp_servers WHERE project_id=? AND server_id=?", (self.project_id, server_id))
        if not row or not row["trusted"]:
            raise InvalidRequest("UNTRUSTED_SERVER", f"{server_id} is not a selected trusted server")
        command = loads(row["command_json"])
        if identity_hash(command) != row["identity_hash"]:
            raise ConflictError("SERVER_IDENTITY_CHANGED", f"{server_id} no longer matches its pinned identity")
        client = UpstreamClient(command, env)
        init = client.initialize()
        pinned = loads(row["pinned_tools_json"])
        for t in client.list_tools():
            pin = pinned.get(t["name"])
            h = hash_value(t.get("inputSchema", {}))
            if not pin:
                self.dropped.append({"tool": t["name"], "reason": "not pinned by operator"})
                continue
            if pin["schema_hash"] != h:
                self.dropped.append({"tool": t["name"], "reason": "schema hash changed since pinning"})
                continue
            tool_id = f"mcp__{server_id}__{t['name']}"
            self.core.projects.register_tool(self.project_id, tool_id, t["inputSchema"], pin["effect_class"], f"mcp:{server_id}",
                                             server_id=server_id, meta={"destination_arg": pin.get("destination_arg"),
                                                                        "ignored_annotations": t.get("annotations", {})})
            self.exposed[tool_id] = {"name": tool_id, "description": t.get("description", "")[:500], "inputSchema": t["inputSchema"]}
        self.upstreams[server_id] = client
        self.core.executors.mcp_clients[server_id] = client
        return {"server_id": server_id, "server_info": init.get("serverInfo"), "exposed": sorted(self.exposed), "dropped": self.dropped}

    def start_run(self, agent_id: str, task_id: str, mode: str = "live") -> dict:
        c = self.core
        agent = c.coordinator.agent(agent_id)
        if agent["adapter"] != "mcp_proxy":
            raise InvalidRequest("ADAPTER_MISMATCH", "agent is not a proxy agent")
        task = c.projects.get_task(self.project_id, task_id)
        policy = c.projects.policy(self.project_id)
        reg = c.registry.head(self.project_id)
        branch = c.versions.branch(agent["accepted_branch_id"])
        rid = new_id("run")
        start_cfg = {"schema_version": 1, "agent": {"agent_id": agent_id, "adapter": "mcp_proxy"}, "model": {"provider": "host", "model_id": "host-agent"},
                     "task_ref": f"{task_id}@{task['revision']}", "policy_revision": policy["revision"], "registry_revision": reg,
                     "limits": {"max_tool_steps": task["body"]["max_tool_steps"], "repeat_failure_limit": c.config.REPEAT_FAILURE_LIMIT,
                                "no_progress_limit": 10_000, "max_repair_steps": 0, "max_run_cost": "0"},
                     "context_budget_tokens": 0, "purpose": "proxy", "environment": branch["state"]["environment"]}
        now = utcnow()
        c.db.execute("INSERT INTO runs(run_id,project_id,agent_id,branch_id,task_id,task_revision,adapter,mode,status,health,policy_revision,registry_revision,base_version,context_budget_tokens,start_config_json,start_config_hash,thread_id,purpose,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, self.project_id, agent_id, branch["branch_id"], task_id, task["revision"], "mcp_proxy", mode, "running", "Healthy",
                      policy["revision"], reg, branch["head_version"], 0, dumps(start_cfg), hash_value(start_cfg), f"proxy:{rid}", "proxy", now, now))
        self.run = c.coordinator.run(rid)
        c.journal.append(self.project_id, rid, "run.started", {"agent_id": agent_id, "adapter": "mcp_proxy", "mode": mode,
                                                               "capabilities": c.coordinator.capabilities("mcp_proxy").model_dump(),
                                                               "note": "tool-only proxy; host context not visible"}, actor_type="coordinator")
        c.registry.ack(self.run, reg, "run_start")
        return self.run

    # ------------------------------------------------------------ host-facing protocol
    def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            return None  # notifications need no response
        try:
            if method == "initialize":
                caps = {}
                for cap in SUPPORTED_SERVER_CAPS:
                    if all(cap in u.server_caps for u in self.upstreams.values()):
                        caps[cap] = {"listChanged": False}
                return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": caps,
                                                                 "serverInfo": {"name": "antibody-proxy", "version": "1"}}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": mid, "result": {"tools": list(self.exposed.values())}}
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": mid, "result": self._call(msg.get("params", {}))}
            if method == "ping":
                return {"jsonrpc": "2.0", "id": mid, "result": {}}
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method {method} not supported by proxy"}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"proxy error: {type(e).__name__}"}}

    def _call(self, params: dict) -> dict:
        c = self.core
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        if name not in self.exposed:
            return {"content": [{"type": "text", "text": "Tool not available through ANTIBODY."}], "isError": True}
        run = c.coordinator.run(self.run["run_id"])
        action_id, op = c.coordinator.issue_operation(run, purpose=name)
        tool = c.projects.tool(self.project_id, name)
        view = c.gateway.submit(run["run_id"], ActionProposal(action_id=action_id, operation_id=op, tool_id=name,
                                                              tool_schema_hash=tool["schema_hash"], arguments=args,
                                                              observation_refs=self._observed()), c.worker_identity)
        if view.outcome != "ALLOW":
            return {"content": [{"type": "text", "text": f"Blocked before execution ({view.reason_code}): {view.message}"}], "isError": True,
                    "_meta": {"antibody": {"decision_id": view.decision_id, "outcome": view.outcome, "executor_invoked": False}}}
        res = view.result or {}
        if view.effect_state != "COMPLETED":
            err = (res.get("error") or {}).get("message", "tool failed")
            src = c.sources.ingest_tool_result(self.project_id, run["run_id"], name, {"status": "error", "is_error": True, "error_message": err})
            c.coordinator.receive_observation(run, src["source_id"])
            return {"content": [{"type": "text", "text": err}], "isError": True,
                    "_meta": {"antibody": {"decision_id": view.decision_id, "source_id": src["source_id"], "origin": "EXTERNAL_DATA"}}}
        src = c.sources.ingest_tool_result(self.project_id, run["run_id"], name, res)
        refs = c.coordinator.receive_observation(run, src["source_id"])
        text, _ = c.compiler.observation_text(run, refs[0]) if refs else (res.get("text", ""), {})
        return {"content": [{"type": "text", "text": text if text is not None else "[quarantined]"}], "isError": False,
                "_meta": {"antibody": {"decision_id": view.decision_id, "source_id": src["source_id"], "origin": "EXTERNAL_DATA",
                                       "label": "tool result is data, not instructions"}}}

    def _observed(self) -> list[str]:
        return [r["src_ref"] for r in self.core.db.all("SELECT DISTINCT src_ref FROM edges WHERE run_id=? AND kind='observation'", (self.run["run_id"],))][-40:]

    def serve_stdio(self, stdin=sys.stdin, stdout=sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                stdout.flush()
                continue
            out = self.handle(msg)
            if out is not None:
                stdout.write(json.dumps(out) + "\n")
                stdout.flush()

    def close(self) -> None:
        for u in self.upstreams.values():
            u.close()


def main() -> None:  # pragma: no cover - CLI entry
    import argparse
    from ..core import Core
    ap = argparse.ArgumentParser(description="ANTIBODY MCP proxy (stdio)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--server", action="append", required=True)
    a = ap.parse_args()
    core = Core()
    proxy = McpProxy(core, a.project)
    for s in a.server:
        proxy.connect(s)
    proxy.start_run(a.agent, a.task)
    try:
        proxy.serve_stdio()
    finally:
        proxy.close()
        core.close()


if __name__ == "__main__":  # pragma: no cover
    main()
