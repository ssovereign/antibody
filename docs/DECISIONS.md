# Decision log and deviations

Short records of implementation choices the Technical Design left to the implementer, plus every material deviation. Versions are pinned in `requirements.lock` and `apps/web/package-lock.json`.

## Stack

| ID | Decision | Why |
|---|---|---|
| D01 | Python 3.12, FastAPI 0.143.3, Pydantic 2.14.1, uvicorn 0.54.0 | The design's chosen stack. |
| D02 | SQLite through the standard-library `sqlite3` module with an explicit numbered-migration runner (`server/storage/migrations/*.sql`, `schema_migrations` table) | Maintained, dependency-free and gives direct control of `BEGIN IMMEDIATE`, WAL and `synchronous=FULL`. The repository boundary (`Database`, `ObjectStore`) is what a PostgreSQL deployment replaces. |
| D03 | LangGraph 1.2.16 with `langgraph-checkpoint-sqlite` 3.1.2 (`SqliteSaver`) in a separate `checkpoints.sqlite` | Persistent checkpointer as required (not the in-memory saver). Effect deduplication is ours: replay re-enters the gateway stage with the stored operation ID. |
| D04 | HTML: standard-library `html.parser` subclass | No script execution, no network; it lets us record hidden spans (`display:none`, `hidden`, `aria-hidden`, zero font size, hidden classes, hidden inputs) and comments as exclusions with offsets. |
| D05 | ICS: `icalendar` 7.3.0 + `dateutil.rrule` bounded expansion (25 instances / 120 days) | Handles folding, escapes, time zones, recurrence, cancellations. |
| D06 | Web client: React 18.3.1, TypeScript 5.6.3, Vite 5.4.11, no component library, hash routing | Small dependency surface. SSE is consumed through `fetch` so the bearer token stays in a header, never in a URL. |
| D07 | MCP proxy implemented directly over newline-delimited JSON-RPC 2.0 (stdio), protocol `2025-11-25`, tools only | Avoids SDK version churn; the proxy needs only initialize / tools/list / tools/call with correlation, capability negotiation and error forwarding. |

## Behaviour choices

| ID | Decision |
|---|---|
| D10 | **Model provider.** No model credentials exist in this environment, so the default provider is a deterministic reference planner and its runs are labeled `simulated`. A live Claude planner (`MODEL_PROVIDER=anthropic`) and a recorded-trace planner (`recorded` mode) are implemented. The live planner has not been exercised here. |
| D11 | **Susceptible-model flag.** `planner.follows_embedded_instructions` makes the simulated planner act on instructions it reads, so the fixed gateway and learned interception have something to stop. The flag is recorded in each run's immutable start configuration and shown in the UI. |
| D12 | **Semantic auditor.** The default auditor is a rules-based constrained auditor with a strict output schema, a timeout and fail-closed handling. It adapts ideas from ActGuard and ARGUS but does not reproduce their probability-based attribution. `AUDITOR_PROVIDER=anthropic` uses a separate constrained Claude call with hostile text passed as labeled data. |
| D13 | **Where procedure lessons act.** Accepted `procedure` lessons may set only operator-declared learnable parameters (`write_report.encoding`). The adapter's bounded procedure module applies them when action-time conditions hold, evaluated by code. A parameter the model chose explicitly is never overridden. Lessons also appear in the context packet for live models. |
| D14 | **Clean context repair.** After a denied action anchored in external content, the anchoring sentences are quarantined (span level, so benign material survives). The run continues from a clean packet compiled from trusted task state and validated progress (the design allows this as an alternative to re-running from a pre-exposure checkpoint), and dependent packets and summaries are invalidated. |
| D15 | **Bounded recovery.** Two equivalent failures open an incident, generate a candidate and start one repair branch per incident: a child run with its own branch, workspace copy, step budget (`MAX_REPAIR_STEPS`) and the same gateway. Repair runs never spawn further repairs. A passing repair is not acceptance; the candidate still needs the fixed suite. |
| D16 | **Evaluation isolation.** Each evaluation case runs in a fresh sandbox `Core` (its own database, object store, workspaces, outbox and checkpoints) using the same runtime code. Candidate lessons and patterns are injected as "under test" and never touch the project registry. |
| D17 | **Health state machine.** It follows the design's diagram plus edges needed for operation: `Degraded → Healthy` and `Recovering → Healthy` when checks pass, `Paused → Healthy` on an explicit operator resume, and `Degraded → Quarantined` for a suspect context. Every transition is persisted with a reason code. |
| D18 | **Deny versus pause.** A missing grant, stale revision, auditor uncertainty, unknown effect or budget pauses the run. A deterministic scope violation is denied; the run continues with independent authorized work unless the denial is anchored in external content, in which case context repair runs. |
| D19 | **Restore cursor scoping.** A saved adapter cursor seeds a restored run only when the run is for the same task. Its operation IDs are bound to the new run, so re-entering a completed operation deduplicates. A different intended operation always receives a new ID. |
| D20 | **Verified artifacts across branches.** A check record is bound to exact content (hash). Fork and merge carry it to branches holding identical bytes. A changed file is unapproved until re-checked. |
| D21 | **Canonical JSON.** Sorted keys, UTF-8, no whitespace, floats rejected (money and ratios are decimal strings), list order significant. `version_id = sha256` of the manifest without `version_id`. |
| D22 | **Local auth.** `PROJECT_AUTH_MODE=local` lets loopback clients obtain an app-scoped, project-scoped token from `/v1/session/bootstrap`. Admin, operator and worker tokens are created by `make setup` and stay server-side in `var/secrets.json`. App tokens cannot act as the runtime worker. |
| D23 | **Baseline ladder in `make eval`.** A: the same planner calling executors directly (no gateway, no screening). B: fixed gateway plus a static always-UTF-8 wrapper. C: gateway plus an unscoped growing memory file of procedure notes (including a later overbroad UTF-16 note). D: full ANTIBODY with lessons learned from discovery and validated on the validation split. Same planner, budget, fixtures and checker for all four. |
| D24 | **Price configuration.** Cost is computed from `fixtures/price_config.json` (placeholders). With the deterministic planner, token counts are estimates and the price is reported as not measured. |

## Known limitations (not hidden)

- **Live model and auditor:** implemented but not exercised (no credentials). All recorded results use `simulated` or `recorded` modes.
- **Live calendar provider (Google Calendar v3):** implemented with sync tokens, 410 resync, cancellations, recurrence instances and revision dedupe. Tested only against a mocked transport. It shows `unconfigured` without `GOOGLE_CALENDAR_TOKEN` and `GOOGLE_CALENDAR_IDS`.
- **SSRF:** DNS answers are checked on every redirect hop immediately before the request, but the connection is not pinned to the vetted address. A rebinding resolver has a small window. Production should pin connections.
- **File containment:** containment is enforced in the executor (resolved paths, symlink and reparse-point rejection, roots checked at use time). It is not an operating-system sandbox.
- **Exactly-once:** no local transaction guarantees exactly-once delivery in remote systems. Tools without a reconciliation function require an explicit operator decision for `UNKNOWN` effects.
- **Platform claims:** the `fixture_1` cp1252 export default is a declared encoding fixture. Tests ran on a Windows 11 machine, which is not evidence about any particular Windows tool.
- **Statistics:** the deterministic planner makes repeated trials identical. Counts are reported with percentages; there is no statistical reliability claim.

## Not built (extensions, deliberately absent)

HarnessPatch (executable harness edits) · isolated arbitrary-shell executor (shell stays disabled) · cross-organization threat federation · native Codex, Cursor and Letta adapters (the capability view says so) · PostgreSQL and remote object storage (boundary only) · model-weight training.
