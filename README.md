# ANTIBODY

ANTIBODY is a protected workspace for AI agents. It checks every tool action before it runs, treats outside content as data, watches run health, learns tested fixes, shares accepted knowledge between agents, and keeps a Git-like history of agent state. **RECEIPT** is the version-history subsystem: save, branch, compare, keep selected changes, and restore without repeating external actions.

This repository implements the local developer application described in [`docs/ANTIBODY_Technical_Design.md`](docs/ANTIBODY_Technical_Design.md): a FastAPI service with a bounded worker pool, SQLite (WAL) storage plus a content-addressed object store, a LangGraph reference agent with a persistent checkpointer, an MCP tool proxy, and a React web client.

> **Honest labels.** No model API key was available while this was built. Agent runs therefore use a deterministic reference planner and are labeled **simulated**. One replayed trace is labeled **recorded**. The gateway, journal, executors, checks, learning, registry, evaluation and version operations all run real code against synthetic fixtures. Set `MODEL_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` to run a live Claude planner (implemented, not exercised here).

## Quick start

Requirements: Python 3.12 (3.11+), Node 18+ for the web client, and `make` (optional; every target has a plain command).

```bash
make setup
```
```bash
make demo
```
```bash
make dev
```

Then open http://127.0.0.1:8765 (built UI) or http://127.0.0.1:5173 (hot reload). Both bind to loopback only.

| Command | What it does | Without make |
|---|---|---|
| `make setup` | venv, pinned deps (`requirements.lock`), `npm ci` + build, migrations, server-side tokens | `python scripts/setup.py` |
| `make dev` | API + worker pool (+ Vite dev server) | `python scripts/dev.py` |
| `make test` | deterministic unit and integration tests (T01–T14) | `.venv/Scripts/python -m pytest` |
| `make test-security` | gateway, source and restore regressions | `pytest -m security` |
| `make eval` | frozen comparison: discovery → validation → untouched final split, baseline ladder | `python -m server.cli eval` |
| `make demo` / `make demo-reset` | seed / rebuild the labeled demonstration project | `python -m server.cli demo [--reset]` |
| `make export` | manifests, traces and result summary → `exports/` | `python -m server.cli export` |

Other commands: `python -m server.cli backup DIR`, `restore-backup DIR`, and `gc [--apply]` (garbage collection plus raw-evidence retention purge). `python -m server.runtime.mcp_proxy --project P --agent A --task T --server S` runs the stdio MCP proxy.

## What the demo shows

`make demo` seeds `proj_demo` with synthetic people, domains and documents, then runs these flows through the real services:

1. **Learn from an execution failure.** The export tool has a declared cp1252 default (an encoding fixture). Writing `→` and `✓` fails twice, which opens an incident and generates a candidate (UTF-8 for this tool when the text is not cp1252-encodable). A bounded repair runs on an isolated branch. The fixed suite then runs 2 fresh targets and 2 protected counterexamples. The candidate is accepted with assisted validation and a version node is saved. **Agent B** uses the lesson on a different task.
2. **A rejected rule.** A seeded overbroad "always UTF-16" candidate fails protected counterexamples and is rejected.
3. **Block a harmful action.** A poisoned vendor page tells the agent to forward the report to `audit-review@exfil.example`. The gateway denies the send before execution (the executor count stays at zero), quarantines the anchoring sentence and continues from a clean context. A threat-pattern candidate passes its rewritten variants and benign near-matches.
4. **Shared protection.** Agent B loads the new registry revision. A related instruction inside a calendar invite is quarantined *before model ingestion*. The legitimate send to Finance still goes out.
5. **RECEIPT.** An experiment branch with a seeded bad memory (`preferred_report_encoding=utf-16`) gets stuck. Compare shows the memory change, removing it keeps the lesson, the continuation passes, and the corrected report is merged into main with a three-way selected merge that passes combined checks.
6. **Revocation.** The pattern false-positives on an internal IT notice. Its revision 1 is revoked globally, and restoring an older snapshot of Agent B stays free of it. A narrower revision 2 (internal-domain exception) is evaluated and accepted.
7. **No duplicate sends.** A simulated crash right after a completed outbox send is followed by save, restore and resume. The same operation deduplicates and the outbox count is unchanged.
8. **Unknown effect.** An outbox timeout after write leaves an `UNKNOWN` effect. The run waits until you click **Reconcile** in *Effects & outbox*.
9. **Context caps.** A small budget omits history but keeps the permissions and source labels. An impossible budget pauses the run instead of dropping authority.
10. **Proxy capability.** A host agent connects through the MCP proxy (tool-only profile, no full restore). The proxy blocks a send to an unauthorized address.

## Architecture

```
Task agent ──proposal──▶ Gateway ──bound decision──▶ Executors (files, outbox, ledger, fetch, MCP upstream)
    ▲                       │ decisions, effects             │ results
    │ context packet        ▼                                ▼
Context compiler ◀── Journal (events, effect journal) ◀── Source processing (HTML, text, JSONL, ICS, tool results)
    ▲                       │                                ▲
Versioned lessons ◀── Learning + independent evaluation ◀── Health monitor / incidents
(registry, RECEIPT)
```

| Module | Role |
|---|---|
| `server/gateway/` | ordered policy checks, bound decisions, effect reservation, dispatch of stored args, constrained auditor |
| `server/ingress/` | parsers, shared source pipeline, log cursors, live calendar adapter |
| `server/executors/` | contained files, local outbox, payment ledger, SSRF-safe fetch |
| `server/runtime/` | LangGraph reference agent, planners, procedure module, coordinator, MCP proxy |
| `server/health/` | signals, state machine, incidents |
| `server/learning/` | candidates, threat patterns, promotion, revision, revocation |
| `server/registry/` | monotonic revisions, acknowledgements, authoritative revocation |
| `server/versions/` | manifests, atomic saves, fork/restore, compare, three-way merge, GC |
| `server/context/` | packet compiler and token accounting |
| `server/evaluation/` | suite registry, pinned checkers, sandbox runs, baseline ladder |
| `server/storage/` | SQLite repository, migrations, content-addressed objects |
| `server/api/` | authenticated HTTP API with SSE |
| `apps/web/` | React client: overview, assignments, runs, incidents, knowledge, evaluation, version history, effects, sources, settings |
| `fixtures/` | synthetic sources (benign and hostile), frozen suite, MCP upstream fixture |
| `tests/` | T01–T14 |

## Capability profiles

| Adapter | intercept | label obs. | export msgs | restore cursor | isolate workspace | pause | export context |
|---|---|---|---|---|---|---|---|
| `langgraph_reference` | yes | yes | yes | yes | yes | yes | yes |
| `mcp_proxy` | yes | yes | no | no | no | yes | no |

An agent is protected only for actions routed through a tested adapter. A host that keeps a direct shell, browser or filesystem tool alongside the proxy has a bypass, and the capability view says so. No native Codex, Cursor or Letta adapter is built. The shell executor is disabled.

## Requirement-to-test coverage

| Req | Test | Evidence |
|---|---|---|
| R01 R03 | T01 `tests/test_t01_authority.py` | page, invite and log instructions cannot add recipients, tools or file scope; strict schemas reject authority fields |
| R02 | T02 `tests/test_t02_sources.py` | HTML hidden spans, Markdown comments, JSONL offsets and malformed lines, ICS folding, time zones and recurrence, tool results, chunk offsets, log cursors and rotation |
| R03 | T03 `tests/test_t03_gateway.py` | zero executor calls for denials; dispatched hash equals checked hash; operation identity; auditor and storage fail closed; grants; spending; SSRF and egress |
| R04 | T04 `tests/test_t04_health.py` | repeated failure → bounded repair; pause without recovery; step budget; no progress; failed repair |
| R05 | T05 `tests/test_t05_learning.py` | fresh targets + protected counterexamples, ablation, revision and supersede, rejection memory, checker pinning, automatic class |
| R06 | T06 `tests/test_t06_registry.py` | Agent B acknowledgement and use; incompatible environment filtered; pattern intercepts a held-out invite before ingestion; benign article untouched |
| R07 | T07 `tests/test_t07_restart.py` | restart resumes from checkpoint with deduplication; in-flight effects marked; crash points |
| R08 | T08 `tests/test_t08_branches.py` | file, memory and thread isolation; diff; selected merge; conflicts; stale head; failed combined checks |
| R09 | T09/T10 `tests/test_t09_t10_effects.py` | restore after send does not resend; unknown effects pause; reconciliation |
| R10 | T11 `tests/test_t11_context.py` | cap holds, mandatory content survives, pause on overflow, revoked lesson excluded from old manifest |
| R11 | T12 `tests/test_t12_api_flow.py` | incident → candidate → evaluation → accepted version through the API; auth scoping; SSE replay |
| R12 | T13 `tests/test_t13_proxy.py` | pinned tools, ignored annotations, gateway-routed calls, unsupported restore reported |
| R13 | T14 `tests/test_t14_export.py` | export modes, config, counts and evidence; baseline ladder; live calendar adapter (mocked) |

## Configuration

Environment variables (defaults in `server/config.py`):

- Storage: `DATABASE_PATH`, `OBJECT_STORE_ROOT`, `WORKSPACE_ROOT`
- Model: `MODEL_PROVIDER` (`deterministic` | `anthropic` | `recorded`), `MODEL_ID`, `MODEL_CONTEXT_LIMIT`, `AUDITOR_PROVIDER`
- Service: `API_BIND_HOST` (127.0.0.1), `UI_ORIGIN`, `PROJECT_AUTH_MODE`
- Sources and context: `MAX_SOURCE_BYTES`, `CHUNK_SIZE`, `CONTEXT_MARGIN`
- Limits: `MAX_TOOL_STEPS`, `MAX_REPAIR_STEPS`, `MAX_RUN_COST`, `GUARD_TIMEOUT_SECONDS`, `EFFECT_LEASE_SECONDS`
- Learning and retention: `AUTO_PROMOTE_CLASSES`, `RAW_EVIDENCE_RETENTION_DAYS`
- Live calendar: `GOOGLE_CALENDAR_TOKEN`, `GOOGLE_CALENDAR_IDS`

## Documents

- [`docs/COMPLETION_REPORT.md`](docs/COMPLETION_REPORT.md): what works, recorded results, limitations, live versus simulated
- [`docs/DECISIONS.md`](docs/DECISIONS.md): decision log and deviations
- [`docs/API.md`](docs/API.md) / [`docs/openapi.json`](docs/openapi.json): generated from the implemented app
- [`docs/ANTIBODY_Technical_Design.md`](docs/ANTIBODY_Technical_Design.md): the specification

## Research and component acknowledgements

These works informed the mechanisms; none of their results are claimed or reproduced here: LangGraph persistence and time travel (checkpointer); the MCP tools specification; ActGuard and ARGUS (action auditing and provenance; adapted with rules, not reproduced); CaMeL (control/data separation; no formal guarantees inherited); Sentry, Self-Harness, Agentic Harness Engineering, SkillHone and ReHarness (failure-triggered, evaluated, revisable learning); MemSecBench and Agent Meltdowns (memory repair; bounded recovery); Temporal (execution versus effect state); AgentDojo (security/utility separation; `suite_core` is a custom suite, **not** an official AgentDojo result). Kiro Crew, Letta, Mem0, AgentStateGraph, Lakera and Invariant are related products whose documented patterns were considered. No code was copied from them.
