"""ANTIBODY connector: links Claude Code on this computer to a hosted ANTIBODY.

    python scripts/antibody_connector.py connect <code>     # code from the website: "Connect a computer"
    python scripts/antibody_connector.py guard <folder>      # guard a project folder
    python scripts/antibody_connector.py demo [name]         # make and guard a practice folder
    python scripts/antibody_connector.py run                 # stay connected (website restores and merges reach this computer)

The hook script calls into this module for every Claude Code event in a guarded folder. It sends the
event to the hosted server; for prompts and turn ends it also sends a snapshot of the folder (content
hashes of the files, Claude's memory for the folder and the conversation), and uploads only content the
server does not already have. Changes the server sends back (going back to a saved state, switching
branches, a merge, a resumable session) are applied only inside guarded folders and Claude Code's own
session folder. Standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONFIG_DIR = Path(os.environ.get("ANTIBODY_HOME") or Path.home() / ".antibody")
CONFIG = CONFIG_DIR / "connector.json"
HASH_CACHE = CONFIG_DIR / "hash_cache.json"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache", ".antibody"}
MAX_FILES, MAX_FILE_BYTES, PART_LINES = 2000, 1_000_000, 400
UPLOAD_BATCH = 6_000_000
READ_ONLY = {"Read", "Glob", "Grep", "LS", "TodoWrite", "WebSearch"}


# ------------------------------------------------------------------ config + http
def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1), encoding="utf-8")


def call(cfg: dict, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    req = urllib.request.Request(cfg["server"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if "127.0.0.1" in cfg["server"] or "localhost" in cfg["server"] \
        else urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


def encode_dir(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def ref_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ snapshots
def _project_files(root: Path) -> list[Path]:
    tracked = None
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                tracked = [root / l for l in out.stdout.splitlines() if l.strip()]
        except Exception:
            tracked = None
    files = []
    for p in (tracked if tracked is not None else root.rglob("*")):
        if len(files) >= MAX_FILES:
            break
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file() or p.is_symlink():
            continue
        if (rel.parts[:1] == (".claude",) and rel.name.startswith("settings")) or str(rel) == ".mcp.json":
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(p)
    return files


class Snapshot:
    """Content hashes of a folder (+ Claude's memory and conversation for it), and the bytes on demand."""

    def __init__(self, project_dir: str, transcript_path: str | None = None):
        self.root = Path(project_dir)
        self.content: dict[str, bytes | Path] = {}
        try:
            cache = json.loads(HASH_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        self._cache, self._dirty = cache, False
        files = {}
        for p in _project_files(self.root):
            files[p.relative_to(self.root).as_posix()] = self._hash(p)
        cpd = claude_projects_dir()
        mem = cpd / encode_dir(str(self.root)) / "memory"
        memory = {}
        if mem.is_dir():
            for f in sorted(mem.rglob("*")):
                if f.is_file() and f.stat().st_size <= MAX_FILE_BYTES:
                    memory[f.relative_to(mem).as_posix()] = self._hash(f)
        transcripts = []
        tpath = Path(transcript_path) if transcript_path else self._newest_transcript(cpd)
        if tpath and tpath.exists():
            data = tpath.read_bytes()
            lines = data.splitlines(keepends=True)
            parts = [b"".join(lines[i:i + PART_LINES]) for i in range(0, len(lines), PART_LINES)] or [b""]
            refs = []
            for part in parts:
                r = ref_of(part)
                self.content[r] = part
                refs.append(r)
            transcripts.append({"session_id": tpath.stem, "parts": refs, "has_conversation": b'"assistant"' in data})
        self.body = {"files": files, "memory": memory, "memory_dir": str(mem), "transcripts": transcripts,
                     "client": {"os": platform.system(), "claude_projects_dir": str(cpd)}}
        if self._dirty:
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                HASH_CACHE.write_text(json.dumps(self._cache), encoding="utf-8")
            except OSError:
                pass

    def _newest_transcript(self, cpd: Path) -> Path | None:
        d = cpd / encode_dir(str(self.root))
        try:
            cands = [p for p in d.glob("*.jsonl") if p.is_file()]
        except OSError:
            return None
        return max(cands, key=lambda p: p.stat().st_mtime) if cands else None

    def _hash(self, p: Path) -> str:
        st = p.stat()
        key = str(p)
        hit = self._cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            self.content[hit[2]] = p
            return hit[2]
        r = ref_of(p.read_bytes())
        self._cache[key] = [st.st_mtime_ns, st.st_size, r]
        self._dirty = True
        self.content[r] = p
        return r

    def bytes_for(self, ref: str) -> bytes | None:
        src = self.content.get(ref)
        if src is None:
            return None
        data = src if isinstance(src, bytes) else src.read_bytes()
        return data if ref_of(data) == ref else None


def upload(cfg: dict, snap: Snapshot, refs: list[str]) -> None:
    batch, size = {}, 0
    for ref in refs:
        data = snap.bytes_for(ref)
        if data is None:
            continue
        batch[ref] = base64.b64encode(data).decode("ascii")
        size += len(data)
        if size >= UPLOAD_BATCH:
            call(cfg, "POST", "/v1/connector/blobs", {"blobs": batch}, timeout=300)
            batch, size = {}, 0
    if batch:
        call(cfg, "POST", "/v1/connector/blobs", {"blobs": batch}, timeout=300)


# ------------------------------------------------------------------ applying changes from the server
def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def apply_ops(cfg: dict, ops: list[dict]) -> int:
    roots = [Path(f) for f in cfg.get("folders", [])] + [claude_projects_dir()]
    n = 0
    for op in ops or []:
        p = Path(op.get("path", ""))
        if not p.is_absolute() or not any(_inside(p, r) for r in roots):
            continue  # never touch anything outside guarded folders and Claude Code's session folder
        try:
            if op.get("op") == "write":
                data = base64.b64decode(op.get("data_b64", ""))
                p.parent.mkdir(parents=True, exist_ok=True)
                if not (p.exists() and p.read_bytes() == data):
                    p.write_bytes(data)
                n += 1
            elif op.get("op") == "delete" and p.is_file():
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


# ------------------------------------------------------------------ hook events
def send_event(cfg: dict, ev: dict, project_dir: str | None) -> dict:
    ev["antibody_remote"] = True
    ev["antibody_project_dir"] = project_dir or ev.get("cwd")
    snap = None
    if ev.get("hook_event_name") in ("UserPromptSubmit", "Stop", "SessionStart") and ev["antibody_project_dir"]:
        snap = Snapshot(ev["antibody_project_dir"], ev.get("transcript_path"))
        ev["antibody_snapshot"] = snap.body
    long = ev.get("hook_event_name") == "UserPromptSubmit"
    path = f"/v1/hooks/claude-code?project_id={cfg['project_id']}"
    for _ in range(4):
        res = call(cfg, "POST", path, ev, timeout=1790 if long else 25)
        if res.get("need_blobs") and snap:
            upload(cfg, snap, res["need_blobs"])
            continue
        apply_ops(cfg, res.get("apply") or [])
        return res
    return {"stdout": None, "exit": 0}


def hook_main(args: list[str]) -> None:
    raw = sys.stdin.buffer.read()
    try:
        ev = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        sys.exit(0)
    cfg = load_config()
    folder = args[args.index("--dir") + 1] if "--dir" in args else None
    if not cfg.get("token"):
        _unreachable(ev, "this computer is not connected (run the connector's connect step)")
    try:
        res = send_event(cfg, ev, folder)
    except urllib.error.HTTPError as e:
        _unreachable(ev, f"HTTP {e.code}")
        return
    except (urllib.error.URLError, OSError, ValueError) as e:
        _unreachable(ev, type(e).__name__)
        return
    out = res.get("stdout")
    if out:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.flush()
    sys.exit(int(res.get("exit", 0)))


def _unreachable(ev: dict, why: str) -> None:
    mode = os.environ.get("ANTIBODY_FAIL_MODE", "ask").lower()
    name = ev.get("hook_event_name")
    if name == "UserPromptSubmit" and str(ev.get("prompt", "")).lower().startswith(("ab:", "/ab ")):
        sys.stdout.write(json.dumps({"decision": "block", "reason": f"ANTIBODY is not reachable ({why})."}))
        sys.exit(0)
    if mode == "open" or name != "PreToolUse" or ev.get("tool_name") in READ_ONLY:
        sys.exit(0)
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny" if mode == "closed" else "ask",
                                                        "permissionDecisionReason": f"ANTIBODY could not check this call ({why})."}}))
    sys.exit(0)


# ------------------------------------------------------------------ setup commands
def connect(code: str) -> dict:
    pad = "=" * (-len(code.strip()) % 4)
    info = json.loads(base64.urlsafe_b64decode(code.strip() + pad).decode("utf-8"))
    cfg = load_config()
    cfg.update({"server": info["server"].rstrip("/"), "token": info["token"], "name": platform.node() or "computer"})
    me = call(cfg, "GET", "/v1/connector/whoami")
    cfg["project_id"] = me["project_id"]
    cfg["user"] = me.get("user")
    cfg.setdefault("folders", [])
    save_config(cfg)
    return me


def guard(folder: str) -> dict:
    cfg = load_config()
    if not cfg.get("token"):
        raise SystemExit("Not connected yet. Run: antibody_connector.py connect <code>")
    pdir = Path(folder).expanduser().resolve()
    pdir.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    hook = str(HERE / "claude_hook.py")
    cmd = {"type": "command", "command": py, "args": [hook, "--connector", "--dir", str(pdir)], "timeout": 30}
    long_cmd = dict(cmd, timeout=1800)
    sp = pdir / ".claude" / "settings.local.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    hooks = data.setdefault("hooks", {})
    for event, matcher in (("SessionStart", None), ("UserPromptSubmit", None), ("PreToolUse", "*"), ("PostToolUse", "*"),
                           ("PostToolUseFailure", "*"), ("Stop", None), ("SessionEnd", None)):
        groups = [g for g in hooks.get(event, []) if not any("claude_hook.py" in " ".join([h.get("command", "")] + h.get("args", []))
                                                            for h in g.get("hooks", []))]
        group = {"hooks": [long_cmd if event == "UserPromptSubmit" else cmd]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        hooks[event] = groups
    sp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if str(pdir) not in cfg["folders"]:
        cfg["folders"].append(str(pdir))
    save_config(cfg)
    res = call(cfg, "POST", "/v1/connector/folders", {"project_dir": str(pdir), "os": platform.system(),
                                                      "claude_projects_dir": str(claude_projects_dir())})
    # first snapshot, so the website shows the folder right away
    snap = Snapshot(str(pdir))
    send_event(cfg, {"hook_event_name": "Stop", "session_id": f"connector-{res['agent_id']}", "cwd": str(pdir)}, str(pdir))
    return {"folder": str(pdir), "agent": res.get("name"), "files": len(snap.body["files"])}


def demo(name: str = "judge") -> dict:
    folder = Path.home() / "Documents" / f"antibody-demo-{name}"
    if not folder.exists():
        tpl = REPO / "fixtures" / "demo_env"
        for f in tpl.rglob("*"):
            if f.is_file():
                dst = folder / f.relative_to(tpl)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
        (folder / "inbox").mkdir(parents=True, exist_ok=True)
        for n in ("vendor_update.html", "offsite_invite.ics"):
            shutil.copyfile(REPO / "fixtures" / "sources" / n, folder / "inbox" / n)
    return guard(str(folder))


def poll_once(cfg: dict, timeout: float = 20) -> list[dict]:
    """One round: apply queued changes, run any commands started from the website. Returns what ran."""
    res = call(cfg, "GET", f"/v1/connector/poll?timeout={timeout}&name={urllib.request.quote(cfg.get('name', 'computer'))}"
                           f"&os={platform.system()}", timeout=timeout + 20)
    apply_ops(cfg, res.get("apply") or [])
    ran = []
    for c in res.get("commands") or []:
        snap = Snapshot(c["project_dir"]) if c.get("project_dir") and Path(c["project_dir"]).exists() else None
        body = {"command_id": c["command_id"], "snapshot": snap.body if snap else None}
        out = {}
        for _ in range(4):
            out = call(cfg, "POST", "/v1/connector/run", body, timeout=1800)
            if out.get("need_blobs") and snap:
                upload(cfg, snap, out["need_blobs"])
                continue
            break
        n = apply_ops(cfg, out.get("apply") or [])
        print(f"  ran '{c['line']}' from the website ({n} change(s) applied)", flush=True)
        ran.append({"line": c["line"], "applied": n, "result": out.get("result")})
    return ran


def run_forever() -> None:
    cfg = load_config()
    if not cfg.get("token"):
        raise SystemExit("Not connected yet.")
    print(f"Connected to {cfg['server']} as project {cfg['project_id']}. Leave this running; Ctrl+C to stop.", flush=True)
    backoff = 1
    while True:
        try:
            poll_once(cfg)
            backoff = 1
        except KeyboardInterrupt:
            return
        except Exception as e:  # network blips, server restarts
            print(f"  connection problem ({type(e).__name__}); retrying in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def main() -> None:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return
    cmd = a[0]
    if cmd == "connect":
        me = connect(a[1])
        print(f"Connected as {me.get('user') or me['project_id']} to {load_config()['server']}.")
    elif cmd == "guard":
        print(json.dumps(guard(a[1]), indent=1))
    elif cmd == "demo":
        print(json.dumps(demo(a[1] if len(a) > 1 else "judge"), indent=1))
    elif cmd == "run":
        run_forever()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
