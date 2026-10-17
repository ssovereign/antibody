"""ANTIBODY HTTP API (FastAPI).

Every endpoint authenticates a scoped service token and authorizes project
access (reads included). Mutations accept an Idempotency-Key; state changes
take expected_head / expected_revision. Errors return a stable code and a
readable message: 400 malformed, 401/403 auth, 404 missing, 409 stale or
conflicting, 422 incompatible manifest/capability, 503 protection or storage
unavailable.
"""
from __future__ import annotations


import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..canonical import dumps, hash_value, loads, utcnow
from ..config import ROOT, Config
from ..core import Core
from ..ingress.parsers import IngestError
from ..journal import row_to_event
from ..projects import AuthError, ConflictError, InvalidRequest, NotFound
from ..schemas import (ActionProposal, CandidateRevise, EvaluateRequest, ForkRequest, GrantCreate, MemoryEdit, MergeRequest,
                       PreferenceSet, ProjectCreate, PromoteRequest, RevokeRequest, RunStart, SourceImport, TaskCreate, VersionSave)
from ..storage.db import StorageUnavailable
from ..versions.store import diff_states
from ..telemetry import disk_status


def load_secrets(core: Core) -> dict:
    p = Path(core.config.SECRETS_PATH)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def create_app(core: Core | None = None) -> FastAPI:
    core = core or Core(start_workers=True)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        yield
        core.shutdown()  # safe shutdown barrier: stop dispatch, pause active runs

    app = FastAPI(title="ANTIBODY", version="1.0.0", description="Protected agent workspace with RECEIPT version history.", lifespan=lifespan)
    app.state.core = core
    cfg = core.config
    app.add_middleware(CORSMiddleware, allow_origins=[cfg.UI_ORIGIN, "http://localhost:5173", f"http://127.0.0.1:{cfg.API_PORT}",
                                                     f"http://localhost:{cfg.API_PORT}"] + list(cfg.EXTRA_UI_ORIGINS),
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"] + list(cfg.EXTRA_ALLOWED_HOSTS))

    # ------------------------------------------------------------ errors
    def err(status: int, code: str, message: str, detail: Any = None) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "detail": detail}})

    @app.exception_handler(AuthError)
    async def _auth(_, e: AuthError):
        return err(e.status, e.code, e.message)

    @app.exception_handler(ConflictError)
    async def _conflict(_, e: ConflictError):
        return err(409, e.code, e.message, e.detail)

    @app.exception_handler(NotFound)
    async def _nf(_, e: NotFound):
        return err(404, "NOT_FOUND", e.message)

    @app.exception_handler(InvalidRequest)
    async def _inv(_, e: InvalidRequest):
        return err(e.status, e.code, e.message)

    @app.exception_handler(IngestError)
    async def _ing(_, e: IngestError):
        return err(400, e.code, e.message)

    @app.exception_handler(StorageUnavailable)
    async def _su(_, e: StorageUnavailable):
        return err(503, "STORAGE_UNAVAILABLE", "Durable storage is unavailable; protection state is unknown and mutating actions are refused.")

    @app.exception_handler(RequestValidationError)
    async def _val(_, e: RequestValidationError):
        return err(400, "MALFORMED_REQUEST", "The request does not match the schema.", json.loads(json.dumps(e.errors(), default=str))[:10])

    @app.exception_handler(ValidationError)
    async def _val2(_, e: ValidationError):
        return err(400, "MALFORMED_REQUEST", "The request does not match the schema.", json.loads(json.dumps(e.errors(), default=str))[:10])

    # ------------------------------------------------------------ auth
    def principal(request: Request, authorization: str | None = Header(default=None)) -> dict:
        token = authorization.split(" ", 1)[1] if authorization and authorization.lower().startswith("bearer ") else None
        return core.projects.authenticate(token)

    def need(p: dict, project_id: str | None, *roles: str) -> None:
        core.projects.authorize(p, project_id, set(roles) or {"app"})

    def idem(p: dict, project_id: str, endpoint: str, key: str | None, body: Any, fn):
        if not key:
            return fn()
        h = hash_value(json.loads(json.dumps(body, default=str)))
        row = core.db.one("SELECT * FROM idempotency WHERE project_id=? AND idem_key=? AND endpoint=?", (project_id, key, endpoint))
        if row:
            if row["request_hash"] != h:
                raise ConflictError("IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was used with a different body")
            return JSONResponse(status_code=row["status_code"], content=loads(row["response_json"]))
        result = fn()
        content = json.loads(json.dumps(result, default=str))
        core.db.execute("INSERT OR IGNORE INTO idempotency VALUES (?,?,?,?,?,?,?)", (project_id, key, endpoint, h, 200, dumps(content), utcnow()))
        return content

    def run_project(run_id: str) -> str:
        return core.coordinator.run(run_id)["project_id"]

    # ------------------------------------------------------------ system
    @app.get("/v1/health")
    def health():
        try:
            integrity = core.db.integrity_check()
            ok = integrity == "ok"
        except Exception:
            ok = False
        disk = disk_status(core.config.OBJECT_STORE_ROOT)
        ok = ok and disk["ok"]
        return {"status": "ok" if ok else "degraded", "protection": "active" if ok else "unknown", "schema_version": core.db.schema_version(),
                "time": utcnow(), "disk": disk, "startup_recovery": core.recovery_report}

    @app.post("/v1/session/bootstrap")
    def bootstrap(request: Request, body: dict = Body(default={})):
        """Local mode only: hands the UI an app-scoped token for one project. Loopback clients only."""
        if cfg.PROJECT_AUTH_MODE != "local":
            raise AuthError(403, "BOOTSTRAP_DISABLED", "Bootstrap is only available in local mode.")
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "testclient", "localhost"):
            raise AuthError(403, "LOOPBACK_ONLY", "Bootstrap is restricted to loopback clients.")
        pid = body.get("project_id")
        projects = [p["project_id"] for p in core.projects.list_projects()]
        if not pid:
            pid = projects[0] if projects else None
        if not pid or pid not in projects:
            raise NotFound("project")
        token = core.projects.issue_token(pid, "app", "user:local")
        return {"token": token, "project_id": pid, "projects": projects, "role": "app"}

    @app.post("/v1/session/login")
    def login(body: dict = Body(...)):
        """Hosted (token) mode: the UI signs in with an access token from `python -m server.cli invite`."""
        tok = str(body.get("token", "")).strip()
        p = core.projects.authenticate(tok)
        if p["role"] not in ("app", "admin", "operator"):
            raise AuthError(403, "ROLE_FORBIDDEN", "This token cannot use the web app.")
        all_projects = [x["project_id"] for x in core.projects.list_projects()]
        projects = all_projects if p["project_id"] is None else [p["project_id"]]
        wanted = body.get("project_id")
        pid = wanted if wanted in projects else (projects[0] if projects else None)
        if not pid:
            raise NotFound("project")
        return {"token": tok, "project_id": pid, "projects": projects, "role": p["role"], "mode": cfg.PROJECT_AUTH_MODE}

    @app.get("/v1/capabilities")
    def capabilities(p=Depends(principal)):
        return {"adapters": core.coordinator.capability_profiles(),
                "integrations": [
                    {"name": "ics_import", "state": "supported", "note": "Imported ICS / normalized event JSON files"},
                    {"name": "google_calendar", "state": core.integration_state("google_calendar")["state"],
                     "note": "Live provider adapter; requires GOOGLE_CALENDAR_TOKEN and selected calendar IDs"},
                    {"name": "web_fetch", "state": "supported", "note": "Restricted public HTTPS fetch through the gateway"},
                    {"name": "log_files", "state": "supported", "note": "Text/JSONL files with persisted cursors"},
                    {"name": "shell", "state": "disabled", "note": "No isolated shell executor exists; unrestricted shell is disabled"},
                    {"name": "codex / cursor / letta", "state": "not_built", "note": "No native adapter has been built or tested"}]}

    @app.get("/v1/settings")
    def settings(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        from ..context.compiler import presets
        proj = core.projects.get_project(project_id)
        return {"config": {k: v for k, v in cfg.public_view().items() if not k.endswith("_PATH") and not k.endswith("_ROOT")},
                "context_presets": presets(cfg.MODEL_CONTEXT_LIMIT), "project_settings": proj["settings"],
                "policy": core.projects.policy(project_id), "registry_revision": core.registry.head(project_id)}

    # ------------------------------------------------------------ projects
    @app.post("/v1/projects")
    def create_project(body: ProjectCreate, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, None, "admin")
        return idem(p, body.project_id, "projects", idempotency_key, body.model_dump(),
                    lambda: core.projects.create_project(body.project_id, body.name, body.owner))

    @app.get("/v1/projects")
    def list_projects(p=Depends(principal)):
        if p["role"] == "admin":
            return core.projects.list_projects()
        return [core.projects.get_project(p["project_id"])]

    @app.get("/v1/projects/{project_id}")
    def project_overview(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        db = core.db
        return {"project": core.projects.get_project(project_id), "policy": core.projects.policy(project_id),
                "tools": core.projects.tools(project_id), "recipients": core.projects.recipients(project_id),
                "agents": core.coordinator.agents(project_id), "registry_revision": core.registry.head(project_id),
                "counts": {
                    "runs": db.scalar("SELECT COUNT(*) FROM runs WHERE project_id=?", (project_id,)),
                    "open_incidents": db.scalar("SELECT COUNT(*) FROM incidents WHERE project_id=? AND status IN ('open','recovering')", (project_id,)),
                    "accepted_lessons": len(core.registry.active(project_id)),
                    "denied_actions": db.scalar("SELECT COUNT(*) FROM decisions WHERE project_id=? AND outcome='DENY'", (project_id,)),
                    "denied_executor_invocations": db.scalar("SELECT COUNT(*) FROM executor_invocations i JOIN decisions d ON d.decision_id=i.decision_id WHERE d.project_id=? AND d.outcome!='ALLOW'", (project_id,)),
                    "unknown_effects": db.scalar("SELECT COUNT(*) FROM effects WHERE project_id=? AND state='UNKNOWN'", (project_id,)),
                    "versions": db.scalar("SELECT COUNT(*) FROM versions WHERE project_id=?", (project_id,)),
                    "early_interceptions": db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE project_id=? AND use_kind='early_interception'", (project_id,)),
                    "outbox_entries": core.executors.outbox.count(project_id),
                }}

    @app.patch("/v1/projects/{project_id}/settings")
    def update_settings(project_id: str, body: dict = Body(...), p=Depends(principal)):
        need(p, project_id, "app")
        return core.projects.update_settings(project_id, body, p["actor_id"])

    # ------------------------------------------------------------ tasks & grants
    @app.post("/v1/tasks")
    def create_task(body: TaskCreate, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")
        return idem(p, body.project_id, "tasks", idempotency_key, body.model_dump(),
                    lambda: core.projects.create_task_revision(body.project_id, body.task, p, body.expected_revision))

    @app.post("/v1/tasks/draft")
    def draft_task(body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app")
        return core.projects.suggest_task_draft(str(body.get("goal", "")))

    @app.get("/v1/tasks")
    def list_tasks(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        return core.projects.list_tasks(project_id)

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str, project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        return {"current": core.projects.get_task(project_id, task_id), "revisions": core.projects.task_revisions(project_id, task_id)}

    @app.post("/v1/grants")
    def create_grant(body: GrantCreate, p=Depends(principal)):
        need(p, body.project_id, "app")
        return core.projects.create_grant(body.project_id, p, body.tool_id, body.arguments, body.operation_class, body.resource_scope,
                                          body.expires_in_seconds, body.uses)

    # ------------------------------------------------------------ sources
    @app.post("/v1/sources")
    def import_source(body: SourceImport, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")

        def do():
            if body.url:
                res = core.executors.fetcher.fetch(body.url, None)
                return core.sources.ingest_bytes(body.project_id, "web", res.pop("body"), content_type=res["content_type"],
                                                 label=body.label or res["final_url"], extra_meta={k: res[k] for k in ("url", "final_url", "redirects", "sha256")})
            if body.content_b64 is not None:
                data = base64.b64decode(body.content_b64, validate=True)
            elif body.content_text is not None:
                data = body.content_text.encode("utf-8")
            else:
                raise InvalidRequest("NO_CONTENT", "provide content_text, content_b64 or url")
            return core.sources.ingest_bytes(body.project_id, body.channel, data, body.filename, body.content_type, label=body.label)
        return idem(p, body.project_id, "sources", idempotency_key, body.model_dump(), do)

    @app.get("/v1/sources")
    def list_sources(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return core.sources.list(project_id)

    @app.get("/v1/sources/{source_id}")
    def get_source(source_id: str, p=Depends(principal)):
        s = core.sources.get(source_id)
        if not s:
            raise NotFound("source")
        need(p, s["project_id"], "app", "operator")
        return {"source": s, "chunks": core.sources.chunks(source_id), "exclusions": core.sources.exclusions(source_id),
                "dispositions": core.db.all("SELECT * FROM dispositions WHERE source_id=? ORDER BY id", (source_id,)),
                "raw_available": bool(s["raw_ref"]) and core.objects.exists(s["project_id"], s["raw_ref"], restricted=True),
                "display_note": "Plain text only; markup is never rendered."}

    @app.get("/v1/sources/{source_id}/raw")
    def get_raw(source_id: str, p=Depends(principal)):
        s = core.sources.get(source_id)
        if not s:
            raise NotFound("source")
        need(p, s["project_id"], "operator")
        raw = core.sources.raw(source_id)
        if raw is None:
            return {"available": False, "note": "Raw evidence was purged or not retained."}
        return {"available": True, "text": raw.decode("utf-8", "replace"), "note": "Restricted evidence; displayed as plain text."}

    # ------------------------------------------------------------ agents, branches, memory
    @app.post("/v1/agents")
    def create_agent(body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app")
        allowed = {"instructions", "environment", "planner", "model"}
        cfg_in = {k: v for k, v in (body.get("config") or {}).items() if k in allowed}
        return core.coordinator.create_agent(body["project_id"], body["name"], body.get("adapter", "langgraph_reference"), cfg_in, p["actor_id"])

    @app.get("/v1/agents")
    def list_agents(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        return core.coordinator.agents(project_id)

    @app.get("/v1/branches")
    def list_branches(project_id: str, agent_id: str | None = None, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        out = []
        for b in core.versions.branches(project_id, agent_id):
            b["clean"] = core.versions.is_clean(b["branch_id"]) if b["head_version"] else False
            b["state"].pop("restored_cursor", None)
            out.append(b)
        return out

    @app.post("/v1/branches/{branch_id}/memory")
    def edit_memory(branch_id: str, body: MemoryEdit, p=Depends(principal)):
        need(p, body.project_id, "app")
        b = core.versions.branch(branch_id)
        if b["project_id"] != body.project_id:
            raise NotFound("branch")
        return core.versions.edit_memory(branch_id, body.key, body.value, body.delete, body.note, body.seeded_test_condition)

    @app.post("/v1/branches/{branch_id}/lessons")
    def branch_lessons(branch_id: str, body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app")
        return core.learning.disable_on_branch(branch_id, body["ref"], bool(body.get("disabled", True)))

    @app.post("/v1/preferences")
    def set_pref(body: PreferenceSet, p=Depends(principal)):
        need(p, body.project_id, "app")
        core.db.execute("INSERT OR REPLACE INTO preferences(project_id,scope,scope_id,key,value_json,confirmed_by,created_at) VALUES (?,?,?,?,?,?,?)",
                        (body.project_id, body.scope, body.scope_id, body.key, dumps(body.value), p["actor_id"], utcnow()))
        return {"ok": True}

    # ------------------------------------------------------------ runs
    @app.post("/v1/runs")
    def start_run(body: RunStart, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")

        def do():
            if body.resume_run_id:
                return core.coordinator.continue_run(body.resume_run_id, p["actor_id"], wait=body.wait,
                                                     context_budget_tokens=body.context_budget_tokens)
            return core.coordinator.start_run(body.project_id, body.agent_id, body.task_ref, base_version=body.base_version,
                                              branch_id=body.branch_id, adapter=body.adapter, context_budget_tokens=body.context_budget_tokens,
                                              mode=body.mode, actor=p["actor_id"], wait=body.wait)
        return idem(p, body.project_id, "runs", idempotency_key, body.model_dump(), do)

    @app.get("/v1/runs")
    def list_runs(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        return core.coordinator.runs(project_id)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, p=Depends(principal)):
        need(p, run_project(run_id), "app", "operator", "worker")
        return core.coordinator.run_view(run_id)

    @app.post("/v1/runs/{run_id}/actions")
    def submit_action(run_id: str, body: ActionProposal, p=Depends(principal)):
        need(p, run_project(run_id), "worker")
        return core.gateway.submit(run_id, body, p).model_dump()

    @app.post("/v1/runs/{run_id}/operations")
    def issue_operation(run_id: str, body: dict = Body(default={}), p=Depends(principal)):
        """Runtimes obtain action_id/operation_id from the coordinator, never from the model."""
        need(p, run_project(run_id), "worker")
        run = core.coordinator.run(run_id)
        a, o = core.coordinator.issue_operation(run, str(body.get("purpose", "action"))[:80])
        return {"action_id": a, "operation_id": o}

    @app.post("/v1/runs/{run_id}/pause")
    def pause_run(run_id: str, p=Depends(principal)):
        need(p, run_project(run_id), "app")
        return core.coordinator.pause(run_id, p["actor_id"])

    @app.post("/v1/runs/{run_id}/resume")
    def resume_run(run_id: str, body: dict = Body(default={}), p=Depends(principal)):
        need(p, run_project(run_id), "app")
        return core.coordinator.resume(run_id, p["actor_id"], wait=bool(body.get("wait")))

    @app.post("/v1/runs/{run_id}/continue")
    def continue_run(run_id: str, body: dict = Body(default={}), p=Depends(principal)):
        need(p, run_project(run_id), "app")
        return core.coordinator.continue_run(run_id, p["actor_id"], wait=bool(body.get("wait")))

    @app.get("/v1/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: int = 0, stream: bool = False, idle_timeout: float = 0,
                         last_event_id: str | None = Header(default=None), p=Depends(principal)):
        need(p, run_project(run_id), "app", "operator", "worker")
        cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after
        if not stream and "text/event-stream" not in request.headers.get("accept", ""):
            return {"run_id": run_id, "events": core.journal.events(run_id, cursor, 1000)}
        return StreamingResponse(_sse(request, lambda c: core.journal.events(run_id, c, 200), cursor, key="sequence", idle_timeout=idle_timeout),
                                 media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/v1/events")
    async def project_events(project_id: str, request: Request, after: str = "", stream: bool = False, p=Depends(principal)):
        need(p, project_id, "app", "operator")

        def fetch(c):
            rows = core.db.all("SELECT * FROM events WHERE project_id=? AND timestamp_utc>? ORDER BY timestamp_utc, sequence LIMIT 300", (project_id, c or ""))
            return [row_to_event(r) for r in rows]
        if not stream:
            return {"events": list(reversed(core.journal.project_events(project_id, limit=300)))}
        return StreamingResponse(_sse(request, fetch, after or utcnow(), key="timestamp_utc"), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    async def _sse(request: Request, fetch, cursor, key: str, idle_timeout: float = 0):
        loop = asyncio.get_event_loop()
        last_beat = last_event = loop.time()
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            if idle_timeout and loop.time() - last_event > idle_timeout:
                yield "event: stream.idle\ndata: {}\n\n"
                return
            evs = await asyncio.to_thread(fetch, cursor)
            if evs:
                last_event = loop.time()
            for e in evs:
                cursor = e[key]
                ident = e["sequence"] if key == "sequence" else e["event_id"]
                yield f"id: {ident}\nevent: {e['kind']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"
            now = asyncio.get_event_loop().time()
            if now - last_beat > 10:
                last_beat = now
                yield f": heartbeat {utcnow()}\n\n"
            if not evs:
                await asyncio.sleep(0.4)

    @app.get("/v1/runs/{run_id}/decisions")
    def run_decisions(run_id: str, p=Depends(principal)):
        need(p, run_project(run_id), "app", "operator")
        rows = core.db.all("SELECT d.*, a.args_redacted_json FROM decisions d JOIN actions a ON a.action_id=d.action_id WHERE d.run_id=? ORDER BY d.created_at", (run_id,))
        for r in rows:
            for k in ("evidence_refs_json", "checks_json", "audit_json", "args_redacted_json"):
                r[k.replace("_json", "")] = loads(r.pop(k), None)
            r["executor_invocations"] = core.db.scalar("SELECT COUNT(*) FROM executor_invocations WHERE decision_id=?", (r["decision_id"],))
        return rows

    @app.get("/v1/runs/{run_id}/graph")
    def run_graph(run_id: str, p=Depends(principal)):
        need(p, run_project(run_id), "app", "operator")
        return core.db.all("SELECT src_ref, dst_ref, kind, detail_json, created_at FROM edges WHERE run_id=? ORDER BY id", (run_id,))

    @app.get("/v1/runs/{run_id}/packets")
    def run_packets(run_id: str, p=Depends(principal)):
        need(p, run_project(run_id), "app", "operator")
        rows = core.db.all("SELECT * FROM context_packets WHERE run_id=? ORDER BY created_at", (run_id,))
        for r in rows:
            r["included"] = loads(r.pop("included_json"), [])
            r["omitted"] = loads(r.pop("omitted_json"), [])
        return rows

    @app.get("/v1/packets/{packet_id}")
    def get_packet(packet_id: str, p=Depends(principal)):
        r = core.db.one("SELECT * FROM context_packets WHERE packet_id=?", (packet_id,))
        if not r:
            raise NotFound("packet")
        need(p, r["project_id"], "app", "operator")
        return json.loads(core.objects.get(r["project_id"], r["packet_ref"]))

    # ------------------------------------------------------------ decisions, effects
    @app.get("/v1/decisions/{decision_id}")
    def get_decision(decision_id: str, p=Depends(principal)):
        d = core.db.one("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        if not d:
            raise NotFound("decision")
        need(p, d["project_id"], "app", "operator")
        a = core.db.one("SELECT * FROM actions WHERE action_id=?", (d["action_id"],))
        for k in ("evidence_refs_json", "checks_json", "audit_json"):
            d[k.replace("_json", "")] = loads(d.pop(k), None)
        d["arguments_redacted"] = loads(a["args_redacted_json"])
        d["executor_invocations"] = core.db.scalar("SELECT COUNT(*) FROM executor_invocations WHERE decision_id=?", (decision_id,))
        d["evidence"] = [_chunk_evidence(r) for r in d["evidence_refs"] or [] if ":chunk_" in r]
        d["effect"] = core.db.one("SELECT operation_id, state, external_ref FROM effects WHERE decision_id=?", (decision_id,))
        return d

    def _chunk_evidence(ref: str) -> dict:
        sid, _, idx = ref.partition(":chunk_")
        ch = core.db.one("SELECT c.*, s.origin, s.channel, s.label FROM chunks c JOIN sources s ON s.source_id=c.source_id WHERE c.source_id=? AND c.chunk_index=?", (sid, int(idx or 0)))
        if not ch:
            return {"ref": ref, "available": False}
        return {"ref": ref, "source_id": sid, "origin": ch["origin"], "channel": ch["channel"], "label": ch["label"],
                "offsets": [ch["start_offset"], ch["end_offset"]], "text": ch["text"],
                "dispositions": core.db.all("SELECT run_id, disposition, pattern_ref, reason, detail_json FROM dispositions WHERE source_id=? AND (chunk_index=? OR chunk_index IS NULL)", (sid, int(idx or 0)))}

    @app.get("/v1/effects")
    def list_effects(project_id: str, run_id: str | None = None, state: str | None = None, agent_id: str | None = None,
                     tool_id: str | None = None, effect_class: str | None = None, q: str | None = None,
                     include_children: bool = False, external_only: bool = False, p=Depends(principal)):
        """Search effects by run (optionally with the runs it spawned), agent, tool, kind, state or free text."""
        need(p, project_id, "app", "operator")
        return core.effects.list(project_id, run_id, state, agent_id, tool_id, effect_class, q, include_children, external_only)

    @app.get("/v1/runs/{run_id}/impact")
    def run_impact(run_id: str, include_children: bool = True, p=Depends(principal)):
        """Everything a run (and its child runs) did to the outside world, what was blocked, and how to mitigate it."""
        pid = run_project(run_id)
        need(p, pid, "app", "operator")
        return core.effects.impact(pid, run_id, include_children)

    @app.get("/v1/effects/{operation_id}")
    def get_effect(operation_id: str, project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return core.effects.get(project_id, operation_id)

    @app.post("/v1/effects/{operation_id}/reconcile")
    def reconcile(operation_id: str, body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app", "operator")
        if body.get("resolve_to"):
            return core.effects.operator_resolve(body["project_id"], operation_id, body["resolve_to"], p["actor_id"], body.get("note", ""))
        return core.effects.reconcile(body["project_id"], operation_id, p["actor_id"])

    @app.get("/v1/outbox")
    def outbox(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return {"entries": core.executors.outbox.entries(project_id), "label": "local outbox (test destination)"}

    # ------------------------------------------------------------ incidents & learning
    @app.get("/v1/incidents")
    def list_incidents(project_id: str, status: str | None = None, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return core.incidents.list(project_id, status)

    @app.get("/v1/incidents/{incident_id}")
    def get_incident(incident_id: str, p=Depends(principal)):
        inc = core.incidents.get(incident_id)
        if not inc:
            raise NotFound("incident")
        need(p, inc["project_id"], "app", "operator")
        ev = {"decisions": [], "chunks": [], "events": []}
        for r in inc["evidence_refs"]:
            if r.startswith("decision:"):
                ev["decisions"].append(get_decision(r.split(":", 1)[1], p))
            elif ":chunk_" in r:
                ev["chunks"].append(_chunk_evidence(r))
            elif r.startswith("event:"):
                e = core.db.one("SELECT * FROM events WHERE event_id=?", (r.split(":", 1)[1],))
                if e:
                    ev["events"].append(row_to_event(e))
        cand = core.learning.candidate(inc["candidate_id"]) if inc["candidate_id"] else None
        rec = core.coordinator.run_view(inc["recovery_run_id"]) if inc.get("recovery_run_id") else None
        return {"incident": inc, "evidence": ev, "candidate": cand, "recovery_run": rec,
                "presentation": {"facts": inc["observed"], "hypotheses": inc["hypotheses"], "attribution_level": inc["attribution_level"]}}

    @app.post("/v1/incidents/{incident_id}/counterfactual")
    def counterfactual(incident_id: str, body: dict = Body(default={}), p=Depends(principal)):
        """Controlled attribution: rerun in sandboxes with only the linked spans masked (bounded repeats)."""
        inc = core.incidents.get(incident_id)
        if not inc:
            raise NotFound("incident")
        need(p, inc["project_id"], "app")
        try:
            return core.attribution.counterfactual(incident_id, int(body.get("repeats", 2)))
        except ValueError as e:
            raise InvalidRequest("NOT_APPLICABLE", str(e))

    @app.get("/v1/candidates")
    def list_candidates(project_id: str, status: str | None = None, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return core.learning.candidates(project_id, status)

    @app.get("/v1/candidates/{candidate_id}")
    def get_candidate(candidate_id: str, revision: int | None = None, p=Depends(principal)):
        c = core.learning.candidate(candidate_id, revision)
        need(p, c["project_id"], "app", "operator")
        return c

    @app.post("/v1/candidates")
    def create_candidate(body: dict = Body(...), p=Depends(principal)):
        """Manual candidates (e.g. a user-proposed memory fact or a seeded test condition) still need evaluation."""
        need(p, body.get("project_id"), "app")
        kind = body.get("kind")
        if kind not in ("procedure", "threat_pattern", "memory_fact"):
            raise InvalidRequest("UNSUPPORTED_KIND", "manual candidates may be procedure, threat_pattern or memory_fact")
        b = dict(body.get("body") or {})
        if body.get("seeded_test_condition"):
            b["seeded_test_condition"] = True
        return core.learning.create_candidate(body["project_id"], kind, body["lesson_id"], b, p["actor_id"])

    @app.post("/v1/candidates/{candidate_id}/revise")
    def revise(candidate_id: str, body: CandidateRevise, p=Depends(principal)):
        need(p, body.project_id, "app")
        return core.learning.revise_candidate(candidate_id, body.model_dump(exclude={"project_id"}), p["actor_id"])

    @app.post("/v1/candidates/{candidate_id}/evaluate")
    def evaluate(candidate_id: str, body: EvaluateRequest, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")
        c = core.learning.candidate(candidate_id)
        if c["project_id"] != body.project_id:
            raise NotFound("candidate")
        if body.wait:
            return idem(p, body.project_id, "evaluate", idempotency_key, {"c": candidate_id, **body.model_dump()},
                        lambda: core.learning.evaluate(body.project_id, candidate_id, body.suite_revision, p["actor_id"]))
        return idem(p, body.project_id, "evaluate", idempotency_key, {"c": candidate_id, **body.model_dump()},
                    lambda: {"job_id": core.jobs.enqueue(body.project_id, "evaluate", {"project_id": body.project_id, "candidate_id": candidate_id,
                                                                                      "suite": body.suite_revision, "actor": p["actor_id"]})})

    @app.post("/v1/candidates/{candidate_id}/promote")
    def promote(candidate_id: str, body: PromoteRequest, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")
        if body.mode == "automatic":
            raise InvalidRequest("AUTOMATIC_NOT_MANUAL", "automatic acceptance happens only through owner-enabled classes after evaluation")
        return idem(p, body.project_id, "promote", idempotency_key, {"c": candidate_id, **body.model_dump()},
                    lambda: core.learning.promote(body.project_id, candidate_id, body.expected_registry_revision, body.target_branch,
                                                  body.expected_head, body.mode, p["actor_id"]))

    @app.get("/v1/lessons")
    def lessons(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        return core.registry.all_lessons(project_id)

    @app.post("/v1/lessons/{lesson_id}/revoke")
    def revoke(lesson_id: str, body: RevokeRequest, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")
        return idem(p, body.project_id, "revoke", idempotency_key, {"l": lesson_id, **body.model_dump()},
                    lambda: core.learning.revoke(body.project_id, lesson_id, body.revision, body.reason, p["actor_id"]))

    @app.get("/v1/registry")
    def registry(project_id: str, since: int = 0, p=Depends(principal)):
        need(p, project_id, "app", "operator", "worker")
        snap = core.registry.snapshot(project_id, since)
        snap["acknowledgements"] = core.registry.acks(project_id)[:100]
        return snap

    # ------------------------------------------------------------ evaluation
    @app.get("/v1/suites")
    def suites(p=Depends(principal)):
        return core.evaluator.suites()

    @app.get("/v1/evaluations")
    def evaluations(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return core.evaluator.evaluations(project_id)

    @app.get("/v1/evaluations/{evaluation_id}")
    def evaluation(evaluation_id: str, p=Depends(principal)):
        e = core.evaluator.evaluation(evaluation_id)
        need(p, e["project_id"], "app", "operator")
        return e

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str, p=Depends(principal)):
        j = core.jobs.get(job_id)
        if not j:
            raise NotFound("job")
        need(p, j["project_id"], "app", "operator", "worker")
        return j

    @app.get("/v1/jobs")
    def jobs(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return core.jobs.list(project_id)

    # ------------------------------------------------------------ versions
    @app.post("/v1/versions")
    def save_version(body: VersionSave, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")
        return idem(p, body.project_id, "versions", idempotency_key, body.model_dump(),
                    lambda: core.versions.save(body.project_id, body.branch_id, body.expected_head, body.label, p["actor_id"]))

    @app.get("/v1/versions")
    def list_versions(project_id: str, agent_id: str | None = None, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        out = []
        for v in core.versions.versions(project_id, agent_id):
            m = v["manifest"]
            out.append({"version_id": v["version_id"], "branch_id": v["branch_id"], "parents": v["parents"], "label": v["label"],
                        "created_at": v["created_at"], "requires_reconciliation": bool(v["requires_reconciliation"]),
                        "lesson_revisions": m["lesson_revisions"], "registry_revision": m["registry_revision"],
                        "task_revision_ref": m["task_revision_ref"]})
        return out

    @app.get("/v1/versions/{version_id}")
    def get_version(version_id: str, p=Depends(principal)):
        v = core.versions.version(version_id)
        need(p, v["project_id"], "app", "operator")
        st = core.versions.manifest_state(v["project_id"], v["manifest"])
        st.pop("adapter_state", None)
        return {"version": v, "state": st}

    @app.get("/v1/versions/{version_id}/restore-preview")
    def restore_preview(version_id: str, project_id: str, p=Depends(principal)):
        need(p, project_id, "app")
        return core.versions.preview_restore(project_id, version_id)

    @app.post("/v1/versions/{version_id}/fork")
    def fork(version_id: str, body: ForkRequest, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")
        return idem(p, body.project_id, "fork", idempotency_key, {"v": version_id, **body.model_dump()},
                    lambda: core.versions.fork(body.project_id, version_id, body.name, body.purpose, p["actor_id"]))

    @app.get("/v1/compare")
    def compare(project_id: str, a: str, b: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        if b.startswith("working:"):
            va = core.versions.version(a)
            sa = core.versions.manifest_state(project_id, va["manifest"])
            sb = core.versions.working_state(b.split(":", 1)[1])
            return diff_states(core.versions, project_id, sa, sb)
        return core.versions.compare(project_id, a, b)

    @app.post("/v1/merges")
    def merge(body: MergeRequest, p=Depends(principal), idempotency_key: str | None = Header(default=None)):
        need(p, body.project_id, "app")
        return idem(p, body.project_id, "merges", idempotency_key, body.model_dump(),
                    lambda: core.merges.merge(body.project_id, body.target_branch, body.expected_head, body.source_version,
                                              body.selected_changes, body.resolutions, body.suite_revision, p["actor_id"]))

    @app.post("/v1/gc")
    def gc(body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "operator")
        return core.versions.gc(body["project_id"], dry_run=bool(body.get("dry_run", True)))

    # ------------------------------------------------------------ Claude connection
    @app.get("/v1/integrations/claude")
    def claude_status(p=Depends(principal)):
        """Connection status. Never returns the key itself (only its last four characters)."""
        return core.claude.status()

    @app.post("/v1/integrations/claude/key")
    def claude_key(body: dict = Body(...), p=Depends(principal)):
        need(p, None, "app", "operator")
        try:
            core.claude.save_key(str(body.get("api_key", "")))
        except ValueError as e:
            raise InvalidRequest("INVALID_KEY", str(e))
        core.refresh_auditor()
        return core.claude.status()

    @app.delete("/v1/integrations/claude/key")
    def claude_key_clear(p=Depends(principal)):
        need(p, None, "app", "operator")
        core.claude.clear_key()
        core.refresh_auditor()
        return core.claude.status()

    @app.patch("/v1/integrations/claude")
    def claude_settings(body: dict = Body(...), p=Depends(principal)):
        need(p, None, "app", "operator")
        try:
            core.claude.update_settings(body)
        except ValueError as e:
            raise InvalidRequest("INVALID_SETTING", str(e))
        core.refresh_auditor()
        return core.claude.status()

    @app.post("/v1/integrations/claude/test")
    def claude_test(p=Depends(principal)):
        need(p, None, "app", "operator")
        return core.claude.test()

    @app.get("/v1/integrations/mcp")
    def mcp_snippet(project_id: str, p=Depends(principal)):
        """Configuration for connecting Claude Code or Claude Desktop to ANTIBODY's protected tools over MCP."""
        need(p, project_id, "app", "operator")
        import sys as _sys
        py = str(Path(_sys.executable))
        script = str(ROOT / "scripts" / "mcp_serve.py")
        args = [script, "--project", project_id, "--agent", "Claude Code (via MCP)", "--task", "mcp_tools"]
        return {"command": py, "args": args,
                "claude_code_cli": "claude mcp add antibody -- " + " ".join(f'"{a}"' if " " in a else a for a in [py] + args),
                "mcp_json": {"mcpServers": {"antibody": {"command": py, "args": args}}},
                "task_id": "mcp_tools",
                "note": "The first start creates a 'Claude Code (via MCP)' agent and an 'mcp_tools' assignment (read and write files only). "
                        "Edit that assignment in Assignments to widen or narrow what the connected agent may do."}

    # ------------------------------------------------------------ Claude Code guard (hooks)
    @app.post("/v1/hooks/claude-code")
    def claude_code_hook(project_id: str, body: dict = Body(...), p=Depends(principal)):
        """One Claude Code hook event (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SessionEnd).
        Returns what the hook script should print and its exit code."""
        need(p, project_id, "app", "operator")
        return core.claude_code.handle(project_id, body)

    @app.get("/v1/integrations/claude-code")
    def claude_code_status(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        rows = []
        for a in core.coordinator.agents(project_id):
            if a["adapter"] != "claude_code_hooks":
                continue
            pdir = a["config"]["project_dir"]
            sp = Path(pdir) / ".claude" / "settings.local.json"
            installed = sp.exists() and "claude_hook.py" in sp.read_text(encoding="utf-8", errors="ignore")
            last = core.db.one("SELECT session_id, last_seen, run_id FROM host_sessions WHERE agent_id=? ORDER BY last_seen DESC LIMIT 1", (a["agent_id"],))
            counts = core.db.one("SELECT COUNT(*) AS n, SUM(outcome='DENY') AS denied, SUM(outcome='PAUSE') AS asked FROM host_tool_calls h "
                                 "JOIN runs r ON r.run_id=h.run_id WHERE r.agent_id=?", (a["agent_id"],))
            rows.append({"agent_id": a["agent_id"], "name": a["name"], "project_dir": pdir, "installed": installed, "last_session": last,
                         "calls": counts["n"] or 0, "denied": counts["denied"] or 0, "asked": counts["asked"] or 0})
        settings = core.projects.get_project(project_id)["settings"]
        return {"projects": rows, "python": sys.executable, "auto_accept": settings.get("claude_code_auto_accept", True)}

    @app.post("/v1/integrations/claude-code/install")
    def claude_code_install(body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app", "operator")
        return core.claude_code.install(body["project_id"], body["project_dir"], sys.executable, str(ROOT))

    @app.post("/v1/integrations/claude-code/uninstall")
    def claude_code_uninstall(body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app", "operator")
        return core.claude_code.uninstall(body["project_dir"])

    # ------------------------------------------------------------ Claude Code history tree (website)
    def _cc_agent(agent_id: str, p, *roles):
        a = core.coordinator.agent(agent_id)
        need(p, a["project_id"], *(roles or ("app", "operator")))
        if a["adapter"] != "claude_code_hooks":
            raise InvalidRequest("NOT_CLAUDE_CODE", "this agent is not a Claude Code project")
        return a

    @app.get("/v1/claude-code/agents/{agent_id}/tree")
    def cc_tree(agent_id: str, p=Depends(principal)):
        """Branches and checkpoints of one Claude Code project, for the history graph."""
        from .. import cc_state as ccs
        a = _cc_agent(agent_id, p)
        cur = core.claude_code._branch_id(a)
        branches = [{k: b[k] for k in ("branch_id", "name", "parent_branch_id", "base_version", "head_version", "purpose", "created_at")}
                    for b in core.versions.branches(a["project_id"], agent_id)]
        versions = []
        for v in core.db.all("SELECT v.version_id, v.branch_id, v.parents_json, v.label, v.created_at, v.manifest_ref FROM versions v "
                             "JOIN branches b ON b.branch_id=v.branch_id WHERE b.agent_id=? ORDER BY v.created_at", (agent_id,)):
            man = core.objects.get_json(a["project_id"], v["manifest_ref"])
            files = core.objects.get_json(a["project_id"], man["artifact_tree_ref"])["files"]
            versions.append({"version_id": v["version_id"], "branch_id": v["branch_id"], "parents": json.loads(v["parents_json"]),
                             "label": v["label"], "created_at": v["created_at"], "has_conversation": ccs.META in files,
                             "is_merge": ccs.MERGE_RECORD in files,
                             "files": sum(1 for k in files if not k.startswith("_claude_session/"))})
        lessons = [{"ref": l["ref"], "summary": l["body"]["summary"], "fix": l["body"]["fix"], "tool": l["body"].get("tool")}
                   for l in core.cc_learning.lessons(a["project_id"], a)]
        failures = core.db.scalar("SELECT COUNT(*) FROM host_failures WHERE agent_id=?", (agent_id,))
        prevented = core.db.scalar("SELECT COUNT(*) FROM lesson_uses u JOIN runs r ON r.run_id=u.run_id WHERE r.agent_id=? AND u.use_kind='prevented_failure'", (agent_id,))
        return {"agent": {"agent_id": agent_id, "name": a["name"], "project_dir": a["config"]["project_dir"]}, "current_branch_id": cur,
                "branches": branches, "versions": versions, "lessons": lessons, "failures_seen": failures, "failures_prevented": prevented}

    @app.get("/v1/claude-code/agents/{agent_id}/versions/{version_id}")
    def cc_version(agent_id: str, version_id: str, p=Depends(principal)):
        from .. import cc_state as ccs
        a = _cc_agent(agent_id, p)
        v = core.versions.version(version_id)
        files = core.versions.manifest_state(a["project_id"], v["manifest"])["files"]
        ch, meta = core.claude_code.state_chain(a["project_id"], version_id)
        record = json.loads(core.objects.get(a["project_id"], files[ccs.MERGE_RECORD]["blob"])) if ccs.MERGE_RECORD in files else None
        return {"version_id": version_id, "label": core.db.scalar("SELECT label FROM versions WHERE version_id=?", (version_id,)),
                "conversation": ccs.summarize(ch, 30), "messages": len(ch),
                "files": sorted(k for k in files if not k.startswith("_claude_session/")),
                "memory": sorted(k[len("_claude_session/memory/"):] for k in files if k.startswith("_claude_session/memory/")),
                "merge_record": record}

    @app.post("/v1/claude-code/agents/{agent_id}/command")
    def cc_command(agent_id: str, body: dict = Body(...), p=Depends(principal)):
        """Run a history command (restore, switch, merge, branch, save, diff, ...) on the project's current branch."""
        a = _cc_agent(agent_id, p, "app")
        return core.commands.run(a["project_id"], agent_id, core.claude_code._branch_id(a), str(body.get("line", "")), p["actor_id"])

    @app.put("/v1/claude-code/settings")
    def cc_settings(body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app", "operator")
        return core.projects.update_settings(body["project_id"], {"claude_code_auto_accept": bool(body.get("auto_accept", True))}, p["actor_id"])["settings"]

    # ------------------------------------------------------------ chat
    @app.get("/v1/chats")
    def list_chats(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        return core.chat.list(project_id)

    @app.post("/v1/chats")
    def create_chat(body: dict = Body(...), p=Depends(principal)):
        need(p, body.get("project_id"), "app")
        return core.chat.create(body["project_id"], p, body.get("title"), body.get("agent_id"), body.get("permissions"))

    @app.get("/v1/chats/{cid}")
    def get_chat(cid: str, p=Depends(principal)):
        c = core.chat.get(cid)
        need(p, c["project_id"], "app", "operator")
        return c

    @app.get("/v1/chats/{cid}/messages")
    def chat_messages(cid: str, after: int = 0, p=Depends(principal)):
        c = core.chat.get(cid)
        need(p, c["project_id"], "app", "operator")
        return {"messages": core.chat.messages(cid, after), "conversation": c}

    @app.post("/v1/chats/{cid}/messages")
    def post_chat(cid: str, body: dict = Body(...), p=Depends(principal)):
        c = core.chat.get(cid)
        need(p, c["project_id"], "app")
        return core.chat.post(cid, str(body.get("text", "")), p, wait=bool(body.get("wait")))

    @app.put("/v1/chats/{cid}/permissions")
    def chat_permissions(cid: str, body: dict = Body(...), p=Depends(principal)):
        c = core.chat.get(cid)
        need(p, c["project_id"], "app")
        return core.chat.set_permissions(cid, body, p)

    @app.post("/v1/commands")
    def run_command(body: dict = Body(...), p=Depends(principal)):
        """Git-style version history command for one agent (same commands as chat's /commands)."""
        need(p, body.get("project_id"), "app")
        agent_id = body.get("agent_id")
        if not agent_id and body.get("agent_name"):
            row = core.db.one("SELECT agent_id FROM agents WHERE project_id=? AND name=?", (body["project_id"], body["agent_name"]))
            if not row:
                raise NotFound(f"agent named {body['agent_name']}")
            agent_id = row["agent_id"]
        if not agent_id:
            raise InvalidRequest("AGENT_REQUIRED", "name an agent (agent_id or agent_name)")
        return core.commands.run(body["project_id"], agent_id, body.get("branch_id"), str(body.get("command", "")), p["actor_id"])

    # ------------------------------------------------------------ export
    @app.get("/v1/export")
    def export(project_id: str, p=Depends(principal)):
        need(p, project_id, "app", "operator")
        from ..export import build_export
        return build_export(core, project_id)

    # ------------------------------------------------------------ static web client
    dist = ROOT / "apps" / "web" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(dist / "index.html")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("v1/"):
                raise NotFound(path)
            f = dist / path
            if f.is_file():
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app
