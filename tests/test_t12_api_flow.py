"""T12 (R11): incident -> candidate -> tests -> accepted version is navigable through the API with live data."""
import base64

import pytest
from fastapi.testclient import TestClient

from conftest import FIX, PID
from server.api.app import create_app


@pytest.fixture
def client(core):
    app = create_app(core)
    c = TestClient(app, base_url="http://127.0.0.1")
    tok = c.post("/v1/session/bootstrap", json={"project_id": PID}).json()["token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


def test_full_evidence_chain(client):
    c = client
    assert c.get("/v1/health").json()["protection"] == "active"
    src = c.post("/v1/sources", json={"project_id": PID, "channel": "document", "filename": "q3_notes.md",
                                     "content_b64": base64.b64encode((FIX / "q3_notes.md").read_bytes()).decode()}).json()
    agent = c.post("/v1/agents", json={"project_id": PID, "name": "Agent A"}).json()
    t = c.post("/v1/tasks", json={"project_id": PID, "task": {"task_id": "task_report", "goal": "Q3 report", "tools": ["read_source", "write_report"],
                                                             "resources": {"source_ids": [src["source_id"]], "output_path": "reports/q3.md"}}}).json()
    assert t["revision"] == 1
    run = c.post("/v1/runs", json={"project_id": PID, "agent_id": agent["agent_id"], "task_ref": "task_report@1", "wait": True},
                 headers={"Idempotency-Key": "start-1"}).json()
    again = c.post("/v1/runs", json={"project_id": PID, "agent_id": agent["agent_id"], "task_ref": "task_report@1", "wait": True},
                   headers={"Idempotency-Key": "start-1"}).json()
    assert again["run_id"] == run["run_id"]
    conflict = c.post("/v1/runs", json={"project_id": PID, "agent_id": agent["agent_id"], "task_ref": "task_report@1", "wait": False},
                      headers={"Idempotency-Key": "start-1"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    view = c.get(f"/v1/runs/{run['run_id']}").json()
    assert view["mode"] == "simulated" and view["status_reason"] == "RECOVERED_IN_BRANCH"
    ev = c.get(f"/v1/runs/{run['run_id']}/events").json()["events"]
    assert ev[0]["kind"] == "run.started" and any(e["kind"] == "incident.opened" for e in ev)
    inc_id = next(e["payload"]["incident_id"] for e in ev if e["kind"] == "incident.opened")
    inc = c.get(f"/v1/incidents/{inc_id}").json()
    assert inc["presentation"]["facts"] and inc["candidate"]["status"] == "Draft" and inc["recovery_run"]["status"] == "completed"
    assert inc["evidence"]["events"]
    cand = inc["candidate"]["candidate_id"]
    assert c.post(f"/v1/candidates/{cand}/promote", json={"project_id": PID}).status_code == 409  # needs a passing evaluation first
    e = c.post(f"/v1/candidates/{cand}/evaluate", json={"project_id": PID, "wait": True}).json()
    assert e["status"] == "passed"
    assert c.get(f"/v1/evaluations/{e['evaluation_id']}").json()["results"]
    prom = c.post(f"/v1/candidates/{cand}/promote", json={"project_id": PID}).json()
    vid = prom["version"]["version_id"]
    versions = c.get("/v1/versions", params={"project_id": PID}).json()
    assert any(v["version_id"] == vid and prom["lesson"]["ref"] in v["lesson_revisions"] for v in versions)
    parent = prom["version"]["parents"][0]
    diff = c.get("/v1/compare", params={"project_id": PID, "a": parent, "b": vid}).json()
    assert diff["summary"][0]["text"].startswith("Added procedure") and diff["summary"][0]["path"].startswith("lessons.")
    lessons = c.get("/v1/lessons", params={"project_id": PID}).json()
    assert lessons[0]["evaluation_id"] == e["evaluation_id"] and lessons[0]["acceptance_mode"] == "assisted"
    reg = c.get("/v1/registry", params={"project_id": PID}).json()
    assert reg["revision"] == 1 and reg["records"][0]["ref"] == prom["lesson"]["ref"]
    prev = c.get(f"/v1/versions/{vid}/restore-preview", params={"project_id": PID}).json()
    assert prev["message"].startswith("Restores supported agent state")
    exp = c.get("/v1/export", params={"project_id": PID}).json()
    assert exp["honesty"]["run_modes"]["simulated"] >= 2


def test_auth_scoping_and_errors(client, core):
    c = client
    r = c.get("/v1/runs", params={"project_id": PID}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    core.projects.create_project("other", "Other", "o")
    assert c.get("/v1/runs", params={"project_id": "other"}).status_code == 403
    assert c.post("/v1/projects", json={"project_id": "p3", "name": "x", "owner": "o"}).status_code == 403
    bad = c.post("/v1/tasks", json={"project_id": PID, "task": {"task_id": "t", "goal": "g", "tools": [], "admin": True}})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "MALFORMED_REQUEST"
    # app tokens cannot act as the runtime worker
    assert c.post("/v1/runs/run_x/actions", json={"action_id": "a", "operation_id": "o", "tool_id": "x", "arguments": {}}).status_code in (403, 404)
    assert c.get("/v1/versions/sha256:nope/restore-preview", params={"project_id": PID}).status_code == 404
    host = TestClient(c.app, base_url="http://evil.example")
    assert host.get("/v1/health").status_code == 400  # host allowlist


def test_sse_stream_replays_after_cursor(client, core):
    c = client
    src = c.post("/v1/sources", json={"project_id": PID, "channel": "document", "filename": "a.md", "content_text": "plain"}).json()
    agent = c.post("/v1/agents", json={"project_id": PID, "name": "A", "config": {"environment": {"export_adapter": "fixture_2"}}}).json()
    c.post("/v1/tasks", json={"project_id": PID, "task": {"task_id": "t", "goal": "g", "tools": ["read_source", "write_report"],
                                                         "resources": {"source_ids": [src["source_id"]], "output_path": "r.md"}}})
    run = c.post("/v1/runs", json={"project_id": PID, "agent_id": agent["agent_id"], "task_ref": "t", "wait": True}).json()
    total = len(c.get(f"/v1/runs/{run['run_id']}/events").json()["events"])
    with c.stream("GET", f"/v1/runs/{run['run_id']}/events", params={"stream": True, "idle_timeout": 1}, headers={"Last-Event-ID": str(total - 2)}) as s:
        got = []
        for line in s.iter_lines():
            if line.startswith("id: "):
                got.append(int(line[4:]))
    assert got == [total - 1, total]
