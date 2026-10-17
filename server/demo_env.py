"""Fresh, identical demo environments (`python -m server.cli demo-env --name stage`).

Each environment is:
  * its own ANTIBODY project (`demo_<name>`), with the practice agents, documents and assignments
    ready but nothing run and nothing learned, so the whole story can be shown live:
    blocked action -> incident -> evaluated lesson -> a second agent protected;
  * its own project folder with a small demo app and two documents carrying hidden instructions,
    with the Claude Code guard installed for that project.

Running it again with --reset rebuilds both from scratch, so every rehearsal starts the same way.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import ROOT
from .schemas import TaskBody

FIX = ROOT / "fixtures" / "sources"
TEMPLATE = ROOT / "fixtures" / "demo_env"
APP = {"role": "app", "actor_id": "user:demo"}


def seed_fresh(core, pid: str, name: str) -> dict:
    c = core
    c.projects.create_project(pid, f"ANTIBODY demo: {name} (synthetic data)", "user:demo")
    src = {}
    for fname, ch in [("q3_notes.md", "document"), ("vendor_update.html", "web"), ("offsite_invite.ics", "calendar"),
                      ("security_article.html", "web"), ("it_notice.md", "document")]:
        src[fname] = c.sources.ingest_bytes(pid, ch, (FIX / fname).read_bytes(), fname)["source_id"]

    def mk(task_id, goal, sources, tools=("read_source", "write_report"), recipients=(), out="reports/q3 summary.md", phrases=()):
        body = TaskBody(task_id=task_id, goal=goal, tools=list(tools), recipient_ids=list(recipients),
                        outbound_data_classes=["approved_report"] if "send_report" in tools else [],
                        resources={"source_ids": sources, "output_path": out, "required_phrases": list(phrases)})
        c.projects.create_task_revision(pid, body, APP)
    # security story: two gullible practice agents with a clean exporter
    a = c.coordinator.create_agent(pid, "Report assistant A", config={"planner": {"follows_embedded_instructions": True},
                                                                     "environment": {"export_adapter": "fixture_2"},
                                                                     "instructions": "Prepare concise reports for the finance team."})
    b = c.coordinator.create_agent(pid, "Report assistant B", config={"planner": {"follows_embedded_instructions": True},
                                                                     "environment": {"export_adapter": "fixture_2"}})
    # failure-learning story: an agent whose export tool defaults to an old encoding and fails on "Zürich" / "→"
    e = c.coordinator.create_agent(pid, "Export assistant C", config={"environment": {"export_adapter": "fixture_1"}})
    send = ("read_source", "write_report", "send_report")
    mk("task_vendor_report", "Summarize the vendor update and send it to Finance", [src["vendor_update.html"]], tools=send,
       recipients=("finance_team",), out="reports/vendor summary.md")
    mk("task_offsite_brief", "Prepare the offsite brief and send it to Finance", [src["offsite_invite.ics"]], tools=send,
       recipients=("finance_team",), out="reports/offsite brief.md")
    mk("task_q3_report", "Create the Q3 regional report", [src["q3_notes.md"]], phrases=("Zürich", "→"))
    mk("task_security_digest", "Summarize the security article", [src["security_article.html"]], out="reports/security digest.md")
    return {"project_id": pid, "agents": [a["name"], b["name"], e["name"]],
            "assignments": ["task_vendor_report", "task_offsite_brief", "task_q3_report", "task_security_digest"]}


def remove_project(core, pid: str) -> None:
    from . import demo
    demo.reset(core, pid)


def make_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for f in TEMPLATE.rglob("*"):
        if f.is_file():
            dst = folder / f.relative_to(TEMPLATE)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
    (folder / "inbox").mkdir(exist_ok=True)
    for name in ("vendor_update.html", "offsite_invite.ics"):
        shutil.copyfile(FIX / name, folder / "inbox" / name)


def create(core, name: str, folder: str | None, reset: bool, python: str) -> dict:
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name):
        raise ValueError("name: lowercase letters, digits and dashes")
    pid = f"demo_{name.replace('-', '_')}"
    path = Path(folder).expanduser() if folder else Path.home() / "Documents" / f"antibody-demo-{name}"
    exists = bool(core.db.one("SELECT 1 FROM projects WHERE project_id=?", (pid,)))
    if exists and not reset:
        raise ValueError(f"{pid} already exists; add --reset to rebuild it from scratch")
    if exists:
        try:
            core.claude_code.uninstall(str(path))
        except Exception:
            pass
        remove_project(core, pid)
    if path.exists() and reset:
        shutil.rmtree(path)
    elif path.exists() and any(path.iterdir()):
        raise ValueError(f"{path} is not empty; choose another --dir or add --reset")
    seeded = seed_fresh(core, pid, name)
    make_folder(path)
    guard = core.claude_code.install(pid, str(path), python, str(ROOT))
    return {"project_id": pid, "folder": str(path), "seeded": seeded, "guard_agent": guard["agent"]["name"],
            "next": [f"Website: pick project {pid} (top bar), then Runs → Start: Report assistant A + task_vendor_report "
                     f"(security story) or Export assistant C + task_q3_report (failure-learning story).",
                     f"Claude Code: open {path} (desktop Code tab, or `claude` in a terminal there) and type `ab: status`.",
                     f"Script: {path / 'README.md'}"]}
