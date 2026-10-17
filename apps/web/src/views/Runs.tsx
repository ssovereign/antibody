import React, { useEffect, useMemo, useRef, useState } from "react";
import { api, currentProject, newKey, q, short, StreamState, streamEvents, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Json, KV, Link, Loading, ModeBadge, PlainText, Status, Tabs, useAction, useApi } from "../ui";

export default function Runs({ runId }: { runId?: string }) {
  if (runId) return <RunDetail runId={runId} />;
  return <RunList />;
}

function RunList() {
  const pid = currentProject();
  const runs = useApi(() => api(`/v1/runs${q({ project_id: pid })}`), [pid]);
  const agents = useApi(() => api(`/v1/agents${q({ project_id: pid })}`), [pid]);
  const tasks = useApi(() => api(`/v1/tasks${q({ project_id: pid })}`), [pid]);
  const [agent, setAgent] = useState("");
  const [task, setTask] = useState("");
  const [budget, setBudget] = useState(6000);
  const { busy, run } = useAction();
  const start = () =>
    run("start", async () => {
      const r = await api("/v1/runs", { method: "POST", body: { project_id: pid, agent_id: agent, task_ref: task, context_budget_tokens: budget }, idem: newKey("start") });
      window.location.hash = `#/runs/${r.run_id}`;
    }, "Run started");
  const refAgents = (agents.data || []).filter((a: any) => a.adapter === "langgraph_reference");
  return (
    <Page title="Runs">
      <div className="card">
        <h2>Start a run</h2>
        <div className="row">
          <select value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Agent">
            <option value="">Agent…</option>
            {refAgents.map((a: any) => <option key={a.agent_id} value={a.agent_id}>{a.name}</option>)}
          </select>
          <select value={task} onChange={(e) => setTask(e.target.value)} aria-label="Task">
            <option value="">Assignment…</option>
            {(tasks.data || []).map((t: any) => <option key={t.task_id} value={t.task_id}>{t.task_id} (rev {t.revision})</option>)}
          </select>
          <label className="small">Context budget <input type="number" value={budget} min={200} step={500} onChange={(e) => setBudget(+e.target.value)} style={{ width: 90 }} /> tokens</label>
          <Btn kind="primary" busy={busy === "start"} onClick={start} disabledReason={!agent ? "Choose an agent" : !task ? "Choose an assignment" : null}>Start</Btn>
        </div>
        <p className="muted small">Runs bind the current assignment revision, policy and registry revision at start. The web client never calls executors.</p>
      </div>
      <div className="card">
        <h2>All runs</h2>
        <ErrorBox error={runs.error} retry={runs.reload} />
        {runs.loading && !runs.data ? <Loading /> : null}
        {runs.data && runs.data.length === 0 ? <Empty>No runs yet.</Empty> : null}
        <table>
          <thead><tr><th>Run</th><th>Agent</th><th>Task</th><th>Status</th><th>Health</th><th>Mode</th><th>Created</th></tr></thead>
          <tbody>
            {(runs.data || []).map((r: any) => (
              <tr key={r.run_id} className="clickable" onClick={() => (window.location.hash = `#/runs/${r.run_id}`)}>
                <td className="mono">{short(r.run_id)}{r.parent_run_id ? <div className="small muted">child of {short(r.parent_run_id)}</div> : null}</td>
                <td className="small">{(agents.data || []).find((a: any) => a.agent_id === r.agent_id)?.name || short(r.agent_id)}</td>
                <td>{r.task_id}@{r.task_revision} <div className="small muted">{r.purpose}</div></td>
                <td><Status value={r.status} /><div className="small muted">{r.status_reason}</div></td>
                <td><Status value={r.health} /></td>
                <td><ModeBadge mode={r.mode} /></td>
                <td className="small muted">{when(r.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Page>
  );
}

function describe(e: any): string {
  const p = e.payload || {};
  switch (e.kind) {
    case "run.started": return `${p.mode} run of ${p.task_ref} on ${p.adapter}; budget ${p.context_budget_tokens} tokens`;
    case "registry.loaded": return `Loaded registry revision ${p.revision} (${(p.records || []).length} accepted records) at ${p.boundary}`;
    case "context.compiled": return `Packet ~${p.token_count}/${p.budget_tokens} tokens (${p.count_kind}); ${p.observations} observations; lessons: ${(p.lessons || []).join(", ") || "none"}${p.fits ? "" : " — mandatory content does not fit"}`;
    case "model.called": return p.finish ? `Model finished: ${p.rationale}` : `Model proposed ${p.tool_id}: ${p.rationale}`;
    case "action.proposed": return `${p.tool_id} ${JSON.stringify(p.arguments).slice(0, 140)}`;
    case "action.decided": return `${p.outcome} ${p.reason_code}: ${p.message}${p.executor_invoked === false && p.outcome !== "ALLOW" ? " (executor not invoked)" : ""}`;
    case "effect.started": return `Dispatching ${p.tool_id} (${short(p.operation_id)})`;
    case "effect.completed": return `${p.tool_id} completed`;
    case "effect.failed": return `${p.tool_id} failed: ${p.error?.family} ${p.error?.message || ""}`;
    case "effect.unknown": return `${p.tool_id} outcome unknown: the tool may have completed. Reconcile before retrying.`;
    case "effect.reconciled": return `Reconciled ${short(p.operation_id)}: ${p.from} → ${p.to} (${p.evidence})`;
    case "observation.received": return `${p.channel} source "${p.label}" (${p.origin}); ${p.chunks?.length} chunk(s); ${p.parse_exclusions?.length || 0} parse exclusion(s); ${p.quarantined_spans} quarantined span(s)`;
    case "source.quarantined": return `Quarantined span ${p.stage}${p.pattern_ref ? ` by ${p.pattern_ref} (${p.level})` : ""}`;
    case "check.finished": return p.check === "report_artifact" ? `Report check ${p.passed ? "passed" : "failed"} (${p.data_class})` : `Step ${p.tool_id}: ${p.progress ? "progress" : "no measurable progress"}`;
    case "health.changed": return `${p.from} → ${p.to}: ${p.reason_code}`;
    case "incident.opened": return `Incident: ${p.category} (${p.attribution_level})`;
    case "candidate.created": return `Candidate ${p.kind} for ${p.lesson_id}`;
    case "lesson.applied": return p.applied ? `Applied ${p.ref} (${JSON.stringify(p.changes)})` : `Did not apply ${p.ref}: ${p.reason}`;
    case "recovery.started": return `Repair branch ${short(p.repair_branch)} with ${p.candidate}; budget ${p.budget_steps} steps`;
    case "recovery.finished": return `Repair ${p.passed ? "passed independent checks" : "failed"}. ${p.note}`;
    case "run.status": return `Status ${p.from} → ${p.status}${p.reason ? ` (${p.reason})` : ""}`;
    default: return JSON.stringify(p).slice(0, 160);
  }
}

const KIND_TONE: Record<string, string> = {
  "action.decided": "", "effect.unknown": "warn", "effect.failed": "bad", "source.quarantined": "bad", "incident.opened": "warn",
  "health.changed": "violet", "lesson.applied": "info", "registry.loaded": "info",
};

function RunDetail({ runId }: { runId: string }) {
  const pid = currentProject();
  const runQ = useApi(() => api(`/v1/runs/${runId}`), [runId]);
  const [events, setEvents] = useState<any[]>([]);
  const [stream, setStream] = useState<StreamState>("connecting");
  const [sel, setSel] = useState<any>(null);
  const seen = useRef(new Set<number>());
  const last = useRef<number>(0);
  const { busy, run } = useAction();

  useEffect(() => {
    seen.current = new Set();
    last.current = 0;
    setEvents([]);
    setSel(null);
    const stop = streamEvents(
      `/v1/runs/${runId}/events?stream=true`,
      (ev) => {
        if (seen.current.has(ev.sequence)) return; // tolerate duplicate delivery
        seen.current.add(ev.sequence);
        last.current = Math.max(last.current, ev.sequence);
        setEvents((xs) => [...xs, ev].sort((a, b) => a.sequence - b.sequence));
        if (["run.status", "health.changed", "effect.unknown", "action.decided"].includes(ev.kind)) runQ.reload();
      },
      setStream,
      () => (last.current ? String(last.current) : null),
    );
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const r = runQ.data;
  const active = r && ["running", "recovering"].includes(r.status);
  const unknown = r?.counts?.effects_unknown > 0;
  return (
    <Page
      title={<>Run <span className="mono">{short(runId, 16)}</span></>}
      actions={
        r ? (
          <div className="row">
            <ModeBadge mode={r.mode} />
            <Btn onClick={() => run("pause", () => api(`/v1/runs/${runId}/pause`, { method: "POST", body: {} }).then(runQ.reload), "Pause barrier set")}
                 busy={busy === "pause"} disabledReason={!active ? "Run is not active" : !r.capabilities.cancel_or_pause ? "Adapter cannot pause" : null}>Pause</Btn>
            <Btn onClick={() => run("resume", () => api(`/v1/runs/${runId}/resume`, { method: "POST", body: {} }).then(runQ.reload), "Resumed")}
                 busy={busy === "resume"} disabledReason={r.status !== "paused" ? "Only paused runs resume" : unknown ? "Reconcile unknown effects first" : null}>Resume</Btn>
            <Btn onClick={() => run("cont", async () => { const n = await api(`/v1/runs/${runId}/continue`, { method: "POST", body: {} }); window.location.hash = `#/runs/${n.run_id}`; }, "Continuation started")}
                 busy={busy === "cont"} disabledReason={active ? "Pause first" : unknown ? "Reconcile unknown effects first" : r.adapter !== "langgraph_reference" ? "Proxy runs are driven by the host" : null}
                 title="Start a new run under the current authorization, keeping validated progress">Continue as new run</Btn>
            <a className="btn" href={`#/effects/${runId}`} title="Everything this run did outside the agent, and how to mitigate it">What did this run do?</a>
          </div>
        ) : null
      }
    >
      <ErrorBox error={runQ.error} retry={runQ.reload} />
      {stream === "disconnected" ? (
        <div className="banner warn" role="status">Event stream disconnected. Reconnecting from event {last.current}; the timeline is paused, not progressing.</div>
      ) : null}
      {r ? <RunHeader r={r} stream={stream} /> : <Loading what="run" />}
      <div className="split">
        <div className="card">
          <h2>Timeline <span className="muted small">{events.length} events</span></h2>
          {events.length === 0 ? <Loading what="events" /> : null}
          <ol className="timeline">
            {events.map((e) => (
              <li key={e.sequence} className={sel?.sequence === e.sequence ? "selected" : ""} onClick={() => setSel(e)} tabIndex={0}
                  onKeyDown={(k) => (k.key === "Enter" || k.key === " ") && setSel(e)}>
                <span className="seq">{e.sequence}</span>
                <div>
                  <div className="row" style={{ gap: 6 }}>
                    <span className="k">{e.kind}</span>
                    {e.kind === "action.decided" ? <Status value={e.payload.outcome} /> : null}
                    {KIND_TONE[e.kind] ? <Badge kind={KIND_TONE[e.kind]} plain>{e.actor_type}</Badge> : <span className="muted small">{e.actor_type}</span>}
                  </div>
                  <div className="d">{describe(e)}</div>
                </div>
              </li>
            ))}
          </ol>
        </div>
        <div className="sticky">
          <Inspector ev={sel} runId={runId} />
        </div>
      </div>
    </Page>
  );
}

function RunHeader({ r, stream }: { r: any; stream: StreamState }) {
  const sc = r.start_config || {};
  return (
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="row">
          <Status value={r.status} /> <span className="muted small">{r.status_reason}</span>
          <Status value={r.health} />
          <Badge kind={stream === "live" ? "ok" : stream === "disconnected" ? "warn" : "neutral"}>{stream === "live" ? "stream live" : stream}</Badge>
        </div>
        <div className="small muted">updated {when(r.updated_at)}</div>
      </div>
      <div className="grid2" style={{ marginTop: 10 }}>
        <KV items={[["Task", `${r.task_id}@${r.task_revision}`], ["Agent", sc.agent?.name || r.agent_id], ["Model", `${sc.model?.provider} / ${sc.model?.model_id}`]]} />
        <KV items={[["Branch", <span className="mono">{short(r.branch_id)}</span>], ["Base version", <span className="mono">{short(r.base_version)}</span>], ["Policy", r.policy_revision]]} />
        <KV items={[["Registry rev.", String(r.registry_revision)], ["Decisions", `${r.counts.decisions} (${r.counts.denied} denied)`], ["Executor calls for denied", String(r.counts.denied_executor_invocations)]]} />
        <KV items={[["Quarantined spans", String(r.counts.quarantined_spans)], ["Lesson uses", String(r.counts.lesson_uses)], ["Tokens", `${(r.cost.input_tokens || 0) + (r.cost.output_tokens || 0)} (${r.cost.count_kind || "estimated"})`]]} />
      </div>
      {r.parent_run_id ? <p className="small">Child of <Link to={`/runs/${r.parent_run_id}`}>{short(r.parent_run_id)}</Link> ({sc.purpose})</p> : null}
      {r.children?.length ? <p className="small">Child runs: {r.children.map((c: string) => <Link key={c} to={`/runs/${c}`}>{short(c)} </Link>)}</p> : null}
      {sc.model?.follows_embedded_instructions ? (
        <div className="banner info small">Simulated susceptible model: the reference planner acts on instructions it reads, to exercise the gateway.</div>
      ) : null}
      {!r.capabilities.restore_cursor ? <div className="banner warn small">Tool-only proxy run: host context and cursor are not visible; full-agent restore is unavailable.</div> : null}
      <details>
        <summary className="small">Health transitions ({r.health_transitions.length}) and limits</summary>
        <table>
          <tbody>
            {r.health_transitions.map((t: any) => (
              <tr key={t.id}><td className="small muted">{when(t.created_at)}</td><td><Status value={t.from_state} /> → <Status value={t.to_state} /></td><td>{t.reason_code}</td></tr>
            ))}
          </tbody>
        </table>
        <Json value={sc.limits} />
      </details>
    </div>
  );
}

function Inspector({ ev, runId }: { ev: any; runId: string }) {
  if (!ev) return <div className="card"><h2>Evidence inspector</h2><Empty>Select an event to see its evidence, decision, effect or context packet.</Empty></div>;
  const p = ev.payload || {};
  return (
    <div className="card">
      <h2>{ev.kind} <span className="muted small">#{ev.sequence}</span></h2>
      <KV items={[["Actor", ev.actor_type], ["Time", when(ev.timestamp_utc)], ["Policy", ev.policy_revision || "—"], ["Registry", ev.registry_revision ?? "—"]]} />
      {p.decision_id ? <DecisionPanel id={p.decision_id} /> : null}
      {ev.kind === "context.compiled" ? <PacketPanel id={p.packet_id} /> : null}
      {ev.kind === "incident.opened" ? <p><Link to={`/incidents/${p.incident_id}`}>Open incident →</Link></p> : null}
      {p.repair_run ? <p><Link to={`/runs/${p.repair_run}`}>Open repair run →</Link></p> : null}
      {p.source_id ? <p><Link to={`/sources/${p.source_id}`}>Open source {short(p.source_id)} →</Link></p> : null}
      {ev.evidence_refs?.length ? (
        <>
          <h3>Evidence references</h3>
          <ul className="small" style={{ paddingLeft: 18 }}>{ev.evidence_refs.map((r: string) => <li key={r} className="mono">{r}</li>)}</ul>
        </>
      ) : null}
      <details><summary className="small">Raw event payload (redacted)</summary><Json value={p} /></details>
    </div>
  );
}

function DecisionPanel({ id }: { id: string }) {
  const d = useApi(() => api(`/v1/decisions/${id}`), [id]);
  if (d.error) return <ErrorBox error={d.error} />;
  if (!d.data) return <Loading what="decision" />;
  const x = d.data;
  return (
    <>
      <h3>Decision</h3>
      <div className="row"><Status value={x.outcome} /> <b>{x.reason_code}</b> <Status value={x.attribution_status} /></div>
      <p>{x.outcome === "DENY" && !x.message.startsWith("Blocked before execution") ? "Blocked before execution. " : ""}{x.message}</p>
      <KV items={[["Tool", x.tool_id], ["Arguments hash", <span className="mono">{short(x.args_hash, 16)}</span>], ["Executor invocations", String(x.executor_invocations)],
                  ["Effect", x.effect ? `${x.effect.state} (${short(x.effect.operation_id)})` : "none reserved"], ["Task / policy", `rev ${x.task_revision} / ${x.policy_revision}`]]} />
      <h3>Checks in order</h3>
      <ul className="small" style={{ paddingLeft: 18 }}>{(x.checks || []).map((c: any, i: number) => <li key={i}>{c.check}: {c.result} {c.detail ? <span className="muted">({c.detail})</span> : null}</li>)}
        {x.outcome !== "ALLOW" ? <li><b>stopped at {x.reason_code}</b></li> : null}</ul>
      {x.audit ? <><h3>Constrained audit</h3><Json value={x.audit} /></> : null}
      <h3>Arguments (redacted view)</h3>
      <Json value={x.arguments_redacted} />
      {x.evidence?.length ? <h3>Linked source content</h3> : null}
      {(x.evidence || []).map((e: any) => (
        <div key={e.ref}>
          <div className="small"><span className="mono">{e.ref}</span> · {e.origin} · {e.channel} · offsets {e.offsets?.join("–")}</div>
          <div className="small muted">This source is linked to the action arguments. Its causal role is not yet verified.</div>
          <PlainText text={e.text} anchors={Object.values(x.arguments_redacted || {}).filter((v: any) => typeof v === "string" && v.length > 3 && v.length < 120) as string[]}
                     quarantined={(e.dispositions || []).filter((d: any) => d.disposition === "quarantined").flatMap((d: any) => JSON.parse(d.detail_json || "{}").spans || [])} />
        </div>
      ))}
    </>
  );
}

function PacketPanel({ id }: { id: string }) {
  const p = useApi(() => api(`/v1/packets/${id}`), [id]);
  const [tab, setTab] = useState("Included");
  if (!p.data) return <Loading what="packet" />;
  const x = p.data;
  return (
    <>
      <h3>Context packet</h3>
      <p className="small">~{x.token_count} of {x.budget_tokens} tokens ({x.count_kind}; {Math.round(x.margin * 100)}% safety margin, {x.output_reserve} reserved for output). Mandatory assignment and tool schemas are never truncated.</p>
      <Tabs tabs={["Included", "Lessons", "Omitted", "Assignment"]} value={tab} onChange={setTab} />
      {tab === "Included" ? x.observations.map((o: any) => (
        <div key={o.ref}><div className="small"><span className="mono">{o.ref}</span> · {o.origin} · {o.channel}{o.excluded_spans ? ` · ${o.excluded_spans} excluded span(s)` : ""}</div><PlainText text={o.text} /></div>
      )) : null}
      {tab === "Lessons" ? (x.lessons.length ? x.lessons.map((l: any) => <div key={l.ref}><b>{l.ref}</b> {l.under_test ? <Badge kind="violet">under test</Badge> : null}<Json value={{ conditions: l.conditions, procedure: l.procedure }} /></div>) : <Empty>No lessons included.</Empty>) : null}
      {tab === "Omitted" ? (x.omitted.length ? <ul className="small" style={{ paddingLeft: 18 }}>{x.omitted.map((o: any, i: number) => <li key={i}><span className="mono">{o.ref}</span>: {o.reason}</li>)}</ul> : <Empty>Nothing omitted.</Empty>) : null}
      {tab === "Assignment" ? <Json value={x.mandatory.assignment} /> : null}
    </>
  );
}
