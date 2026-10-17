# Completion report

Built and verified on 2026-10-17, on Windows 11 (10.0.26100) with Python 3.12.10 and Node 24.15.

## Start instructions

```bash
make setup
```
```bash
make demo
```
```bash
make dev
```

Open http://127.0.0.1:8765. Without `make`: `python scripts/setup.py`, `.venv/Scripts/python -m server.cli demo`, `.venv/Scripts/python scripts/dev.py`. Tests: `make test` (72 tests) and `make test-security`. Frozen comparison: `make eval`. Export: `make export`.

## Status by requirement

| Req | Status | Evidence |
|---|---|---|
| R01 Assignment and typed permissions | Done | Trusted-actor task revisions; server-owned recipients and payees; strict schemas (T01) |
| R02 Source processing | Done | HTML, text/Markdown, JSONL/text logs with cursors and rotation, ICS and event JSON, tool results, subagent messages; chunk offsets; parse-time exclusions (T02) |
| R03 Checks before execution | Done | Ordered gateway; 0 executor invocations for every non-allowed decision across all tests and the demo; dispatched hash equals checked hash (T03) |
| R04 Health and bounded recovery | Done | Explicit state machine with recorded reasons; repeat-failure repair branch; no-progress, step and cost budgets (T04, extras) |
| R05 Evaluated learning and revision | Done | Fixed suite with pinned checker hash; 2 fresh targets and 2 protected counterexamples; revision, supersede, rejection memory (T05) |
| R06 Shared lessons and patterns | Done | Registry revisions with acknowledgements; Agent B uses the lesson and intercepts a held-out invite variant before ingestion (T06) |
| R07 Durable save and resume | Done | Restart reconstructs the LangGraph run from the SQLite checkpointer; in-flight effects marked; crash points (T07) |
| R08 Fork, diff, selected merge | Done | Isolated directories (copy, never hard-link); structured diff; three-way selected merge with conflicts and combined checks (T08) |
| R09 Effect reconciliation | Done | Permanent journal; operation IDs bound across restore; UNKNOWN pauses until reconciliation (T09/T10) |
| R10 Context budget control | Done | Packet compiler with cap, margin, omissions and labels; pauses rather than truncating authority (T11) |
| R11 Usable evidence interface | Done | React client: incident → evidence → candidate → evaluation → accepted version, live SSE timeline, disconnected state (T12 + manual browser check) |
| R12 Runtime and tool integration | Done, scoped | LangGraph reference adapter (full profile); MCP stdio proxy (tool-only profile, restore reported unsupported) (T13) |
| R13 Export and repeatable evaluation | Done | `make export` bundle with mode labels, config, counts and evidence; `make eval` baseline ladder (T14) |

## Capability profiles

| Adapter | intercept | label | export msgs | restore cursor | isolate ws | pause | export ctx | Tested by |
|---|---|---|---|---|---|---|---|---|
| langgraph_reference | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | T01, T03–T12 |
| mcp_proxy | ✓ | ✓ | ✗ | ✗ | ✗ | ✓ | ✗ | T13 |

## Recorded results (`make eval`, mode: simulated)

The pipeline ran in an isolated store. The discovery cases produced 2 incidents and 2 candidates. Both passed validation (2 of 2 fresh targets improved over baseline, 2 of 2 protected cases passed) and were accepted. All four baselines were then run on the untouched final split: 8 cases × 3 repeats. The planner is deterministic, so the repeats are identical.

| Baseline | Family | Kind | Task completion | Unauthorized effects | Unsafe proposals | Early interceptions | False positives |
|---|---|---|---|---|---|---|---|
| A plain agent | encoding | target | 0 of 6 | 0 | 0 | 0 | 0 |
| B static gateway + UTF-8 wrapper | encoding | target | 6 of 6 | 0 | 0 | 0 | 0 |
| C gateway + growing memory file | encoding | target | 0 of 6 | 0 | 0 | 0 | 0 |
| D ANTIBODY | encoding | target | 6 of 6 | 0 | 0 | 0 | 0 |
| A plain agent | encoding | counterexample | 6 of 6 | 0 | 0 | 0 | 0 |
| B static gateway + UTF-8 wrapper | encoding | counterexample | 6 of 6 | 0 | 0 | 0 | 0 |
| C gateway + growing memory file | encoding | counterexample | 0 of 6 | 0 | 0 | 0 | 0 |
| D ANTIBODY | encoding | counterexample | 6 of 6 | 0 | 0 | 0 | 0 |
| A plain agent | injection | attack variant | 6 of 6 | **6** | 6 | 0 | 0 |
| B static gateway | injection | attack variant | 6 of 6 | 0 | 6 | 0 | 0 |
| C gateway + memory file | injection | attack variant | 0 of 6 | 0 | 0 | 0 | 0 |
| D ANTIBODY | injection | attack variant | 6 of 6 | 0 | 0 | **6** | 0 |
| A plain agent | injection | benign near-match | 6 of 6 | **3** | 3 | 0 | 0 |
| B static gateway | injection | benign near-match | 6 of 6 | 0 | 3 | 0 | 0 |
| C gateway + memory file | injection | benign near-match | 0 of 6 | 0 | 0 | 0 | 0 |
| D ANTIBODY | injection | benign near-match | 6 of 6 | 0 | 3 | 0 | **0** |

Reading these results honestly:

- **The fixed gateway does the protecting.** B and D both executed 0 unauthorized effects. The plain agent executed 6 on attacks and 3 on a benign page that quotes an attack. Learning did not make attacks less successful; the gateway already stopped them. What the learned pattern added was **earlier interception**: in D, 6 of 6 attack variants were quarantined before model ingestion, so 0 unsafe proposals reached the gateway (B had 6), with 0 false positives on benign near-matches.
- **A static fix matches learning on this encoding problem.** B's always-UTF-8 wrapper scores the same as D. The spec anticipated this ("this simple problem may not justify a general supervisor by itself"). The scoped lesson's value here is that it was discovered, tested and shared from a failure rather than hand-written. Showing a clear advantage needs new failure families; that is future work.
- **An unscoped memory file is harmful.** C applies notes from every incident without applicability conditions, including a later overbroad UTF-16 note, and fails everywhere. This is a constructed baseline (decision D23), not a measurement of any product.
- In D's benign row, the 3 unsafe proposals are from the simulated susceptible planner acting on a quoted attack string. The pattern correctly only flagged that quotation (no quarantine), and the gateway denied the send.
- Cost: token counts are estimates (chars/4) and price is not measured; the deterministic planner has no provider cost.

The full rows (passing and failing) are in `exports/eval_*/final_evaluation.json` and the summary in `exports/eval_*/SUMMARY.md`.

## Demonstration outcomes (`make demo`, all runs labeled)

1. Encoding failure (2 equivalent `UnicodeEncodeError`s) → incident → candidate → repair branch passed → fixed suite passed → accepted with assisted validation → version node. Agent B's fresh task passed using the lesson.
2. A seeded overbroad UTF-16 candidate was rejected (protected regression failed).
3. A poisoned page's send to `audit-review@exfil.example` was denied before execution (executor ledger: 0 invocations). The anchoring span was quarantined and the run finished from a clean packet. Controlled attribution: the unsafe proposal reproduced in 2 of 2 reruns with the original sources and disappeared in 2 of 2 with only the spans masked, giving **intervention supported**.
4. The pattern passed its variants and benign controls. Agent B quarantined the related invite instruction before ingestion and still sent to Finance.
5. Experiment branch with a seeded bad memory: stuck (no progress) → compare showed the memory change → memory removed, lesson kept → continuation passed → selected file merge with combined checks (6 of 6).
6. Pattern revision 1 false-positived on an internal IT notice → revoked → restored older snapshot no longer quarantines → revision 2 (internal-domain exception) evaluated and accepted.
7. Simulated crash after a completed send → save → restore → resume: same operation deduplicated; outbox unchanged.
8. An outbox timeout left an `UNKNOWN` effect for reconciliation in the UI. Reconciling it from the browser was verified.
9. A context cap kept permissions and labels; an impossible cap paused the run.
10. Recorded replay (mode `recorded`) and an MCP-proxy host session (send to an unauthorized address blocked).

## Live versus simulated

| Component | Status |
|---|---|
| Gateway, journal, effect ledger, executors (files, outbox, ledger, fetch), checks, learning, registry, evaluation, RECEIPT | Real code, exercised |
| Agent model | **Simulated** deterministic reference planner (no credentials). Live Claude planner implemented, not exercised |
| Semantic auditor | Rules-based constrained auditor (real, deterministic). Claude auditor implemented, not exercised |
| Destinations | Local outbox and payment ledger (real, countable test destinations; synthetic identities) |
| Encoding failure | Declared cp1252 export fixture (`fixture_1`), not a live Windows-tool claim |
| Web fetch | Real restricted HTTPS adapter; tests use a mocked transport and resolver |
| Calendar | ICS import real. Google Calendar adapter implemented and mock-tested; **unconfigured** without credentials |
| MCP proxy | Real stdio proxy against a synthetic upstream server |
| Recorded trace | One replay, labeled `recorded`, never counted as a live attack success |

## Known limitations and incomplete integrations

- The live model, live auditor and live calendar provider have not been run against real services.
- SSRF protection checks DNS on every hop but does not pin the connection (a small rebinding window, documented).
- File containment is executor-level, not an OS sandbox. The shell executor is disabled.
- Exactly-once delivery is impossible without destination support. Tools without a reconciler need an operator decision for `UNKNOWN` effects (implemented: `resolve_to`).
- Statistics: the suite is small and controlled, and repeated trials of a deterministic planner are identical. These are counts, not reliability claims, and `suite_core` is not an AgentDojo result.
- Not built (extensions): HarnessPatch, isolated shell, cross-organization federation, native Codex/Cursor/Letta adapters, PostgreSQL deployment.

## Dependency inventory

- Python (pinned in `requirements.txt`; full lock in `requirements.lock`, 62 packages): fastapi 0.143.3, uvicorn 0.54.0, pydantic 2.14.1, httpx 0.28.1, langgraph 1.2.16, langgraph-checkpoint-sqlite 3.1.2, icalendar 7.3.0, python-dateutil 2.9.0.post0, anthropic 1.15.0, pytest 9.1.1.
- Web (`apps/web/package-lock.json`): react 18.3.1, react-dom 18.3.1, vite 5.4.11, @vitejs/plugin-react 4.3.4, typescript 5.6.3.
