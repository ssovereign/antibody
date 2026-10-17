import React, { useEffect, useState } from "react";
import { api, currentProject, q, short, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Link, Loading, ModeBadge, Status, useAction, useApi } from "../ui";

const STATES = ["COMPLETED", "FAILED", "UNKNOWN", "DENIED", "RESERVED", "DISPATCHING"];
const KINDS: [string, string][] = [["external_send", "Messages sent out"], ["payment", "Payments"], ["delete", "Deletions"], ["write_local", "File writes"],
  ["read_network", "Web reads"], ["read", "Reads"]];

export default function Effects({ runId }: { runId?: string }) {
  const pid = currentProject();
  const runs = useApi(() => api(`/v1/runs${q({ project_id: pid })}`), [pid]);
  const agents = useApi(() => api(`/v1/agents${q({ project_id: pid })}`), [pid]);
  const [agent, setAgent] = useState("");
  const [state, setState] = useState("");
  const [kind, setKind] = useState("");
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const [children, setChildren] = useState(true);
  const [hideReads, setHideReads] = useState(true);
  useEffect(() => {
    const h = setTimeout(() => setSearch(text.trim()), 300);
    return () => clearTimeout(h);
  }, [text]);
  const effects = useApi(() => api(`/v1/effects${q({ project_id: pid, run_id: runId, agent_id: agent, state, effect_class: kind, q: search,
    include_children: runId && children ? "true" : undefined, external_only: hideReads && !kind ? "true" : undefined })}`),
    [pid, runId, agent, state, kind, search, children, hideReads]);
  const outbox = useApi(() => api(`/v1/outbox${q({ project_id: pid })}`), [pid]);
  const { busy, run } = useAction();
  const reload = () => { effects.reload(); outbox.reload(); };
  const reconcile = (op: string) => run(op, () => api(`/v1/effects/${op}/reconcile`, { method: "POST", body: { project_id: pid } }).then(reload),
    "Reconciled with the destination's record");
  const agentName = (id: string) => (agents.data || []).find((a: any) => a.agent_id === id)?.name || short(id);
  const pickRun = (id: string) => { window.location.hash = id ? `#/effects/${id}` : "#/effects"; };
  const clear = () => { setAgent(""); setState(""); setKind(""); setText(""); setHideReads(true); pickRun(""); };
  const filtersOn = !!(runId || agent || state || kind || search || !hideReads);
  const box = outbox.data?.entries || [];
  return (
    <Page title="Effects & outbox" sub="Everything agents did outside their own heads. Restores never erase this record.">
      <div className="card">
        <h2>Find effects</h2>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="field" style={{ marginBottom: 0, flex: "1 1 220px", minWidth: 0, maxWidth: 360 }}>Run
            <select value={runId || ""} onChange={(e) => pickRun(e.target.value)} aria-label="Run" style={{ width: "100%", maxWidth: 340 }}>
              <option value="">All runs</option>
              {(runs.data || []).map((r: any) => (
                <option key={r.run_id} value={r.run_id}>{short(r.run_id)} · {agentName(r.agent_id)} · {r.task_id} · {r.status_reason || r.status}</option>
              ))}
            </select>
          </label>
          <label className="field" style={{ marginBottom: 0 }}>Agent
            <select value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Agent">
              <option value="">Any agent</option>
              {(agents.data || []).map((a: any) => <option key={a.agent_id} value={a.agent_id}>{a.name}</option>)}
            </select>
          </label>
          <label className="field" style={{ marginBottom: 0 }}>Kind
            <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind">
              <option value="">Any kind</option>
              {KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          </label>
          <label className="field" style={{ marginBottom: 0 }}>Outcome
            <select value={state} onChange={(e) => setState(e.target.value)} aria-label="Outcome">
              <option value="">Any outcome</option>
              {STATES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label className="field" style={{ marginBottom: 0, flex: "1 1 200px", minWidth: 0 }}>Search
            <input value={text} onChange={(e) => setText(e.target.value)} placeholder="address, file, operation or run ID" aria-label="Search effects" />
          </label>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <label className="check"><input type="checkbox" checked={hideReads} disabled={!!kind} onChange={(e) => setHideReads(e.target.checked)} /> Hide plain reads</label>
          {runId ? <label className="check"><input type="checkbox" checked={children} onChange={(e) => setChildren(e.target.checked)} /> Include runs it started (repairs, continuations)</label> : null}
          {filtersOn ? <button onClick={clear}>Clear filters</button> : null}
        </div>
      </div>

      {runId ? <RunImpact runId={runId} children={children} onReconcile={reconcile} busy={busy} /> : null}

      <div className="card">
        <h2>Effects <span className="muted small">{effects.data ? `${effects.data.length} shown` : ""}</span></h2>
        <ErrorBox error={effects.error} retry={effects.reload} />
        {effects.loading && !effects.data ? <Loading /> : null}
        {effects.data?.length === 0 ? <Empty>Nothing matches these filters.</Empty> : null}
        {effects.data?.length ? (
          <table>
            <thead><tr><th>What happened</th><th>Outcome</th><th>Agent · task</th><th>Run</th><th>When</th><th /></tr></thead>
            <tbody>
              {effects.data.map((e: any) => (
                <tr key={e.operation_id}>
                  <td><b>{e.summary}</b><div className="small muted mono">{short(e.operation_id)} · {e.tool_id} ({e.effect_class})</div></td>
                  <td><Status value={e.state} />{e.reason_code && e.state === "DENIED" ? <div className="small muted">{e.reason_code}</div> : null}
                    {e.error?.code ? <div className="small muted">{e.error.code}</div> : null}</td>
                  <td className="small">{e.agent_name}<div className="muted">{e.task_id}@{e.task_revision}</div></td>
                  <td className="small">
                    <a href={`#/effects/${e.run_id}`} title="Show everything this run did">{short(e.run_id)}</a>
                    {e.run_purpose !== "task" ? <div className="muted">{e.run_purpose}</div> : null}
                  </td>
                  <td className="small muted">{when(e.updated_at)}</td>
                  <td>{e.state === "UNKNOWN" ? <Btn kind="primary" onClick={() => reconcile(e.operation_id)} busy={busy === e.operation_id}>Reconcile</Btn> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>

      <div className="card">
        <h2>Local outbox (test destination) <span className="muted small">{box.length} message(s)</span></h2>
        {box.length === 0 ? <Empty>No messages delivered.</Empty> : (
          <table>
            <thead><tr><th>Recipient</th><th>Artifact</th><th>Sent by run</th><th>Operation</th><th>Sent</th></tr></thead>
            <tbody>{box.filter((x: any) => !runId || x.run_id === runId || (effects.data || []).some((e: any) => e.operation_id === x.operation_id)).map((x: any) => (
              <tr key={x.entry_id}><td>{x.recipient_id}<div className="small muted">{x.address}</div></td><td className="small">{x.artifact_path} ({x.body_bytes} bytes)</td>
                <td className="small"><a href={`#/effects/${x.run_id}`}>{short(x.run_id)}</a></td>
                <td className="mono small">{short(x.operation_id)}</td><td className="small muted">{when(x.sent_at)}</td></tr>
            ))}</tbody>
          </table>
        )}
        <p className="small muted">Restoring an agent never unsends these. A retry of the same logical operation reuses its operation ID and is deduplicated.</p>
      </div>
    </Page>
  );
}

function RunImpact({ runId, children, onReconcile, busy }: { runId: string; children: boolean; onReconcile: (op: string) => void; busy: string | null }) {
  const d = useApi(() => api(`/v1/runs/${runId}/impact${q({ include_children: children ? "true" : "false" })}`), [runId, children, busy]);
  if (d.error) return <div className="card"><ErrorBox error={d.error} retry={d.reload} /></div>;
  if (!d.data) return <div className="card"><Loading what="run impact" /></div>;
  const x = d.data;
  const r = x.run;
  const c = x.counts;
  return (
    <div className="card" style={{ borderColor: c.unknown || c.messages_sent || c.payments ? "var(--warn)" : undefined }}>
      <h2>What did this run do?</h2>
      <div className="row" style={{ marginBottom: 10 }}>
        <b>{r.agent_name}</b> <span className="muted">on</span> <b>{r.task_id}@{r.task_revision}</b>
        <Status value={r.status} /> <span className="small muted">{r.status_reason}</span> <Status value={r.health} /> <ModeBadge mode={r.mode} />
        <Link to={`/runs/${r.run_id}`}>Open run timeline →</Link>
      </div>
      {x.family.length > 1 ? <p className="small">Includes {x.family.length - 1} run(s) it started: {x.family.slice(1).map((id: string) => <a key={id} href={`#/runs/${id}`} style={{ marginRight: 6 }}>{short(id)}</a>)}</p> : null}
      <div className="grid2">
        {[["Messages sent", c.messages_sent, c.messages_sent ? "warn" : ""], ["Payments made", c.payments, c.payments ? "bad" : ""],
          ["File changes", c.file_changes, ""], ["Blocked attempts", c.blocked, ""], ["Outcome unknown", c.unknown, c.unknown ? "warn" : ""],
          ["Sentences quarantined", c.quarantined_spans, ""]].map(([l, v, tone]) => (
          <div className="stat" key={l as string} style={tone ? { background: `var(--${tone}-soft)` } : undefined}>
            <div className="v">{v as number}</div><div className="l">{l}</div>
          </div>
        ))}
      </div>

      <h3>What to do</h3>
      <ol style={{ paddingLeft: 20, margin: "0 0 8px" }}>
        {x.mitigation.map((m: any, i: number) => (
          <li key={i} style={{ marginBottom: 6 }}>
            {m.text}
            {m.kind === "files" && m.base_version ? <> <Link to={`/history/${r.agent_id}`}>Go to version history →</Link> <span className="small muted">(start version {short(m.base_version)})</span></> : null}
            {m.kind === "reconcile" ? <div className="row" style={{ marginTop: 4 }}>{m.operation_ids.map((op: string) => (
              <Btn key={op} kind="primary" onClick={() => onReconcile(op)} busy={busy === op}>Reconcile {short(op)}</Btn>))}</div> : null}
          </li>
        ))}
      </ol>

      {x.messages.length ? <>
        <h3>Messages that left</h3>
        <ul className="small" style={{ paddingLeft: 18 }}>{x.messages.map((m: any) => <li key={m.entry_id}>{m.artifact_path} → <b>{m.recipient_id}</b> ({m.address}) at {when(m.sent_at)}</li>)}</ul>
      </> : null}
      {x.files.length ? <>
        <h3>Files changed (inside the agent's workspace)</h3>
        <ul className="small" style={{ paddingLeft: 18 }}>{x.files.map((f: any) => <li key={f.operation_id}>{f.summary} <span className="muted">· branch {short(f.branch_id)}</span></li>)}</ul>
      </> : null}
      {x.blocked.length ? <>
        <h3>Stopped before running</h3>
        <ul className="small" style={{ paddingLeft: 18 }}>{x.blocked.map((b: any) => (
          <li key={b.decision_id}><Status value={b.outcome} /> {b.tool_id} {Object.values(b.arguments).filter((v: any) => typeof v === "string" && v.length < 80).join(" · ")} <span className="muted">({b.reason_code})</span></li>
        ))}</ul>
      </> : null}
      {x.incidents.length ? <>
        <h3>Related incidents</h3>
        <div className="row">{x.incidents.map((i: any) => <Link key={i.incident_id} to={`/incidents/${i.incident_id}`}><Badge kind="warn" plain>{i.category.replace(/_/g, " ")} · {i.status}</Badge></Link>)}</div>
      </> : null}
      <p className="small muted" style={{ marginTop: 10, marginBottom: 0 }}>{x.note}</p>
    </div>
  );
}
