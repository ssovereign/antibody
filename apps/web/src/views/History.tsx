import React, { useMemo, useState } from "react";
import { api, currentProject, newKey, q, short, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Json, KV, Link, Loading, Status, Tabs, useAction, useApi } from "../ui";

export default function History({ agentId }: { agentId?: string }) {
  const pid = currentProject();
  const agents = useApi(() => api(`/v1/agents${q({ project_id: pid })}`), [pid]);
  const aid = agentId || agents.data?.[0]?.agent_id;
  const versions = useApi(() => (aid ? api(`/v1/versions${q({ project_id: pid, agent_id: aid })}`) : Promise.resolve([])), [pid, aid]);
  const branches = useApi(() => (aid ? api(`/v1/branches${q({ project_id: pid, agent_id: aid })}`) : Promise.resolve([])), [pid, aid]);
  const [a, setA] = useState<string | null>(null);
  const [b, setB] = useState<string | null>(null);
  const reload = () => { versions.reload(); branches.reload(); };
  const agent = (agents.data || []).find((x: any) => x.agent_id === aid);
  return (
    <Page
      title="Version history"
      sub="RECEIPT: save, branch, compare, keep selected changes, restore"
      actions={
        <select value={aid || ""} onChange={(e) => { window.location.hash = `#/history/${e.target.value}`; setA(null); setB(null); }} aria-label="Agent">
          {(agents.data || []).map((x: any) => <option key={x.agent_id} value={x.agent_id}>{x.name}</option>)}
        </select>
      }
    >
      <ErrorBox error={versions.error || branches.error} retry={reload} />
      {agents.loading && !agents.data ? <Loading what="agents" /> : null}
      {agents.data && !aid ? <Empty>No agents.</Empty> : null}
      {agent && agent.adapter === "claude_code_hooks" ? (
        <div className="banner">This is a Claude Code project. Its saved states (files, exact conversation and memory), going back and negotiated
          merges are on <a href={`#/claude-code/${agent.agent_id}`}>Claude Code history</a>.</div>
      ) : agent && !agent.capabilities.restore_cursor ? (
        <div className="banner warn">This agent uses the tool-only proxy. Tool history and supported workspace versions are available; full-agent restore is disabled.</div>
      ) : null}
      {aid && versions.data && branches.data ? (
        <>
          <Graph versions={versions.data} branches={branches.data} acceptedBranch={agent?.accepted_branch_id} a={a} b={b}
                 onPick={(v) => { if (!a || (a && b)) { setA(v); setB(null); } else setB(v); }} />
          <div className="split">
            <div>
              {a && b ? <Compare a={a} b={b} branches={branches.data} onMerged={reload} /> : (
                <div className="card"><Empty>Select two versions in the graph to compare them ({a ? "now pick the second" : "pick the first"}).</Empty></div>
              )}
            </div>
            <div className="sticky">
              {a ? <VersionPanel id={a} onFork={reload} /> : null}
              <BranchPanel branches={branches.data} onChange={reload} />
            </div>
          </div>
        </>
      ) : <Loading what="versions" />}
    </Page>
  );
}

function Graph({ versions, branches, acceptedBranch, a, b, onPick }: { versions: any[]; branches: any[]; acceptedBranch?: string; a: string | null; b: string | null; onPick: (v: string) => void }) {
  const lanes = useMemo(() => {
    const order = [...branches].sort((x, y) => (x.branch_id === acceptedBranch ? -1 : y.branch_id === acceptedBranch ? 1 : x.created_at.localeCompare(y.created_at)));
    return Object.fromEntries(order.map((br: any, i: number) => [br.branch_id, i]));
  }, [branches, acceptedBranch]);
  const heads = new Set(branches.map((x: any) => x.head_version));
  const pos: Record<string, { x: number; y: number }> = {};
  versions.forEach((v, i) => { pos[v.version_id] = { x: 30 + i * 64, y: 26 + (lanes[v.branch_id] ?? 0) * 46 }; });
  const width = Math.max(400, 60 + versions.length * 64 + 160);
  const height = 40 + Math.max(1, Object.keys(lanes).length) * 46;
  const bname = (id: string) => branches.find((x: any) => x.branch_id === id)?.name || short(id);
  return (
    <div className="card" style={{ overflowX: "auto" }}>
      <h2>Graph <span className="muted small">{versions.length} versions · blue = accepted head · click two nodes to compare</span></h2>
      <svg className="graph" width={width} height={height} role="img" aria-label="Version graph">
        {Object.entries(lanes).map(([bid, i]) => (
          <text key={bid} x={width - 150} y={30 + (i as number) * 46}>{bname(bid)}</text>
        ))}
        {versions.flatMap((v) => v.parents.filter((p: string) => pos[p]).map((p: string) => (
          <line key={v.version_id + p} x1={pos[p].x} y1={pos[p].y} x2={pos[v.version_id].x} y2={pos[v.version_id].y} stroke="var(--line-strong)" strokeWidth={2} />
        )))}
        {versions.map((v) => {
          const pt = pos[v.version_id];
          const isHead = heads.has(v.version_id);
          const accepted = isHead && v.branch_id === acceptedBranch;
          const picked = v.version_id === a || v.version_id === b;
          return (
            <g key={v.version_id} className="node" transform={`translate(${pt.x},${pt.y})`} onClick={() => onPick(v.version_id)} tabIndex={0}
               onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onPick(v.version_id)} aria-label={`${v.label || "version"} ${short(v.version_id)}`}>
              <title>{`${v.label || "unlabeled"}\n${v.version_id}\n${when(v.created_at)}\nlessons: ${v.lesson_revisions.join(", ") || "none"}${v.requires_reconciliation ? "\nrequires reconciliation" : ""}`}</title>
              <circle r={picked ? 10 : 8} fill={accepted ? "var(--accent)" : v.requires_reconciliation ? "var(--warn)" : isHead ? "var(--ok)" : "var(--ink-3)"}
                      stroke={picked ? "var(--focus)" : undefined} strokeWidth={picked ? 3 : 2} />
              {picked ? <text x={-4} y={4} fill="#fff" style={{ fill: "#fff", fontWeight: 700 }}>{v.version_id === a ? "A" : "B"}</text> : null}
              <text x={-18} y={22}>{(v.label || short(v.version_id, 6)).slice(0, 12)}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function Compare({ a, b, branches, onMerged }: { a: string; b: string; branches: any[]; onMerged: () => void }) {
  const pid = currentProject();
  const d = useApi(() => api(`/v1/compare${q({ project_id: pid, a, b })}`), [a, b]);
  const [tab, setTab] = useState("Summary");
  const [selected, setSelected] = useState<string[]>([]);
  const [target, setTarget] = useState<string>("");
  const [conflicts, setConflicts] = useState<any[] | null>(null);
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const { busy, run } = useAction();
  if (d.error) return <div className="card"><ErrorBox error={d.error} /></div>;
  if (!d.data) return <div className="card"><Loading what="comparison" /></div>;
  const c = d.data.changes;
  const merge = () => run("merge", async () => {
    try {
      await api("/v1/merges", { method: "POST", body: { project_id: pid, target_branch: target, source_version: b, selected_changes: selected, resolutions }, idem: newKey("merge") });
      setConflicts(null); onMerged();
    } catch (e: any) {
      if (e.code === "MERGE_CONFLICT") setConflicts(e.detail.conflicts);
      throw e;
    }
  }, "Selected changes merged after combined checks passed");
  return (
    <div className="card">
      <h2>Compare <span className="mono small">A {short(a)}</span> → <span className="mono small">B {short(b)}</span></h2>
      <Tabs tabs={["Summary", "Lessons", "Memory", "Files", "References", "Keep selected changes"]} value={tab} onChange={setTab} />
      {tab === "Summary" ? (d.data.summary.length ? (
        <ul style={{ paddingLeft: 18 }}>{d.data.summary.map((s: any, i: number) => <li key={i}>{s.text} <span className="mono small muted">({s.path})</span></li>)}</ul>
      ) : <Empty>No differences in supported state.</Empty>) : null}
      {tab === "Lessons" ? (c.lessons ? c.lessons.map((l: any) => (
        <div key={l.lesson_id} style={{ marginBottom: 10 }}>
          <b>{l.lesson_id}</b> {l.before || "∅"} → {l.after || "∅"} {l.revoked ? <Status value="revoked" /> : null}
          <KV items={[["Kind", l.kind], ["Conditions", JSON.stringify(l.conditions)], ["Procedure", JSON.stringify(l.procedure)], ["Evaluation", l.evaluation_id ? <Link to={`/evaluation/${l.evaluation_id}`}>{short(l.evaluation_id)}</Link> : "—"]]} />
        </div>
      )) : <Empty>No lesson changes.</Empty>) : null}
      {tab === "Memory" ? (c.memory ? c.memory.map((m: any) => (
        <div key={m.key}><b>{m.key}</b>{(m.after || m.before)?.seeded_test_condition ? <> <Badge kind="warn" plain>seeded test condition</Badge></> : null}<Json value={{ before: m.before?.value, after: m.after?.value, origin: (m.after || m.before)?.origin }} /></div>
      )) : <Empty>No memory changes.</Empty>) : null}
      {tab === "Files" ? (c.files ? c.files.map((f: any) => (
        <div key={f.path} style={{ marginBottom: 10 }}>
          <div><Badge kind={f.status === "added" ? "ok" : f.status === "deleted" ? "bad" : "info"} plain>{f.status}</Badge> <span className="mono">{f.path}</span></div>
          {f.text_diff ? <pre className="plain">{f.text_diff.map((l: string, i: number) => <div key={i} className={`diffline${l.startsWith("+") ? " add" : l.startsWith("-") ? " del" : ""}`}>{l}</div>)}</pre>
            : f.text_diff === null ? <p className="small muted">Binary or non-UTF-8 content (for example UTF-16 output).</p> : null}
        </div>
      )) : <Empty>No file changes.</Empty>) : null}
      {tab === "References" ? <Json value={{ references: c.references, evaluations: c.evaluations, model: c.model, environment: c.environment, disabled_lessons: c.disabled_lessons }} /> : null}
      {tab === "Keep selected changes" ? (
        <>
          <p className="small">Three-way merge of B's selected records into a branch, against the common ancestor. Credentials, effect history and grants are never merged.</p>
          {d.data.change_keys.length === 0 ? <Empty>Nothing to select.</Empty> : d.data.change_keys.map((k: string) => (
            <label key={k} className="check"><input type="checkbox" checked={selected.includes(k)} onChange={(e) => setSelected(e.target.checked ? [...selected, k] : selected.filter((x) => x !== k))} /> <span className="mono">{k}</span></label>
          ))}
          <div className="row" style={{ marginTop: 8 }}>
            <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Target branch">
              <option value="">Target branch…</option>
              {branches.map((br: any) => <option key={br.branch_id} value={br.branch_id}>{br.name}{br.clean ? "" : " (unsaved changes)"}</option>)}
            </select>
            <Btn kind="primary" onClick={merge} busy={busy === "merge"} disabledReason={!selected.length ? "Select changes" : !target ? "Choose a target" : branches.find((x: any) => x.branch_id === target)?.clean === false ? "Save the target branch first" : null}>Test and merge</Btn>
          </div>
          {conflicts ? (
            <div className="banner warn" style={{ display: "block", marginTop: 10 }}>
              <b>Conflicts</b> — choose a side explicitly or create a new evaluated candidate.
              {conflicts.map((cf: any) => (
                <div key={cf.key} className="row small" style={{ marginTop: 6 }}>
                  <span className="mono">{cf.key}</span> ({cf.kind})
                  <label className="check"><input type="radio" name={cf.key} onChange={() => setResolutions({ ...resolutions, [cf.key]: "current" })} /> keep current</label>
                  <label className="check"><input type="radio" name={cf.key} onChange={() => setResolutions({ ...resolutions, [cf.key]: "candidate" })} /> take B</label>
                </div>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function VersionPanel({ id, onFork }: { id: string; onFork: () => void }) {
  const pid = currentProject();
  const v = useApi(() => api(`/v1/versions/${id}`), [id]);
  const prev = useApi(() => api(`/v1/versions/${id}/restore-preview${q({ project_id: pid })}`), [id]);
  const [name, setName] = useState("");
  const { busy, run } = useAction();
  const fork = (purpose: string) => run("fork", async () => {
    const r = await api(`/v1/versions/${id}/fork`, { method: "POST", body: { project_id: pid, name: name || undefined, purpose }, idem: newKey("fork") });
    onFork();
    return r;
  }, purpose === "restore" ? "Restored into a new branch. Completed external actions remain recorded." : "Branch created with isolated files and memory");
  if (!v.data) return <div className="card"><Loading what="version" /></div>;
  const m = v.data.version.manifest;
  const p = prev.data;
  return (
    <div className="card">
      <h2>Version A <span className="mono small">{short(id, 12)}</span></h2>
      <KV items={[["Label", v.data.version.label || "—"], ["Created", when(v.data.version.created_at)], ["Parents", m.parent_ids.map((x: string) => short(x)).join(", ") || "root"],
                  ["Lessons pinned", m.lesson_revisions.join(", ") || "none"], ["Task", m.task_revision_ref || "—"], ["Policy then", m.historical_policy_ref], ["Registry then", String(m.registry_revision)],
                  ["Files", String(Object.keys(v.data.state.files).length)], ["Memory facts", Object.keys(v.data.state.memory_facts).join(", ") || "none"], ["Effect cursor", m.effect_cursor_ref]]} />
      {m.requires_reconciliation ? <div className="banner warn small">Saved while an effect outcome was unknown: reconciliation required before continuing.</div> : null}
      <h3>Restore preview</h3>
      {p ? (
        <>
          <p className="small">{p.message}</p>
          <KV items={[["Policy", `${p.policy.historical} then → ${p.policy.current} now`], ["Policy differences", p.policy.differences.length ? p.policy.differences.map((d: any) => d.field).join(", ") : "none"],
                      ["Excluded knowledge", p.excluded_knowledge.length ? p.excluded_knowledge.map((x: any) => x.ref).join(", ") : "none"],
                      ["Pending effects", p.pending_effects.length ? `${p.pending_effects.length} unknown/in-flight` : "none"], ["Unsupported", p.unsupported.join(", ") || "none"]]} />
          {p.task ? <p className="small">{p.task}</p> : null}
        </>
      ) : <Loading />}
      <label className="field">New branch name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="optional" /></label>
      <div className="row">
        <Btn onClick={() => fork("work")} busy={busy === "fork"}>Branch from A</Btn>
        <Btn kind="primary" onClick={() => fork("restore")} busy={busy === "fork"} disabledReason={p && !p.capabilities.restore_cursor ? "Adapter cannot restore an agent cursor" : null}>Restore A into new branch</Btn>
      </div>
    </div>
  );
}

function BranchPanel({ branches, onChange }: { branches: any[]; onChange: () => void }) {
  const pid = currentProject();
  const [bid, setBid] = useState<string>(branches[0]?.branch_id || "");
  React.useEffect(() => {
    if (!branches.find((x: any) => x.branch_id === bid)) setBid(branches[0]?.branch_id || "");
  }, [branches]);
  const [key, setKey] = useState("");
  const [val, setVal] = useState("");
  const [seeded, setSeeded] = useState(false);
  const { busy, run } = useAction();
  const br = branches.find((x: any) => x.branch_id === bid);
  const save = () => run("save", () => api("/v1/versions", { method: "POST", body: { project_id: pid, branch_id: bid, expected_head: br?.head_version || "", label: `saved ${new Date().toLocaleTimeString()}` }, idem: newKey("save") }).then(onChange), "Version saved at a stable boundary");
  const mem = (del: boolean, k?: string) => run("mem", () => api(`/v1/branches/${bid}/memory`, { method: "POST", body: { project_id: pid, key: k || key, value: del ? null : val, delete: del, seeded_test_condition: seeded } }).then(() => { setKey(""); setVal(""); onChange(); }), del ? "Memory fact removed on this branch" : "Memory fact set on this branch");
  const disable = (ref: string) => run("dis", () => api(`/v1/branches/${bid}/lessons`, { method: "POST", body: { project_id: pid, ref, disabled: true } }).then(onChange), "Lesson removed from this branch only (not revoked)");
  return (
    <div className="card">
      <h2>Branch working state</h2>
      <select value={bid} onChange={(e) => setBid(e.target.value)} aria-label="Branch">
        {branches.map((x: any) => <option key={x.branch_id} value={x.branch_id}>{x.name} ({x.purpose})</option>)}
      </select>
      {br ? (
        <>
          <KV items={[["Head", <span className="mono">{short(br.head_version)}</span>], ["Unsaved changes", br.clean ? "no" : "yes"], ["Workspace", "isolated directory"],
                      ["Environment", JSON.stringify(br.state.environment)]]} />
          <div className="row" style={{ marginTop: 8 }}><Btn onClick={save} busy={busy === "save"} disabledReason={br.clean ? "No unsaved changes" : null}>Save version</Btn></div>
          <h3>Pinned lessons</h3>
          {br.state.lesson_revisions.length ? br.state.lesson_revisions.map((r: string) => (
            <div key={r} className="row small"><span className="mono">{r}</span><button onClick={() => disable(r)}>Remove from branch</button></div>
          )) : <p className="small muted">None pinned (accepted project lessons still apply when compatible).</p>}
          {br.state.disabled_lessons.length ? <p className="small">Disabled here: {br.state.disabled_lessons.join(", ")}</p> : null}
          <h3>Memory facts</h3>
          {Object.entries(br.state.memory_facts).length ? Object.entries(br.state.memory_facts).map(([k, f]: any) => (
            <div key={k} className="row small"><b>{k}</b> = <code>{JSON.stringify(f.value)}</code> {f.seeded_test_condition ? <Badge kind="warn" plain>seeded</Badge> : null}<span className="muted">({f.origin})</span><button onClick={() => mem(true, k)}>Remove</button></div>
          )) : <p className="small muted">No memory facts.</p>}
          <div className="row" style={{ marginTop: 6 }}>
            <input placeholder="key" value={key} onChange={(e) => setKey(e.target.value)} aria-label="Memory key" style={{ width: 150 }} />
            <input placeholder="value" value={val} onChange={(e) => setVal(e.target.value)} aria-label="Memory value" style={{ width: 120 }} />
            <label className="check small"><input type="checkbox" checked={seeded} onChange={(e) => setSeeded(e.target.checked)} /> seeded test condition</label>
            <Btn onClick={() => mem(false)} busy={busy === "mem"} disabledReason={!key ? "Enter a key" : null}>Set</Btn>
          </div>
        </>
      ) : null}
    </div>
  );
}
