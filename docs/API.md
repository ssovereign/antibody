# ANTIBODY HTTP API (generated)

Generated from the running FastAPI application by `scripts/export_openapi.py`; do not edit by hand.

All endpoints except `/v1/health` and the loopback-only `/v1/session/bootstrap` require `Authorization: Bearer <token>` and authorize project access, reads included. Mutations accept `Idempotency-Key`. Errors: `{"error": {"code", "message", "detail"}}` with 400 / 401 / 403 / 404 / 409 / 422 / 503.

| Method | Path | Summary |
|---|---|---|
| POST | `/v1/agents` | Create Agent |
| GET | `/v1/agents` | List Agents |
| GET | `/v1/branches` | List Branches |
| POST | `/v1/branches/{branch_id}/lessons` | Branch Lessons |
| POST | `/v1/branches/{branch_id}/memory` | Edit Memory |
| GET | `/v1/candidates` | List Candidates |
| POST | `/v1/candidates` | Manual candidates (e.g. a user-proposed memory fact or a seeded test condition) still need evaluation. |
| GET | `/v1/candidates/{candidate_id}` | Get Candidate |
| POST | `/v1/candidates/{candidate_id}/evaluate` | Evaluate |
| POST | `/v1/candidates/{candidate_id}/promote` | Promote |
| POST | `/v1/candidates/{candidate_id}/revise` | Revise |
| GET | `/v1/capabilities` | Capabilities |
| GET | `/v1/compare` | Compare |
| GET | `/v1/decisions/{decision_id}` | Get Decision |
| GET | `/v1/effects` | Search effects by run (optionally with the runs it spawned), agent, tool, kind, state or free text. |
| GET | `/v1/effects/{operation_id}` | Get Effect |
| POST | `/v1/effects/{operation_id}/reconcile` | Reconcile |
| GET | `/v1/evaluations` | Evaluations |
| GET | `/v1/evaluations/{evaluation_id}` | Evaluation |
| GET | `/v1/events` | Project Events |
| GET | `/v1/export` | Export |
| POST | `/v1/gc` | Gc |
| POST | `/v1/grants` | Create Grant |
| GET | `/v1/health` | Health |
| GET | `/v1/incidents` | List Incidents |
| GET | `/v1/incidents/{incident_id}` | Get Incident |
| POST | `/v1/incidents/{incident_id}/counterfactual` | Controlled attribution: rerun in sandboxes with only the linked spans masked (bounded repeats). |
| GET | `/v1/jobs` | Jobs |
| GET | `/v1/jobs/{job_id}` | Job |
| GET | `/v1/lessons` | Lessons |
| POST | `/v1/lessons/{lesson_id}/revoke` | Revoke |
| POST | `/v1/merges` | Merge |
| GET | `/v1/outbox` | Outbox |
| GET | `/v1/packets/{packet_id}` | Get Packet |
| POST | `/v1/preferences` | Set Pref |
| POST | `/v1/projects` | Create Project |
| GET | `/v1/projects` | List Projects |
| GET | `/v1/projects/{project_id}` | Project Overview |
| PATCH | `/v1/projects/{project_id}/settings` | Update Settings |
| GET | `/v1/registry` | Registry |
| POST | `/v1/runs` | Start Run |
| GET | `/v1/runs` | List Runs |
| GET | `/v1/runs/{run_id}` | Get Run |
| POST | `/v1/runs/{run_id}/actions` | Submit Action |
| POST | `/v1/runs/{run_id}/continue` | Continue Run |
| GET | `/v1/runs/{run_id}/decisions` | Run Decisions |
| GET | `/v1/runs/{run_id}/events` | Run Events |
| GET | `/v1/runs/{run_id}/graph` | Run Graph |
| GET | `/v1/runs/{run_id}/impact` | Everything a run (and its child runs) did to the outside world, what was blocked, and how to mitigate it. |
| POST | `/v1/runs/{run_id}/operations` | Runtimes obtain action_id/operation_id from the coordinator, never from the model. |
| GET | `/v1/runs/{run_id}/packets` | Run Packets |
| POST | `/v1/runs/{run_id}/pause` | Pause Run |
| POST | `/v1/runs/{run_id}/resume` | Resume Run |
| POST | `/v1/session/bootstrap` | Local mode only: hands the UI an app-scoped token for one project. Loopback clients only. |
| GET | `/v1/settings` | Settings |
| POST | `/v1/sources` | Import Source |
| GET | `/v1/sources` | List Sources |
| GET | `/v1/sources/{source_id}` | Get Source |
| GET | `/v1/sources/{source_id}/raw` | Get Raw |
| GET | `/v1/suites` | Suites |
| POST | `/v1/tasks` | Create Task |
| GET | `/v1/tasks` | List Tasks |
| POST | `/v1/tasks/draft` | Draft Task |
| GET | `/v1/tasks/{task_id}` | Get Task |
| POST | `/v1/versions` | Save Version |
| GET | `/v1/versions` | List Versions |
| GET | `/v1/versions/{version_id}` | Get Version |
| POST | `/v1/versions/{version_id}/fork` | Fork |
| GET | `/v1/versions/{version_id}/restore-preview` | Restore Preview |
