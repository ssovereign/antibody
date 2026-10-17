"""Run ANTIBODY locally the way a hosted deployment runs it (token sign-in, separate data folder, port 8790).

    python scripts/hosted_preview.py            # then: python scripts/hosted_preview.py --invite alice
Useful for checking the hosted setup (sign-in screen, invites) before deploying.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ANTIBODY_DATA_DIR", str(ROOT / "var" / "hosted_preview"))
os.environ.setdefault("PROJECT_AUTH_MODE", "token")
os.environ.setdefault("API_PORT", "8790")


def cli(*args):
    return subprocess.run([sys.executable, "-m", "server.cli", *args], cwd=ROOT, env=os.environ, capture_output=True, text=True)


if "--invite" in sys.argv:
    print(cli("invite", "--project", "demo_hosted", "--name", sys.argv[sys.argv.index("--invite") + 1]).stdout)
    sys.exit(0)
cli("setup")
cli("demo-env", "--name", "hosted", "--dir", str(Path(os.environ["ANTIBODY_DATA_DIR"]) / "demo-folders" / "hosted"))
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from server.cli import main  # noqa: E402

main(["serve"])
