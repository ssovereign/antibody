"""`make dev`: start the API + bounded worker pool, and the Vite dev client if available.

API (and the built UI): http://127.0.0.1:8765   Vite hot-reload UI: http://127.0.0.1:5173
Both bind to loopback only.
"""
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main():
    procs = [subprocess.Popen([str(PY if PY.exists() else sys.executable), "-m", "server.cli", "serve"], cwd=ROOT)]
    web = ROOT / "apps" / "web"
    if "--api-only" not in sys.argv and shutil.which("npm") and (web / "node_modules").exists():
        procs.append(subprocess.Popen("npm run dev", cwd=web, shell=True))
        print("UI (hot reload): http://127.0.0.1:5173   API + built UI: http://127.0.0.1:8765", flush=True)
    else:
        print("API + built UI: http://127.0.0.1:8765", flush=True)
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                if os.name == "nt":
                    p.terminate()
                else:
                    p.send_signal(signal.SIGINT)
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
