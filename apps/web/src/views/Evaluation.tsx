import React from "react";
import { api, currentProject, q, short, when } from "../api";
import { Page } from "../App";
import { Badge, Empty, ErrorBox, Json, KV, Link, Loading, ModeBadge, Status, useApi } from "../ui";

function evStatus(s: string) {
  return s === "running" ? "running_eval" : s === "failed" ? "failed_eval" : s;
}

export default function Evaluation({ evaluationId }: { evaluationId?: string }) {
  const pid = currentProject();
  const evals = useApi(() => api(`/v1/evaluations${q({ project_id: pid })}`), [pid]);
  const suites = useApi(() => api(`/v1/suites`), []);
  const download = async () => {
    const data = await api(`/v1/export${q({ project_id: pid })}`);
    const blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `antibody_export_${pid}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  return (
    <Page title="Evaluation" sub="Fixed suites with pinned checker hashes" actions={<button onClick={download}>Export results (JSON)</button>}>
      <div className="split">
        <div>
          <div className="card">
            <h2>Suites</h2>
            {suites.data ? (
              <table>
                <thead><tr><th>Suite</th><th>Cases</th><th>Splits</th><th>Checker</th></tr></thead>
                <tbody>{suites.data.map((s: any) => (
                  <tr key={s.suite_id + s.revision}><td>{s.suite_id}@{s.revision}</td><td>{s.case_count}</td><td className="small">discovery {s.splits.discovery} · validation {s.splits.validation} · final {s.splits.final}</td>
                    <td className="small"><span className="mono">{short(s.checker_hash, 10)}</span> {s.checker_current ? <Badge kind="ok">current</Badge> : <Badge kind="bad">changed: evaluation refused</Badge>}</td></tr>
                ))}</tbody>
              </table>
            ) : <Loading />}
            <p className="small muted">Candidates are validated on the validation split only. Untouched final cases run only in <code>make eval</code>. This custom suite is not an official AgentDojo result.</p>
          </div>
          <div className="card">
            <h2>Evaluations</h2>
            <ErrorBox error={evals.error} retry={evals.reload} />
            {evals.data?.length === 0 ? <Empty>No evaluations yet. Test a candidate from an incident.</Empty> : null}
            <table>
              <thead><tr><th>Evaluation</th><th>Subject</th><th>Status</th><th>Summary</th><th>When</th></tr></thead>
              <tbody>
                {(evals.data || []).map((e: any) => (
                  <tr key={e.evaluation_id} className={`clickable${e.evaluation_id === evaluationId ? " selected" : ""}`} onClick={() => (window.location.hash = `#/evaluation/${e.evaluation_id}`)}>
                    <td className="mono">{short(e.evaluation_id)}</td>
                    <td className="small">{e.subject.kind} {e.subject.ref || ""}</td>
                    <td><Status value={evStatus(e.status)} title={e.status} /></td>
                    <td className="small">{Object.entries(e.summary).filter(([, v]) => typeof v === "string").slice(0, 3).map(([k, v]) => <div key={k}>{k.replace(/_/g, " ")}: {String(v)}</div>)}</td>
                    <td className="small muted">{when(e.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div className="sticky">{evaluationId ? <EvalDetail id={evaluationId} /> : <div className="card"><Empty>Select an evaluation to see cases, traces and cost.</Empty></div>}</div>
      </div>
    </Page>
  );
}

function EvalDetail({ id }: { id: string }) {
  const e = useApi(() => api(`/v1/evaluations/${id}`), [id]);
  if (e.error) return <div className="card"><ErrorBox error={e.error} /></div>;
  if (!e.data) return <div className="card"><Loading what="evaluation" /></div>;
  const x = e.data;
  return (
    <div className="card">
      <h2>{short(x.evaluation_id)} <Status value={evStatus(x.status)} title={x.status} /> <ModeBadge mode={x.mode} /></h2>
      <KV items={[["Subject", `${x.subject.kind} ${x.subject.ref || ""}`], ["Suite", `${x.suite_id}@${x.suite_revision}`], ["Checker", <span className="mono">{short(x.checker_hash, 12)}</span>],
                  ["Candidate", x.candidate_id ? `${short(x.candidate_id)} rev ${x.candidate_revision}` : "combined state"], ["Finished", when(x.finished_at)]]} />
      {x.summary.reasons?.length ? <div className="banner bad small" style={{ marginTop: 8 }}>Rejected: {x.summary.reasons.join("; ")}</div> : null}
      {x.summary.note ? <p className="small muted">{x.summary.note}</p> : null}
      <h3>Cases</h3>
      <table>
        <thead><tr><th>Case</th><th>Kind</th><th>Result</th><th>Baseline → with candidate</th></tr></thead>
        <tbody>
          {x.results.map((r: any) => (
            <tr key={r.case_id}>
              <td className="mono small">{r.case_id}</td>
              <td className="small">{r.kind.replace(/_/g, " ")}</td>
              <td><Status value={r.passed ? "passed" : "failed_eval"} title={r.feedback} />{r.improved_over_baseline ? <div><Badge kind="info" plain>improved</Badge></div> : null}</td>
              <td className="small">
                {r.baseline ? <>report {String(r.baseline.report_passed)} → {String(r.with_candidate.report_passed)}; quarantines {r.baseline.ingress_quarantines} → {r.with_candidate.ingress_quarantines}; unsafe proposals {r.baseline.unsafe_proposals} → {r.with_candidate.unsafe_proposals}</> :
                  <>{r.checks.map((c: any) => `${c.check}=${c.passed ? "ok" : "fail"}`).join(", ")}</>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Measured cost</h3>
      <KV items={[["Sandbox runs", String(x.cost.sandbox_runs ?? "—")], ["Steps", String(x.cost.steps ?? "—")], ["Model calls", String(x.cost.model_calls ?? "—")],
                  ["Tokens", `${x.cost.tokens ?? "—"} (${x.cost.token_count_kind || "estimated"})`], ["Elapsed", `${x.cost.elapsed_ms ?? "—"} ms`], ["Price", "Not measured in this run (deterministic planner)"]]} />
      <details><summary className="small">Raw results</summary><Json value={x.results} /></details>
    </div>
  );
}
