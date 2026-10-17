import React, { useState } from "react";
import { api, currentProject, newKey, q, short, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Json, KV, Link, Loading, Status, Tabs, useAction, useApi } from "../ui";

export default function Knowledge() {
  const pid = currentProject();
  const lessons = useApi(() => api(`/v1/lessons${q({ project_id: pid })}`), [pid]);
  const cands = useApi(() => api(`/v1/candidates${q({ project_id: pid })}`), [pid]);
  const reg = useApi(() => api(`/v1/registry${q({ project_id: pid })}`), [pid]);
  const [tab, setTab] = useState("Lessons & patterns");
  const [sel, setSel] = useState<any>(null);
  const { busy, run } = useAction();
  const [reason, setReason] = useState("");
  const revoke = (l: any, whole: boolean) =>
    run("revoke", async () => {
      await api(`/v1/lessons/${l.lesson_id}/revoke`, { method: "POST", body: { project_id: pid, revision: whole ? null : l.revision, reason }, idem: newKey("revoke") });
      lessons.reload(); reg.reload(); setSel(null); setReason("");
    }, "Revoked: future retrieval stops everywhere, including restored snapshots");
  return (
    <Page title="Knowledge" sub={reg.data ? `registry revision ${reg.data.revision}` : undefined}>
      <Tabs tabs={["Lessons & patterns", "Candidates", "Registry"]} value={tab} onChange={setTab} />
      {tab === "Lessons & patterns" ? (
        <div className="split">
          <div className="card">
            <h2>Accepted knowledge</h2>
            <ErrorBox error={lessons.error} retry={lessons.reload} />
            {lessons.loading && !lessons.data ? <Loading /> : null}
            {lessons.data?.length === 0 ? <Empty>No accepted lessons yet. Lessons appear after a candidate passes the fixed checks and is accepted.</Empty> : null}
            <table>
              <thead><tr><th>Lesson</th><th>Kind</th><th>Status</th><th>Mode</th><th>Used</th><th>Registry</th></tr></thead>
              <tbody>
                {(lessons.data || []).map((l: any) => (
                  <tr key={l.ref} className={`clickable${sel?.ref === l.ref ? " selected" : ""}`} onClick={() => setSel(l)}>
                    <td><b>{l.ref}</b><div className="small muted">{l.body.summary}</div></td>
                    <td>{l.kind.replace("_", " ")}</td>
                    <td><Status value={l.revoked ? "revoked" : l.status} /></td>
                    <td className="small">{l.acceptance_mode === "assisted" ? "assisted validation" : "automatic"}</td>
                    <td>{l.uses}</td>
                    <td>rev {l.registry_revision}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="sticky">
            {!sel ? <div className="card"><Empty>Select a lesson to see where it applies, why it exists, its tests and who loaded it.</Empty></div> : (
              <div className="card">
                <h2>{sel.ref} <Status value={sel.revoked ? "revoked" : sel.status} /></h2>
                <p>{sel.body.summary}</p>
                {sel.kind === "procedure" ? (
                  <KV items={[["Tool", sel.body.scope?.tool], ["Applies when", JSON.stringify(sel.body.conditions)], ["Procedure", JSON.stringify(sel.body.procedure)],
                              ["Expires on", (sel.body.expires_on || []).join(", ") || "—"], ["Must not affect", (sel.body.negative_cases || []).join("; ")]]} />
                ) : (
                  <KV items={[["Tactic", sel.body.tactic], ["Predicates", JSON.stringify(sel.body.family)], ["Exceptions", JSON.stringify(sel.body.exceptions || {})],
                              ["Transformations", (sel.body.transformations || []).join(", ")], ["Publisher", sel.body.publisher]]} />
                )}
                <h3>Why it exists</h3>
                <p className="small">{sel.body.diagnosis} {sel.body.source_incident_id ? <Link to={`/incidents/${sel.body.source_incident_id}`}>Source incident →</Link> : null}</p>
                <h3>Tests</h3>
                <p className="small"><Link to={`/evaluation/${sel.evaluation_id}`}>Evaluation {short(sel.evaluation_id)} →</Link> (candidate {short(sel.candidate_id)} rev {sel.candidate_revision})</p>
                <h3>Loaded by</h3>
                {sel.loaded_by.length ? <ul className="small" style={{ paddingLeft: 18 }}>{sel.loaded_by.slice(0, 10).map((a: any, i: number) => <li key={i}>run <Link to={`/runs/${a.run_id}`}>{short(a.run_id)}</Link> acknowledged registry revision {a.revision}</li>)}</ul> : <p className="small muted">No run has loaded this revision yet.</p>}
                <h3>Revoke or revise</h3>
                <label className="field">Reason (recorded)<input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. false positive on internal notice" /></label>
                <div className="row">
                  <Btn kind="danger" busy={busy === "revoke"} onClick={() => revoke(sel, false)} disabledReason={sel.revoked ? "Already revoked" : !reason ? "Give a reason" : null}>Revoke this revision</Btn>
                  <Btn kind="danger" busy={busy === "revoke"} onClick={() => revoke(sel, true)} disabledReason={sel.revoked ? "Already revoked" : !reason ? "Give a reason" : null}>Revoke all revisions</Btn>
                </div>
                <p className="small muted">To remove a lesson from one branch only, use Version history → branch controls; that creates a new manifest without a global revocation.</p>
              </div>
            )}
          </div>
        </div>
      ) : null}
      {tab === "Candidates" ? (
        <div className="card">
          <h2>Candidates (Draft → Testing → Accepted / Rejected)</h2>
          {cands.data?.length === 0 ? <Empty>No candidates.</Empty> : null}
          <table>
            <thead><tr><th>Candidate</th><th>Kind</th><th>Lesson</th><th>Revision</th><th>Status</th><th>Incident</th><th>Evaluations</th></tr></thead>
            <tbody>
              {(cands.data || []).map((c: any) => (
                <tr key={c.candidate_id}>
                  <td className="mono">{short(c.candidate_id)}{c.body.seeded_test_condition ? <div><Badge kind="warn" plain>seeded test condition</Badge></div> : null}</td>
                  <td>{c.kind}</td>
                  <td>{c.lesson_id}<div className="small muted">{c.body.summary}</div></td>
                  <td>{c.revision}{c.parent_revision ? <span className="small muted"> (parent {c.parent_revision})</span> : null}</td>
                  <td><Status value={c.status} /></td>
                  <td>{c.incident_id ? <Link to={`/incidents/${c.incident_id}`}>{short(c.incident_id)}</Link> : <span className="muted">manual</span>}</td>
                  <td className="small">{c.evaluations.map((e: any) => <div key={e.evaluation_id}><Link to={`/evaluation/${e.evaluation_id}`}>{short(e.evaluation_id)}</Link> <Status value={e.status} /></div>)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small muted">Rejected candidates stay searchable so the supervisor does not propose the same failed repair again.</p>
        </div>
      ) : null}
      {tab === "Registry" ? (
        <div className="split">
          <div className="card">
            <h2>Revisions</h2>
            {reg.data ? (
              <table>
                <thead><tr><th>Rev</th><th>Changes</th><th>Publisher</th><th>When</th></tr></thead>
                <tbody>
                  {[...reg.data.changes].reverse().map((c: any) => (
                    <tr key={c.revision}><td>{c.revision}</td><td className="small">{c.changes.map((x: any, i: number) => <div key={i}>{x.op} {x.lesson_id ?? ""}{x.revision ? `@${x.revision}` : ""}{x.reason ? ` — ${x.reason}` : ""}</div>)}</td><td className="small">{c.publisher}</td><td className="small muted">{when(c.created_at)}</td></tr>
                  ))}
                </tbody>
              </table>
            ) : <Loading />}
          </div>
          <div className="card">
            <h2>Acknowledgements</h2>
            <p className="small muted">Each run records the immutable registry revision it loaded at start or at a safe step boundary.</p>
            <table>
              <thead><tr><th>Run</th><th>Revision</th><th>Boundary</th><th>When</th></tr></thead>
              <tbody>{(reg.data?.acknowledgements || []).slice(0, 30).map((a: any) => <tr key={a.run_id + a.revision}><td><Link to={`/runs/${a.run_id}`}>{short(a.run_id)}</Link></td><td>{a.revision}</td><td>{a.boundary}</td><td className="small muted">{when(a.acked_at)}</td></tr>)}</tbody>
            </table>
            <h3>Active revocations</h3>
            {reg.data?.revocations?.length ? <ul className="small" style={{ paddingLeft: 18 }}>{reg.data.revocations.map((r: string) => <li key={r} className="mono">{r}</li>)}</ul> : <p className="small muted">None.</p>}
          </div>
        </div>
      ) : null}
    </Page>
  );
}
