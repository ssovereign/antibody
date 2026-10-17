"""Claude Code hook: forwards one hook event to ANTIBODY and relays its decision.

Installed by `python -m server.cli claude-code-install --project-dir <folder>`; Claude Code
runs it for SessionStart, UserPromptSubmit, PreToolUse, PostToolUse(Failure) and SessionEnd.
Standard library only, so it starts fast and needs no virtualenv.

If ANTIBODY is not reachable:
  ANTIBODY_FAIL_MODE=ask (default)  tool calls that can change things need your OK in Claude Code;
                                    reads and searches go ahead
  ANTIBODY_FAIL_MODE=closed         those calls are blocked
  ANTIBODY_FAIL_MODE=open           nothing is checked
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("ANTIBODY_URL", "http://127.0.0.1:8765")
READ_ONLY = {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch"}


def token(project: str) -> str | None:
    if os.environ.get("ANTIBODY_HOOK_TOKEN"):
        return os.environ["ANTIBODY_HOOK_TOKEN"]
    try:
        s = json.loads((ROOT / "var" / "secrets.json").read_text(encoding="utf-8"))
        return s.get("claude_code_hook_tokens", {}).get(project)
    except (OSError, ValueError):
        return None


def emit(stdout: str | None, code: int = 0) -> None:
    if stdout:
        sys.stdout.buffer.write(stdout.encode("utf-8"))
        sys.stdout.flush()
    sys.exit(code)


def unreachable(ev: dict, why: str) -> None:
    mode = os.environ.get("ANTIBODY_FAIL_MODE", "ask").lower()
    name = ev.get("hook_event_name")
    if mode == "open" or name != "PreToolUse" or ev.get("tool_name") in READ_ONLY:
        if name == "UserPromptSubmit" and str(ev.get("prompt", "")).lower().startswith(("ab:", "/ab ")):
            emit(json.dumps({"decision": "block", "reason": f"ANTIBODY is not reachable ({why}). Start it with 'make dev'."}))
        emit(None)
    decision = "deny" if mode == "closed" else "ask"
    emit(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                            "permissionDecisionReason": f"ANTIBODY could not check this call ({why})."}}))


def main() -> None:
    args = sys.argv[1:]
    if "--connector" in args:  # hosted ANTIBODY: snapshots up, changes down (see antibody_connector.py)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import antibody_connector
        antibody_connector.hook_main(args)
        return
    project = args[args.index("--project") + 1] if "--project" in args else os.environ.get("ANTIBODY_PROJECT", "")
    raw = sys.stdin.buffer.read()
    try:
        ev = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        emit(None)
        return
    ev["antibody_project_dir"] = (args[args.index("--dir") + 1] if "--dir" in args else None) or os.environ.get("CLAUDE_PROJECT_DIR")
    url = (args[args.index("--url") + 1] if "--url" in args else None) or URL  # the ANTIBODY this folder was installed for
    tok = token(project)
    if not tok:
        unreachable(ev, "no hook token; run the installer again")
        return
    req = urllib.request.Request(f"{os.environ.get('ANTIBODY_URL') or url}/v1/hooks/claude-code?project_id={project}", data=json.dumps(ev).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local service: never via a proxy
        with opener.open(req, timeout=1790 if ev.get("hook_event_name") == "UserPromptSubmit" else 25) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        unreachable(ev, f"HTTP {e.code}")
        return
    except (urllib.error.URLError, OSError, ValueError) as e:
        unreachable(ev, type(e).__name__)
        return
    emit(res.get("stdout"), int(res.get("exit", 0)))


if __name__ == "__main__":
    main()
