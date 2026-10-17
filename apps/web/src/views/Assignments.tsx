import React, { useEffect, useState } from "react";
import { api, currentProject, newKey, q, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Json, KV, Loading, useAction, useApi } from "../ui";

const EMPTY = { task_id: "", goal: "", tools: [] as string[], recipient_ids: [] as string[], source_ids: [] as string[], output_path: "reports/report.md",
  required_phrases: "", public_https: false, allowed_hosts: "", max_tool_steps: 30, spending_limit: "0.00", payees: [] as string[], grant_refs: "" };

export default function Assignments() {
  const pid = currentProject();
  const tasks = useApi(() => api(`/v1/tasks${q({ project_id: pid })}`), [pid]);
  const ov = useApi(() => api(`/v1/projects/${pid}`), [pid]);
  const sources = useApi(() => api(`/v1/sources${q({ project_id: pid })}`), [pid]);
  const [form, setForm] = useState<any>(EMPTY);
  const [base, setBase] = useState<any>(null);
  const [draft, setDraft] = useState<any>(null);
  const { busy, run } = useAction();
  const edit = (t: any) => {
    const b = t.body;
    const f = { task_id: b.task_id, goal: b.goal, tools: b.tools, recipient_ids: b.recipient_ids, source_ids: b.resources.source_ids, output_path: b.resources.output_path || "",
      required_phrases: (b.resources.required_phrases || []).join("\n"), public_https: b.read_network.public_https, allowed_hosts: (b.read_network.allowed_hosts || []).join(", "),
      max_tool_steps: b.max_tool_steps, spending_limit: b.spending.limit, payees: b.write_network.destination_ids, grant_refs: (b.grant_refs || []).join(", ") };
    setForm(f);
    setBase({ ...f, revision: t.revision });
  };
  const body = () => ({
    task_id: form.task_id, goal: form.goal, tools: form.tools, recipient_ids: form.recipient_ids,
    outbound_data_classes: form.tools.includes("send_report") ? ["approved_report"] : [],
    read_network: { public_https: form.public_https, allowed_hosts: form.allowed_hosts.split(",").map((s: string) => s.trim()).filter(Boolean) },
    write_network: { destination_ids: form.payees }, spending: { currency: "USD", limit: form.spending_limit || "0.00" },
    grant_refs: form.grant_refs.split(",").map((s: string) => s.trim()).filter(Boolean), max_tool_steps: +form.max_tool_steps,
    resources: { source_ids: form.source_ids, output_path: form.output_path || null, required_phrases: form.required_phrases.split("\n").map((s: string) => s.trim()).filter(Boolean) },
  });
  const save = () => run("save", async () => {
    await api("/v1/tasks", { method: "POST", body: { project_id: pid, task: body(), expected_revision: base && base.task_id === form.task_id ? base.revision : undefined }, idem: newKey("task") });
    tasks.reload(); setBase(null); setForm(EMPTY);
  }, "New assignment revision saved by a trusted application actor");
  const suggest = () => run("draft", async () => setDraft(await api("/v1/tasks/draft", { method: "POST", body: { project_id: pid, goal: form.goal } })));
  const pending = base && JSON.stringify({ ...base, revision: undefined }) !== JSON.stringify({ ...form, revision: undefined });
  const toggle = (k: string, v: string) => setForm({ ...form, [k]: form[k].includes(v) ? form[k].filter((x: string) => x !== v) : [...form[k], v] });
  const tools = ov.data?.tools || [];
  const people = (ov.data?.recipients || []).filter((r: any) => r.kind !== "payee");
  const payees = (ov.data?.recipients || []).filter((r: any) => r.kind === "payee");
  return (
    <Page title="Assignments" sub="Your goal plus typed permissions. Outside content cannot change these.">
      <div className="split">
        <div className="card">
          <h2>{base ? <>Edit {form.task_id} <Badge kind={pending ? "warn" : "info"}>{pending ? "permission change pending" : `active revision ${base.revision}`}</Badge></> : <>New assignment <Badge kind="neutral">draft</Badge></>}</h2>
          <label className="field">Task ID<input value={form.task_id} onChange={(e) => setForm({ ...form, task_id: e.target.value })} disabled={!!base} /></label>
          <label className="field">Goal (natural language, kept alongside the typed constraints)<textarea value={form.goal} onChange={(e) => setForm({ ...form, goal: e.target.value })} /></label>
          <div className="row"><Btn onClick={suggest} busy={busy === "draft"} disabledReason={!form.goal ? "Write a goal first" : null}>Suggest a draft</Btn><span className="small muted">Suggestions are never authoritative.</span></div>
          {draft ? <div className="banner info small" style={{ marginTop: 8 }}>Draft suggests tools: {draft.tools.join(", ")}. {draft.note} <button onClick={() => { setForm({ ...form, tools: draft.tools }); setDraft(null); }}>Use tools</button></div> : null}
          <h3>Tools (operator registry)</h3>
          {tools.map((t: any) => <label key={t.tool_id} className="check"><input type="checkbox" checked={form.tools.includes(t.tool_id)} onChange={() => toggle("tools", t.tool_id)} /> {t.tool_id} <span className="small muted">({t.effect_class})</span></label>)}
          <h3>Recipients (server-owned records)</h3>
          {people.map((r: any) => <label key={r.recipient_id} className="check"><input type="checkbox" checked={form.recipient_ids.includes(r.recipient_id)} onChange={() => toggle("recipient_ids", r.recipient_id)} /> {r.recipient_id} <span className="small muted">{r.address}</span></label>)}
          <h3>Resources</h3>
          {(sources.data || []).map((s: any) => <label key={s.source_id} className="check"><input type="checkbox" checked={form.source_ids.includes(s.source_id)} onChange={() => toggle("source_ids", s.source_id)} /> {s.label} <span className="small muted">({s.channel})</span></label>)}
          <label className="field">Output path (inside workspace://branch/current)<input value={form.output_path} onChange={(e) => setForm({ ...form, output_path: e.target.value })} /></label>
          <label className="field">Required phrases for the independent report check (one per line)<textarea value={form.required_phrases} onChange={(e) => setForm({ ...form, required_phrases: e.target.value })} /></label>
          <h3>Network and spending</h3>
          <label className="check"><input type="checkbox" checked={form.public_https} onChange={(e) => setForm({ ...form, public_https: e.target.checked })} /> public HTTPS reads</label>
          <label className="field">Allowed hosts (optional, comma separated)<input value={form.allowed_hosts} onChange={(e) => setForm({ ...form, allowed_hosts: e.target.value })} /></label>
          {payees.map((r: any) => <label key={r.recipient_id} className="check"><input type="checkbox" checked={form.payees.includes(r.recipient_id)} onChange={() => toggle("payees", r.recipient_id)} /> payee {r.recipient_id}</label>)}
          <div className="row">
            <label className="field">Spending limit (USD)<input value={form.spending_limit} onChange={(e) => setForm({ ...form, spending_limit: e.target.value })} style={{ width: 100 }} /></label>
            <label className="field">Max tool steps<input type="number" value={form.max_tool_steps} onChange={(e) => setForm({ ...form, max_tool_steps: e.target.value })} style={{ width: 80 }} /></label>
            <label className="field">Grant refs<input value={form.grant_refs} onChange={(e) => setForm({ ...form, grant_refs: e.target.value })} style={{ width: 200 }} /></label>
          </div>
          <div className="row">
            <Btn kind="primary" onClick={save} busy={busy === "save"} disabledReason={!form.task_id ? "Task ID required" : !form.goal ? "Goal required" : !form.tools.length ? "Select at least one tool" : base && !pending ? "No changes" : null}>
              {base ? "Save new revision" : "Create assignment"}
            </Btn>
            {base ? <button onClick={() => { setBase(null); setForm(EMPTY); }}>Cancel</button> : null}
          </div>
          <p className="small muted">Changing the goal or any permission creates a new revision. Paused runs must continue under the new revision explicitly.</p>
        </div>
        <div>
          <div className="card">
            <h2>Assignments</h2>
            <ErrorBox error={tasks.error} retry={tasks.reload} />
            {tasks.loading && !tasks.data ? <Loading /> : null}
            {tasks.data?.length === 0 ? <Empty>No assignments yet.</Empty> : null}
            {(tasks.data || []).map((t: any) => (
              <div key={t.task_id} style={{ borderBottom: "1px solid var(--line)", padding: "8px 0" }}>
                <div className="row"><b>{t.task_id}</b> <Badge kind="info">revision {t.revision}</Badge> <span className="spacer" style={{ flex: 1 }} /><button onClick={() => edit(t)}>Edit</button></div>
                <div className="small">{t.goal}</div>
                <div className="small muted">tools: {t.body.tools.join(", ")} · recipients: {t.body.recipient_ids.join(", ") || "none"} · by {t.created_by} {when(t.created_at)}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Page>
  );
}
