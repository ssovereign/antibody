import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.config import Config, ROOT
from server.core import Core
from server.schemas import TaskBody

tmp = tempfile.mkdtemp(prefix="ab_smoke_")
core = Core(Config.for_root(tmp))
core.propagate_runtime_errors = True
pid = "proj_demo"
core.projects.create_project(pid, "Demo", "owner")
actor = {"role": "app", "actor_id": "user:owner"}
src = core.sources.ingest_bytes(pid, "document", (ROOT / "fixtures/sources/q3_notes.md").read_bytes(), "q3_notes.md")
print("source", src["source_id"], [e["kind"] for e in core.sources.exclusions(src["source_id"])])
a = core.coordinator.create_agent(pid, "Agent A")
core.projects.create_task_revision(pid, TaskBody(task_id="task_report", goal="Create the Q3 report", tools=["read_source", "write_report"],
                                                 resources={"source_ids": [src["source_id"]], "output_path": "reports/q3 summary.md",
                                                            "required_phrases": ["Zürich", "→"]}), actor)
run = core.coordinator.start_run(pid, a["agent_id"], "task_report", wait=True)
print("run", run["status"], run["status_reason"], run["health"])
for e in core.journal.events(run["run_id"]):
    print(e["sequence"], e["kind"], json.dumps(e["payload"], ensure_ascii=False)[:160])
inc = core.incidents.list(pid)
print("incidents", [(i["category"], i["status"], i["candidate_id"], i["attribution_level"]) for i in inc])
cands = core.learning.candidates(pid)
print("candidates", [(c["candidate_id"], c["kind"], c["status"]) for c in cands])
core.close()
