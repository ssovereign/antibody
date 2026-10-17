import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import Config  # noqa: E402
from server.core import Core  # noqa: E402
from server.schemas import TaskBody  # noqa: E402

FIX = ROOT / "fixtures" / "sources"
APP = {"role": "app", "actor_id": "user:test", "project_id": "proj"}
PID = "proj"


def pytest_configure(config):
    config.addinivalue_line("markers", "security: gateway, source and restore regression tests")


@pytest.fixture
def make_core(tmp_path):
    cores = []

    def _make(**overrides):
        c = Core(Config.for_root(tmp_path / "state", **overrides))
        c.propagate_runtime_errors = True
        cores.append(c)
        return c
    yield _make
    for c in cores:
        try:
            c.close()
        except Exception:
            pass


@pytest.fixture
def core(make_core):
    c = make_core()
    c.projects.create_project(PID, "Test", "owner")
    return c


def ingest(core, name, channel="document", pid=PID):
    return core.sources.ingest_bytes(pid, channel, (FIX / name).read_bytes(), name)["source_id"]


def ingest_text(core, text, filename, channel="document", pid=PID):
    return core.sources.ingest_bytes(pid, channel, text.encode("utf-8"), filename)["source_id"]


def task(core, task_id, sources, tools=("read_source", "write_report"), recipients=(), out="reports/q3 summary.md",
         phrases=(), outbound=None, pid=PID, **kw):
    body = TaskBody(task_id=task_id, goal=kw.pop("goal", f"Goal for {task_id}"), tools=list(tools), recipient_ids=list(recipients),
                    outbound_data_classes=outbound if outbound is not None else (["approved_report"] if "send_report" in tools else []),
                    resources={"source_ids": list(sources), "output_path": out, "required_phrases": list(phrases)}, **kw)
    return core.projects.create_task_revision(pid, body, APP)


def agent(core, name="Agent A", naive=False, env=None, pid=PID, adapter="langgraph_reference"):
    cfg = {"planner": {"follows_embedded_instructions": naive}}
    if env:
        cfg["environment"] = env
    return core.coordinator.create_agent(pid, name, adapter, cfg)


def run(core, agent_id, task_id, **kw):
    kw.setdefault("wait", True)
    return core.coordinator.start_run(PID, agent_id, task_id, **kw)


def kinds(core, run_id):
    return [e["kind"] for e in core.journal.events(run_id)]


def denied_invocations(core):
    return core.db.scalar("SELECT COUNT(*) FROM executor_invocations i JOIN decisions d ON d.decision_id=i.decision_id WHERE d.outcome!='ALLOW'")
