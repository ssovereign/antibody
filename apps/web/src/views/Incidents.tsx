import React, { useEffect, useState } from "react";
import { api, currentProject, newKey, q, short, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Json, KV, Link, Loading, PlainText, Status, useAction, useApi } from "../ui";

export default function Incidents({ incidentId }: { incidentId?: string }) {
  const pid = currentProject();
  const list = useApi(() => api(`/v1/incidents${q({ project_id: pid })}`), [pid]);
  return (
    <Page title="Incidents" sub="Observed facts first; hypotheses are labeled">
      <div className="split">
        <div className="card">
          <h2>All incidents</h2>
          <ErrorBox error={list.error} retry={list.reload} />
          {list.loading && !list.data ? <Loading /> : null}
          {list.data?.length === 0 ? <Empty>No incidents. Blocked actions, repeated failures and unknown effects appear here.</Empty> : null}
          <table>
            <thead><tr><th>Incident</th><th>Category</th><th>Status</th><th>Attribution</th><th>Opened</th></tr></thead>
            <tbody>
              {(list.data || []).map((i: any) => (
                <tr key={i.incident_id} className={`clickable${i.incident_id === incidentId ? " selected" : ""}`} onClick={() => (window.location.hash = `#/incidents/${i.incident_id}`)}>
                  <td className="mono">{short(i.incident_id)}</td>
                  <td>{i.category.replace(/_/g, " ")}<div className="small muted">{i.observed?.[0]?.fact}</div></td>
                  <td><Status value={i.status === "recovering" ? "recovering_inc" : i.status} /></td>
                  <td><Status value={i.attribution_level} /></td>
                  <td className="small muted">{when(i.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="sticky">{incidentId ? <IncidentDetail id={incidentId} onChange={list.reload} /> : <div className="card"><Empty>Select an incident.</Empty></div>}</div>
      </div>
    </Page>
  );
}

function IncidentDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const pid = currentProject();
  const d = useApi(() => api(`/v1/incidents/${id}`), [id]);
  const { busy, run } = useAction();
  const [job, setJob] = useState<any>(null);
  useEffect(() => {
    if (!job || ["succeeded", "failed"].includes(job.status)) return;
    const h = setTimeout(() => api(`/v1/jobs/${job.job_id}`).then((j) => { setJob(j); if (["succeeded", "failed"].includes(j.status)) d.reload(); }), 800);
    return () => clearTimeout(h);
  }, [job]);
  if (d.error) return <div className="card"><ErrorBox error={d.error} retry={d.reload} /></div>;
  if (!d.data) return <div className="card"><Loading what="incident" /></div>;
  const { incident: inc, evidence, candidate: cand, recovery_run: rec } = d.data;
  const lastEval = cand?.evaluations?.find((e: any) => e.candidate_revision === cand.revision);
  const evaluate = () => run("eval", async () => {
    const r = await api(`/v1/candidates/${cand.candidate_id}/evaluate`, { method: "POST", body: { project_id: pid }, idem: newKey("eval") });
    setJob({ job_id: r.job_id, status: "queued" });
  }, "Evaluation job queued on the fixed suite");
  const promote = () => run("promote", async () => {
    await api(`/v1/candidates/${cand.candidate_id}/promote`, { method: "POST", body: { project_id: pid }, idem: newKey("promote") });
    d.reload();
    onChange();
  }, "Accepted after required checks passed (assisted validation)");
  const promoteReason = !cand ? "No candidate" : cand.status === "Accepted" ? "Already accepted" : cand.status === "Rejected" ? "Rejected by the fixed checks; revise to retry"
    : cand.status !== "Testing" && cand.status !== "Draft" ? `Candidate is ${cand.status}`
    : !lastEval ? "Run the fixed checks first" : lastEval.status !== "passed" ? `Latest evaluation ${lastEval.status}` : null;
  return (
    <div className="card">
      <h2>{inc.category.replace(/_/g, " ")} <Status value={inc.status === "recovering" ? "recovering_inc" : inc.status} /></h2>
      <KV items={[["Run", <Link to={`/runs/${inc.run_id}`}>{short(inc.run_id)}</Link>], ["Severity", inc.severity], ["Attribution", <Status value={inc.attribution_level} />], ["Opened", when(inc.created_at)]]} />
      <h3>Observed facts</h3>
      <ul className="facts">{inc.observed.map((o: any, i: number) => <li key={i}>{o.fact}{o.reason_code ? <> · <b>{o.reason_code}</b></> : null}{o.error_family ? <> · {o.error_family} ({o.tool_id}, {o.environment}, phase {o.phase})</> : null}{o.executor_invoked === false ? " · executor not invoked" : ""}</li>)}</ul>
      <h3>Hypotheses</h3>
      {inc.hypotheses.length === 0 ? <p className="muted small">None yet.</p> : (
        <ul className="hyp">{inc.hypotheses.map((h: any, i: number) => <li key={i}><Status value={h.status === "suspected" ? "hypothesized" : h.status} /> {h.text}</li>)}</ul>
      )}
      {evidence.decisions.map((x: any) => (
        <div key={x.decision_id}>
          <h3>Decision</h3>
          <p><Status value={x.outcome} /> {x.message} <span className="muted small">({x.executor_invocations} executor invocations)</span></p>
        </div>
      ))}
      {inc.category === "blocked_action" ? (
        <div className="row" style={{ margin: "6px 0" }}>
          <Btn onClick={() => run("cf", () => api(`/v1/incidents/${inc.incident_id}/counterfactual`, { method: "POST", body: { repeats: 2 } }).then(() => { d.reload(); onChange(); }),
                 "Controlled reruns finished; see hypotheses")} busy={busy === "cf"}
               disabledReason={inc.attribution_level === "intervention_supported" ? "Already intervention supported" : null}
               title="Rerun in sandboxes with only the linked spans masked; side effects stay sandboxed">Run controlled attribution</Btn>
        </div>
      ) : null}
      {evidence.chunks.length ? <h3>Linked sources</h3> : null}
      {evidence.chunks.map((c: any) => (
        <div key={c.ref}>
          <div className="small"><Link to={`/sources/${c.source_id}`}>{c.label}</Link> · {c.origin} · {c.channel} · offsets {c.offsets?.join("–")}</div>
          <PlainText text={c.text} quarantined={(c.dispositions || []).filter((x: any) => x.disposition === "quarantined").flatMap((x: any) => JSON.parse(x.detail_json || "{}").spans || [])} />
        </div>
      ))}
      {evidence.events.length ? <><h3>Failure events</h3><ul className="small" style={{ paddingLeft: 18 }}>{evidence.events.map((e: any) => <li key={e.event_id}>#{e.sequence} {e.kind}: {e.payload?.error?.family} {e.payload?.error?.message}</li>)}</ul></> : null}
      {rec ? (
        <div className="banner info small">
          Bounded repair on an isolated branch: <Link to={`/runs/${rec.run_id}`}>{short(rec.run_id)}</Link> <Status value={rec.status} /> {rec.status_reason}. Recovery is not acceptance.
        </div>
      ) : null}
      <h3>Candidate</h3>
      {!cand ? <p className="muted small">No candidate was generated for this incident.</p> : (
        <>
          <div className="row"><b>{cand.lesson_id}</b> <Badge kind="neutral" plain>{cand.kind}</Badge> <span className="small muted">revision {cand.revision}</span> <Status value={cand.status} /></div>
          <p className="small">{cand.body.diagnosis}</p>
          {cand.kind === "procedure" ? (
            <KV items={[["Applies when", JSON.stringify(cand.body.conditions)], ["Procedure", JSON.stringify(cand.body.procedure)], ["Expected", cand.body.expected_improvement], ["Must not affect", (cand.body.negative_cases || []).join("; ")]]} />
          ) : (
            <KV items={[["Tactic", cand.body.tactic], ["Family predicates", JSON.stringify(cand.body.family)], ["Exact / normalized hashes", `${cand.body.exact_hashes?.length} / ${cand.body.normalized_hashes?.length}`], ["Channels", (cand.body.channels || []).join(", ")]]} />
          )}
          <div className="row" style={{ marginTop: 10 }}>
            <Btn onClick={evaluate} busy={busy === "eval" || (job && !["succeeded", "failed"].includes(job.status))}
                 disabledReason={["Accepted", "Rejected", "Superseded", "Revoked"].includes(cand.status) ? `Candidate is ${cand.status}` : null}>Test repair on fixed suite</Btn>
            <Btn kind="primary" onClick={promote} busy={busy === "promote"} disabledReason={promoteReason}>Accept revision</Btn>
          </div>
          {job ? <p className="small">Evaluation job <span className="mono">{short(job.job_id)}</span>: <Status value={job.status === "succeeded" ? "passed" : job.status === "failed" ? "failed" : "running"} />{job.error ? ` ${job.error.message}` : ""}</p> : null}
          {cand.evaluations?.length ? (
            <>
              <h3>Evaluations</h3>
              <ul className="small" style={{ paddingLeft: 18 }}>{cand.evaluations.map((e: any) => <li key={e.evaluation_id}><Link to={`/evaluation/${e.evaluation_id}`}>{short(e.evaluation_id)}</Link> rev {e.candidate_revision}: <Status value={e.status} /></li>)}</ul>
            </>
          ) : null}
          <p className="small muted">Manual acceptance after passing checks is labeled assisted validation. The learner cannot edit its checker or accept its own candidate.</p>
        </>
      )}
    </div>
  );
}
