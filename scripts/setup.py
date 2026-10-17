"""`make setup`: create the venv, install pinned dependencies, build the web client, migrate.

Run with any Python 3.12: `python scripts/setup.py`.
"""
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd, cwd=ROOT):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call([str(c) for c in cmd], cwd=cwd, shell=(os.name == "nt" and str(cmd[0]) in ("npm", "npx")))


def main():
    if sys.version_info < (3, 11):
        sys.exit("Python 3.11+ is required (3.12 tested)")
    if not PY.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
    run([PY, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    run([PY, "-m", "pip", "install", "--quiet", "-r", ROOT / "requirements.lock"])
    web = ROOT / "apps" / "web"
    if shutil.which("npm"):
        run(["npm", "ci", "--no-audit", "--no-fund"], cwd=web)
        run(["npm", "run", "build"], cwd=web)
    else:
        print("npm not found: skipping the web client build (API still works; install Node 18+ for the UI)")
    run([PY, "-m", "server.cli", "setup"])
    print("\nSetup complete. Next: `make demo` then `make dev` and open http://127.0.0.1:8765")


if __name__ == "__main__":
    main()
