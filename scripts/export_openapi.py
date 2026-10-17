"""Write docs/openapi.json and docs/API.md from the implemented FastAPI app, so the
documented API stays aligned with the code (`python scripts/export_openapi.py`)."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.api.app import create_app  # noqa: E402
from server.config import Config  # noqa: E402
from server.core import Core  # noqa: E402

app = create_app(Core(Config.for_root(tempfile.mkdtemp())))
spec = app.openapi()
(ROOT / "docs").mkdir(exist_ok=True)
(ROOT / "docs" / "openapi.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
lines = ["# ANTIBODY HTTP API (generated)", "",
         "Generated from the running FastAPI application by `scripts/export_openapi.py`; do not edit by hand.", "",
         "All endpoints except `/v1/health` and the loopback-only `/v1/session/bootstrap` require `Authorization: Bearer <token>` "
         "and authorize project access, reads included. Mutations accept `Idempotency-Key`. Errors: "
         "`{\"error\": {\"code\", \"message\", \"detail\"}}` with 400 / 401 / 403 / 404 / 409 / 422 / 503.", "",
         "| Method | Path | Summary |", "|---|---|---|"]
for path, ops in sorted(spec["paths"].items()):
    if not path.startswith("/v1"):
        continue
    for method, op in ops.items():
        doc = (op.get("description") or op.get("summary") or "").strip().split("\n")[0]
        lines.append(f"| {method.upper()} | `{path}` | {doc} |")
(ROOT / "docs" / "API.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"{sum(len(v) for k, v in spec['paths'].items() if k.startswith('/v1'))} operations documented")
