import React, { useEffect, useMemo, useState } from "react";
import { api, currentProject, q, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Loading, useAction, useApi } from "../ui";

const ROW = 34;
const LANE = 26;
const PAD = 16;
const COLORS = ["var(--accent)", "var(--ok)", "var(--violet)", "var(--warn)", "var(--bad)"];

export default function ClaudeCode({ agentId }: { agentId?: string }) {
  const pid = currentProject();
  const list = useApi(() => api(`/v1/integrations/claude-code${q({ project_id: pid })}`), [pid]);
  const projects = list.data?.projects || [];
  const chosen = agentId || projects[0]?.agent_id;
  return (
    <Page title="Claude Code history">
      <ErrorBox error={list.error} />
      {!list.data ? <Loading what="Claude Code projects" /> : !projects.length ? (
        <Empty>No Claude Code project is guarded yet. Install the guard in Settings → Guard Claude Code in a project folder.</Empty>
      ) : (
        <>
          {projects.length > 1 ? (
            <div className="row" style={{ marginBottom: 12 }}>
              {projects.map((p: any) => (
                <a key={p.agent_id} href={`#/claude-code/${p.agent_id}`}><Badge kind={p.agent_id === chosen ? "info" : "neutral"}>{p.name}</Badge></a>
              ))}
            </div>
          ) : null}
          {chosen ? <ProjectHistory agentId={chosen} /> : null}
        </>
      )}
    </Page>
  );
}

function ProjectHistory({ agentId }: { agentId: string }) {
  const t = useApi(() => api(`/v1/claude-code/agents/${agentId}/tree`), [agentId]);
  const [sel, setSel] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  const { busy, run } = useAction();
  useEffect(() => { setSel(null); setResult(null); }, [agentId]);
  if (t.error) return <ErrorBox error={t.error} retry={t.reload} />;
  if (!t.data) return <Loading what="history" />;
  const d = t.data;
  const cmd = (line: string, key: string, ok?: string) => run(key, async () => {
    let r = await api(`/v1/claude-code/agents/${agentId}/command`, { method: "POST", body: { line } });
    if (r.queued) {
      // hosted: the command runs through the connector on the user's computer (fresh snapshot in, file changes out)
      setResult({ line, waiting: true, output: "Waiting for your computer to run it…" });
      const started = Date.now();
      for (;;) {
        await new Promise((res) => setTimeout(res, 1500));
        const c = await api(`/v1/claude-code/commands/${r.command_id}`);
        if (c.status === "done") { r = c.result || { ok: false, output: "No result." }; break; }
        if (c.status === "queued" && Date.now() - started > 15000) {
          setResult({ line, waiting: true, output: "Still waiting: is your computer connected? Start start-antibody (Settings → Connect your computer)." });
        }
        if (Date.now() - started > 30 * 60 * 1000) { r = { ok: false, output: "Timed out waiting for your computer." }; break; }
      }
    }
    setResult({ line, ...r });
    t.reload();
    return r;
  }, ok);
  const current = d.branches.find((b: any) => b.branch_id === d.current_branch_id);
  const others = d.branches.filter((b: any) => b.branch_id !== d.current_branch_id && b.head_version);
  return (
    <div className="split">
      <div>
        <div className="card">
          <h2>{d.agent.name}</h2>
          <p className="small muted mono">{d.agent.project_dir}</p>
          <p className="small">Every dot is a saved state: the folder's files, Claude's exact conversation and its memory. One is saved
            automatically before each prompt ("before: …"). Click a dot to look inside or go back to it. You are on{" "}
            <b>{current?.name}</b>.</p>
          <Graph d={d} sel={sel} onSel={(v) => { setSel(v); setResult(null); }} />
        </div>
        <div className="card">
          <h2>Merge another branch into {current?.name}</h2>
          <p className="small">Both agents vote on every change from their own saved conversations; a third agent settles what they still
            disagree on. Takes a few minutes and uses Claude.</p>
          {others.length ? (
            <div className="row">
              {others.map((b: any) => (
                <Btn key={b.branch_id} onClick={() => cmd(`merge ${b.name}`, `m:${b.name}`, "Merge finished")} busy={busy === `m:${b.name}`}>
                  Merge {b.name}</Btn>
              ))}
            </div>
          ) : <p className="small muted">No other branch with saved work yet.</p>}
        </div>
        <Lessons d={d} />
      </div>
      <div>
        {result ? <CommandResult r={result} /> : null}
        {sel ? <VersionPanel agentId={agentId} versionId={sel} d={d} busy={busy}
                             onRestore={(v) => cmd(`restore ${v.slice(7, 19)}`, "restore", "Restored")}
                             onDiff={(v) => cmd(`diff ${v.slice(7, 19)}`, "diff")} />
          : <div className="card"><p className="small muted">Select a saved state in the graph.</p></div>}
      </div>
    </div>
  );
}

function Graph({ d, sel, onSel }: { d: any; sel: string | null; onSel: (v: string) => void }) {
  const layout = useMemo(() => {
    const lanes: Record<string, number> = {};
    d.branches.slice().sort((a: any, b: any) => a.created_at.localeCompare(b.created_at)).forEach((b: any, i: number) => { lanes[b.branch_id] = i; });
    const vs = d.versions.slice().reverse(); // newest first
    const row: Record<string, number> = {};
    vs.forEach((v: any, i: number) => { row[v.version_id] = i; });
    const heads: Record<string, string> = {};
    d.branches.forEach((b: any) => { if (b.head_version) heads[b.head_version] = heads[b.head_version] ? `${heads[b.head_version]}, ${b.name}` : b.name; });
    return { lanes, vs, row, heads, nLanes: Math.max(1, d.branches.length) };
  }, [d]);
  const x = (bid: string) => PAD + (layout.lanes[bid] ?? 0) * LANE;
  const y = (vid: string) => PAD + layout.row[vid] * ROW;
  const width = PAD * 2 + (layout.nLanes - 1) * LANE;
  const curHead = d.branches.find((b: any) => b.branch_id === d.current_branch_id)?.head_version;
  return (
    <div style={{ overflowX: "auto" }}>
      <div style={{ position: "relative", minWidth: width + 360, height: PAD * 2 + layout.vs.length * ROW }}>
        <svg width={width} height={PAD * 2 + layout.vs.length * ROW} style={{ position: "absolute", left: 0, top: 0 }} aria-hidden="true">
          {layout.vs.map((v: any) => v.parents.filter((p: string) => layout.row[p] !== undefined).map((p: string) => {
            const pv = d.versions.find((z: any) => z.version_id === p);
            const x1 = x(v.branch_id), y1 = y(v.version_id), x2 = x(pv.branch_id), y2 = y(p);
            const c = COLORS[(layout.lanes[v.branch_id] ?? 0) % COLORS.length];
            return <path key={`${v.version_id}-${p}`} d={`M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`}
                         stroke={c} strokeWidth={2} fill="none" opacity={0.7} />;
          }))}
          {layout.vs.map((v: any) => {
            const c = COLORS[(layout.lanes[v.branch_id] ?? 0) % COLORS.length];
            return <circle key={v.version_id} cx={x(v.branch_id)} cy={y(v.version_id)} r={v.version_id === sel ? 7 : v.is_merge ? 6 : 5}
                           fill={v.version_id === sel ? c : "var(--surface)"} stroke={c} strokeWidth={v.is_merge ? 3 : 2} />;
          })}
        </svg>
        {layout.vs.map((v: any) => (
          <button key={v.version_id} className="graph-row" onClick={() => onSel(v.version_id)} aria-pressed={v.version_id === sel}
                  style={{ position: "absolute", left: 0, top: y(v.version_id) - ROW / 2 + 2, height: ROW - 4, paddingLeft: width + 6, width: "100%" }}>
            <span className="graph-label">{v.label || "(no label)"}</span>
            {layout.heads[v.version_id] ? <Badge kind={v.version_id === curHead ? "info" : "neutral"}>{layout.heads[v.version_id]}</Badge> : null}
            {v.is_merge ? <Badge kind="ok">negotiated merge</Badge> : null}
            <span className="small muted">{when(v.created_at)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function VersionPanel({ agentId, versionId, d, busy, onRestore, onDiff }:
  { agentId: string; versionId: string; d: any; busy: string | null; onRestore: (v: string) => void; onDiff: (v: string) => void }) {
  const v = useApi(() => api(`/v1/claude-code/agents/${agentId}/versions/${versionId}`), [agentId, versionId]);
  const meta = d.versions.find((x: any) => x.version_id === versionId);
  return (
    <div className="card">
      <h2 style={{ marginBottom: 4 }}>{meta?.label || "(no label)"}</h2>
      <p className="small muted">{when(meta?.created_at)} · {versionId.slice(7, 17)} · {meta?.files} file(s)</p>
      <div className="row" style={{ margin: "8px 0" }}>
        <Btn kind="primary" onClick={() => onRestore(versionId)} busy={busy === "restore"}>Go back to this state</Btn>
        <Btn onClick={() => onDiff(versionId)} busy={busy === "diff"}>Compare with now</Btn>
      </div>
      <p className="small muted">Going back writes these files into the folder (current work is checkpointed first, on its own branch) and
        saves this conversation as a Claude Code session you can resume.</p>
      {!v.data ? <Loading /> : (
        <>
          {v.data.merge_record ? <MergeRecord r={v.data.merge_record} /> : null}
          <h3>Conversation at this point ({v.data.messages} entries)</h3>
          {v.data.conversation.length ? (
            <div className="convo">{v.data.conversation.map((l: string, i: number) => (
              <p key={i} className={`small ${l.startsWith("You:") ? "you" : ""}`}>{l}</p>))}</div>
          ) : <p className="small muted">No conversation saved with this state (files only).</p>}
          {v.data.memory.length ? <p className="small">Claude Code memory: {v.data.memory.join(", ")}</p> : null}
          <details><summary className="small">Files ({v.data.files.length})</summary>
            <p className="small mono">{v.data.files.join("  ·  ")}</p></details>
        </>
      )}
    </div>
  );
}

function MergeRecord({ r }: { r: any }) {
  const units = Object.entries(r.units || {});
  return (
    <div style={{ marginBottom: 12 }}>
      <h3>How the agents decided</h3>
      <table><thead><tr><th>Change</th><th>Result</th><th>Votes and reasons</th></tr></thead><tbody>
        {units.map(([id, u]: [string, any]) => (
          <tr key={id}>
            <td className="small"><b>{u.path}</b><div className="muted">{u.kind === "conflict" ? "both changed it" : `by ${u.kind}`}</div></td>
            <td className="small"><Badge kind={u.by === "arbiter" ? "warn" : "ok"}>{u.decision}</Badge><div className="muted">{u.by}</div></td>
            <td className="small">{(u.votes || []).map((rnd: any, i: number) => (
              <div key={i}>{["ours", "theirs"].map((side) => rnd[side] ? (
                <div key={side}><b>round {i + 1}, {side}:</b> {rnd[side].decision}. {rnd[side].reason}</div>) : null)}</div>))}
              {u.reason ? <div><b>arbiter:</b> {u.reason}</div> : null}</td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

function CommandResult({ r }: { r: any }) {
  const sid = r.data?.session?.session_id;
  const [copied, setCopied] = useState(false);
  const resume = sid ? `claude --resume ${sid}` : null;
  return (
    <div className="card">
      <h3>{r.waiting ? "Running" : r.ok ? "Done" : "Not done"}: <span className="mono">{r.line}</span></h3>
      {resume ? (
        <div style={{ marginBottom: 8 }}>
          <p className="small">To continue as that agent: in Claude Code type <code>/resume</code> and pick the "ANTIBODY: …" session,
            or run this in a terminal in the project folder:</p>
          <div className="row"><code className="mono">{resume}</code>
            <button onClick={() => navigator.clipboard?.writeText(resume).then(() => setCopied(true))}>{copied ? "Copied" : "Copy"}</button></div>
        </div>
      ) : null}
      <pre className="plain small">{r.output}</pre>
    </div>
  );
}

function Lessons({ d }: { d: any }) {
  return (
    <div className="card">
      <h2>Learned from failed commands</h2>
      <p className="small">ANTIBODY watches every failed command. When the same mistake happens twice, it works out why, checks the lesson
        against this project's history, and from then on stops that command before it runs and tells Claude what to do instead.</p>
      <p className="small">{d.failures_seen} failed command(s) seen · {d.failures_prevented} repeat(s) prevented</p>
      {d.lessons.length ? (
        <table><tbody>{d.lessons.map((l: any) => (
          <tr key={l.ref}><td className="small"><b>{l.summary}</b><div>{l.fix}</div><div className="muted mono">{l.ref} · {l.tool}</div></td></tr>
        ))}</tbody></table>
      ) : <p className="small muted">Nothing learned yet.</p>}
    </div>
  );
}
