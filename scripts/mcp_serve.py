"""Run ANTIBODY as an MCP server (stdio) for Claude Code, Claude Desktop or any MCP host.

The host sees ANTIBODY's tools (limited to the assignment); every call is
checked by the gateway, recorded in the journal and shown in the web UI as a
run of a "via MCP" agent. Add it to Claude Code with:

    claude mcp add antibody -- <repo>/.venv/Scripts/python.exe <repo>/scripts/mcp_serve.py --project proj_demo

stdout carries only MCP protocol messages; diagnostics go to stderr.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import Config  # noqa: E402
from server.core import Core  # noqa: E402
from server.projects import NotFound  # noqa: E402
from server.runtime.mcp_proxy import McpProxy  # noqa: E402
from server.schemas import TaskBody  # noqa: E402

DEFAULT_TOOLS = ["read_source", "list_files", "read_file", "write_report"]


def main() -> None:
    ap = argparse.ArgumentParser(description="ANTIBODY MCP server (stdio)")
    ap.add_argument("--project", default="proj_demo")
    ap.add_argument("--agent", default="Claude Code (via MCP)")
    ap.add_argument("--task", default="mcp_tools")
    ap.add_argument("--server", action="append", default=[], help="also proxy a registered upstream MCP server")
    a = ap.parse_args()
    core = Core(Config.from_env(), recover=False, register_suites=False)
    try:
        core.projects.get_project(a.project)
    except NotFound:
        sys.stderr.write(f"antibody: project {a.project} does not exist (run `make demo` or create it first)\n")
        sys.exit(2)
    row = core.db.one("SELECT agent_id FROM agents WHERE project_id=? AND name=?", (a.project, a.agent))
    agent = core.coordinator.agent(row["agent_id"]) if row else core.coordinator.create_agent(a.project, a.agent, "mcp_proxy", {}, actor="mcp_serve")
    try:
        core.projects.get_task(a.project, a.task)
    except NotFound:
        core.projects.create_task_revision(a.project, TaskBody(task_id=a.task, goal="Work done by an MCP host agent through ANTIBODY's protected tools.",
                                                               tools=DEFAULT_TOOLS), {"role": "app", "actor_id": "mcp_serve"})
    proxy = McpProxy(core, a.project)
    for s in a.server:
        proxy.connect(s)
    exposed = proxy.expose_task_tools(a.task)
    run = proxy.start_run(agent["agent_id"], a.task, mode="live")
    sys.stderr.write(f"antibody: serving {len(exposed)} protected tool(s) as run {run['run_id']}\n")
    try:
        proxy.serve_stdio()
    finally:
        try:
            core.coordinator.set_status(run["run_id"], "completed", "HOST_SESSION_ENDED")
        except Exception:
            pass
        proxy.close()
        core.close()


if __name__ == "__main__":
    main()
