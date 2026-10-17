"""What `start-antibody` runs after it has made sure Python and the packages are there.

Sets ANTIBODY up on first run, creates a fresh demo (a website project plus a guarded practice
folder for Claude Code), starts the server and opens the browser. Running it again just starts
ANTIBODY; your data is kept in var/.

Environment overrides: API_PORT (default 8765), ANTIBODY_DEMO_NAME (default "judge"),
ANTIBODY_DEMO_DIR (default ~/Documents/antibody-demo-<name>), ANTIBODY_NO_BROWSER=1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("API_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
NAME = os.environ.get("ANTIBODY_DEMO_NAME", "judge")
PID = f"demo_{NAME.replace('-', '_')}"
DEMO_DIR = Path(os.environ.get("ANTIBODY_DEMO_DIR") or Path.home() / "Documents" / f"antibody-demo-{NAME}")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def healthy() -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{URL}/v1/health", timeout=2) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "server.cli", *args], cwd=ROOT, capture_output=True, text=True,
                          env=dict(os.environ, API_PORT=str(PORT)))


def open_browser() -> None:
    target = f"{URL}/?project={PID}#/"
    if os.environ.get("ANTIBODY_NO_BROWSER"):
        say(f"Open {target} in your browser.")
        return
    try:
        webbrowser.open(target)
    except Exception:
        say(f"Open {target} in your browser.")


def instructions() -> None:
    say("")
    say("=" * 70)
    say(f" ANTIBODY is running:   {URL}")
    say(f" Your demo project:     {PID}  (picked automatically in the browser)")
    say(f" Practice folder:       {DEMO_DIR}")
    say("")
    say(" Next:")
    say("  1. Website: Runs -> Start with 'Report assistant A' + 'task_vendor_report'.")
    say("  2. Claude Code: open the practice folder (desktop app Code tab, or run")
    say("     `claude` in a terminal there), type  ab: status  and follow its README.md.")
    say("  3. Optional: Settings -> Connect Claude to add an API key (needed for Chat")
    say("     and negotiated merges; everything else works without one).")
    say("")
    say(" Keep this window open while you use ANTIBODY. Close it (or Ctrl+C) to stop.")
    say("=" * 70)


def hosted() -> int | None:
    """Connect this computer to a hosted ANTIBODY (connect code from the website), or None to run locally."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import antibody_connector as ac
    cfg = ac.load_config()
    code = os.environ.get("ANTIBODY_CONNECT_CODE", "").strip()
    if not code and not cfg.get("token") and sys.stdin and sys.stdin.isatty():
        say("")
        say("To use the hosted ANTIBODY website: sign in there, open Settings -> Connect your computer,")
        say("create a connect code and paste it here. Or just press Enter to run ANTIBODY on this computer only.")
        try:
            code = input("Connect code: ").strip()
        except EOFError:
            code = ""
    if not code and not cfg.get("token"):
        return None
    try:
        if code:
            me = ac.connect(code)
            say(f"Connected to {ac.load_config()['server']} as {me.get('user') or me['project_id']}.")
        else:
            say(f"Using your connection to {cfg['server']} (delete {ac.CONFIG} to connect differently).")
        res = ac.demo(NAME)
        say(f"Practice folder (guarded): {res['folder']}")
    except Exception as e:
        say(f"Could not connect: {e}")
        say("Check the code (create a new one on the website) and that this computer can reach the internet.")
        return 1
    cfg = ac.load_config()
    say("")
    say("=" * 70)
    say(f" Website:          {cfg['server']}   (your workspace)")
    say(f" Practice folder:  {res['folder']}")
    say("")
    say(" Open the practice folder in Claude Code (desktop app Code tab, or run `claude`")
    say(" in a terminal there), type  ab: status  and follow its README.md. Everything")
    say(" shows up live on the website, and going back or merging there changes files here.")
    say("")
    say(" Keep this window open so the website can reach this computer. Ctrl+C to stop.")
    say("=" * 70)
    if not os.environ.get("ANTIBODY_NO_BROWSER"):
        try:
            webbrowser.open(cfg["server"] + "/#/claude-code")
        except Exception:
            pass
    try:
        ac.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    say("ANTIBODY launcher")
    h = hosted()
    if h is not None:
        return h
    if healthy():
        say(f"ANTIBODY is already running at {URL}.")
        open_browser()
        instructions()
        return 0
    say("Preparing ANTIBODY (the first run takes about a minute)...")
    r = cli("setup")
    if r.returncode != 0:
        say(r.stdout[-2000:] + r.stderr[-2000:])
        say("Setup failed. Please send the lines above to the ANTIBODY team.")
        return 1
    r = cli("demo-env", "--name", NAME, "--dir", str(DEMO_DIR))
    out = r.stdout + r.stderr
    if r.returncode != 0 and "already exists" in out:
        say(f"Using your existing demo project {PID}.")
    elif r.returncode != 0 and "not empty" in out:
        # the folder survived but ANTIBODY's data did not (for example a fresh download): rebuild the practice folder
        r = cli("demo-env", "--name", NAME, "--dir", str(DEMO_DIR), "--reset")
        if r.returncode != 0:
            say((r.stdout + r.stderr)[-2000:])
            return 1
        say(f"Rebuilt the practice folder {DEMO_DIR}.")
    elif r.returncode != 0:
        say(out[-2000:])
        say("Could not create the demo. Please send the lines above to the ANTIBODY team.")
        return 1
    else:
        say(f"Created demo project {PID} and practice folder {DEMO_DIR}.")
    say("Starting ANTIBODY...")
    server = subprocess.Popen([sys.executable, "-m", "server.cli", "serve"], cwd=ROOT, env=dict(os.environ, API_PORT=str(PORT)))
    for _ in range(120):
        if healthy() or server.poll() is not None:
            break
        time.sleep(0.5)
    if not healthy():
        say(f"ANTIBODY did not start (port {PORT} may be in use). Set API_PORT to another number and try again.")
        server.terminate()
        return 1
    open_browser()
    instructions()
    try:
        return server.wait()
    except KeyboardInterrupt:
        say("Stopping ANTIBODY...")
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        return 0


if __name__ == "__main__":
    sys.exit(main())
