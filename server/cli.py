"""Command line entry points behind the Makefile targets.

  python -m server.cli setup            migrate, register suites, create server-side tokens
  python -m server.cli serve            start API + workers (serves the built UI if present)
  python -m server.cli demo [--reset]   seed the clearly labeled demonstration project
  python -m server.cli eval             frozen behavioral comparison (discovery -> validation -> final)
  python -m server.cli export [--project P]
  python -m server.cli backup DIR | restore-backup DIR
  python -m server.cli gc [--apply]
  python -m server.cli claude-code-install --project-dir DIR [--project P] [--uninstall]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, Config


def _core(**kw):
    from .core import Core
    return Core(Config.from_env(), **kw)


def cmd_setup(a) -> None:
    core = _core()
    p = Path(core.config.SECRETS_PATH)
    secrets = json.loads(p.read_text()) if p.exists() else {}
    if "admin_token" not in secrets:
        secrets["admin_token"] = core.projects.issue_token(None, "admin", "admin:local")
        secrets["operator_token"] = core.projects.issue_token(None, "admin", "operator:local")
        secrets["worker_token"] = core.projects.issue_token(None, "worker", "worker:local")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(secrets, indent=1))
    print(f"database schema version {core.db.schema_version()} at {core.config.DATABASE_PATH}")
    print(f"suites: {[s['suite_id'] + '@' + str(s['revision']) for s in core.evaluator.suites()]}")
    print(f"server-side tokens stored in {p} (never shipped to the browser)")
    core.close()


def cmd_serve(a) -> None:
    import uvicorn
    from .api.app import create_app
    cfg = Config.from_env()
    core = _core(start_workers=True)
    app = create_app(core)
    print(f"ANTIBODY API on http://{cfg.API_BIND_HOST}:{cfg.API_PORT} (loopback by default)")
    uvicorn.run(app, host=cfg.API_BIND_HOST, port=cfg.API_PORT, log_level="warning")


def cmd_demo(a) -> None:
    from . import demo
    core = _core()
    if a.reset:
        demo.reset(core)
        print("[demo] previous demo data removed (local synthetic state only)")
    res = demo.seed(core)
    print(json.dumps(res, indent=1))
    core.close()


def cmd_eval(a) -> None:
    """Reproducible frozen comparison in an isolated store:
    discovery cases produce incidents -> candidates are validated on the validation split ->
    accepted lessons -> untouched final split across the baseline ladder."""
    from .core import Core
    from .evaluation.evaluator import resolve_case_sources
    from .export import write_export
    from .schemas import TaskBody
    tmp = tempfile.mkdtemp(prefix="ab_frozen_eval_")
    core = Core(Config.for_root(tmp))
    pid = "proj_eval"
    core.projects.create_project(pid, "Frozen evaluation", "evaluator")
    suite = core.evaluator.suite(a.suite)
    actor = {"role": "app", "actor_id": "evaluator"}
    log = []
    for case in [c for c in suite["cases"] if c["split"] == "discovery"]:
        sids = [core.sources.ingest_bytes(pid, s["channel"], s["data"], s["filename"])["source_id"] for s in resolve_case_sources(case)]
        agent = core.coordinator.create_agent(pid, f"discovery {case['case_id']}", config={"environment": case.get("environment", {}),
                                                                                          "planner": case.get("agent", {})})
        t = case["task"]
        core.projects.create_task_revision(pid, TaskBody(task_id=case["case_id"], goal=t["goal"], tools=t["tools"], recipient_ids=t.get("recipient_ids", []),
                                                         outbound_data_classes=["approved_report"] if "send_report" in t["tools"] else [],
                                                         resources={"source_ids": sids, "output_path": t.get("output_path")}), actor)
        r = core.coordinator.start_run(pid, agent["agent_id"], case["case_id"], wait=True)
        for inc in core.incidents.list(pid):
            if inc["run_id"] == r["run_id"] and inc["candidate_id"]:
                ev = core.learning.evaluate(pid, inc["candidate_id"], a.suite, "evaluator")
                entry = {"discovery_case": case["case_id"], "candidate": inc["candidate_id"], "validation": ev["status"], "summary": ev["summary"]}
                if ev["status"] == "passed":
                    pr = core.learning.promote(pid, inc["candidate_id"], None, None, None, "assisted", "evaluator")
                    entry["accepted"] = pr["lesson"]["ref"]
                log.append(entry)
    final = core.evaluator.final_evaluation(pid, a.suite, repeats=a.repeats)
    final["discovery_and_validation"] = log
    out = write_export(core, pid, str(ROOT / "exports" / f"eval_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"), final_eval=final)
    print(json.dumps(log, indent=1))
    print("\n| Baseline | Family | Kind | Task completion | Unauthorized effects | Unsafe proposals | Early interceptions | False positives | Outcome expectations met |")
    for r in final["table"]:
        print(f"| {r['baseline']} | {r['family']} | {r['kind']} | {r['task_completion']} | {r['unauthorized_effects']} | {r['unsafe_proposals']} | "
              f"{r['early_quarantines']} | {r['false_positives']} | {r['pass_rate']} |")
    print(f"\nresults written to {out}")
    core.close()
    shutil.rmtree(tmp, ignore_errors=True)


def cmd_export(a) -> None:
    from .export import write_export
    core = _core()
    d = write_export(core, a.project)
    print(f"export written to {d}")
    core.close()


def cmd_backup(a) -> None:
    cfg = Config.from_env()
    dst = Path(a.dir)
    dst.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(cfg.DATABASE_PATH)
    out = sqlite3.connect(dst / "antibody.sqlite")
    src.backup(out)
    out.close()
    src.close()
    shutil.copytree(cfg.OBJECT_STORE_ROOT, dst / "objects", dirs_exist_ok=True)
    shutil.copytree(cfg.WORKSPACE_ROOT, dst / "workspaces", dirs_exist_ok=True)
    if Path(cfg.CHECKPOINT_PATH).exists():
        c1, c2 = sqlite3.connect(cfg.CHECKPOINT_PATH), sqlite3.connect(dst / "checkpoints.sqlite")
        c1.backup(c2)
        c1.close()
        c2.close()
    print(f"backup written to {dst}")


def cmd_restore_backup(a) -> None:
    cfg = Config.from_env()
    src = Path(a.dir)
    if not (src / "antibody.sqlite").exists():
        sys.exit("not a backup directory")
    shutil.copy2(src / "antibody.sqlite", cfg.DATABASE_PATH)
    if (src / "checkpoints.sqlite").exists():
        shutil.copy2(src / "checkpoints.sqlite", cfg.CHECKPOINT_PATH)
    shutil.copytree(src / "objects", cfg.OBJECT_STORE_ROOT, dirs_exist_ok=True)
    shutil.copytree(src / "workspaces", cfg.WORKSPACE_ROOT, dirs_exist_ok=True)
    core = _core()
    print(f"restored; integrity {core.db.integrity_check()}; schema {core.db.schema_version()}")
    core.close()


def cmd_gc(a) -> None:
    core = _core()
    for p in core.projects.list_projects():
        print(p["project_id"], core.versions.gc(p["project_id"], dry_run=not a.apply))
        if a.apply:
            n = core.sources.purge_expired_raw(p["project_id"], core.config.RAW_EVIDENCE_RETENTION_DAYS)
            print(f"  purged raw evidence for {n} source(s) older than {core.config.RAW_EVIDENCE_RETENTION_DAYS} days")
    core.close()


def cmd_claude_code(a) -> None:
    core = _core(start_workers=False, recover=False)
    try:
        if a.uninstall:
            print(json.dumps(core.claude_code.uninstall(a.project_dir), indent=1))
        else:
            print(json.dumps(core.claude_code.install(a.project, a.project_dir, sys.executable, str(ROOT)), indent=1))
    finally:
        core.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="antibody")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    d = sub.add_parser("demo")
    d.add_argument("--reset", action="store_true")
    d.set_defaults(fn=cmd_demo)
    e = sub.add_parser("eval")
    e.add_argument("--suite", default="suite_core@1")
    e.add_argument("--repeats", type=int, default=3)
    e.set_defaults(fn=cmd_eval)
    x = sub.add_parser("export")
    x.add_argument("--project", default="proj_demo")
    x.set_defaults(fn=cmd_export)
    b = sub.add_parser("backup")
    b.add_argument("dir")
    b.set_defaults(fn=cmd_backup)
    rb = sub.add_parser("restore-backup")
    rb.add_argument("dir")
    rb.set_defaults(fn=cmd_restore_backup)
    g = sub.add_parser("gc")
    g.add_argument("--apply", action="store_true")
    g.set_defaults(fn=cmd_gc)
    cc = sub.add_parser("claude-code-install", help="guard Claude Code sessions in a project folder")
    cc.add_argument("--project-dir", required=True)
    cc.add_argument("--project", default="proj_demo")
    cc.add_argument("--uninstall", action="store_true")
    cc.set_defaults(fn=cmd_claude_code)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
