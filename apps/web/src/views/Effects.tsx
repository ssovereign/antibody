import React, { useState } from "react";
import { api, currentProject, q, short, when } from "../api";
import { Page } from "../App";
import { Btn, Empty, ErrorBox, Json, Link, Loading, Status, useAction, useApi } from "../ui";

export default function Effects() {
  const pid = currentProject();
  const [state, setState] = useState("");
  const effects = useApi(() => api(`/v1/effects${q({ project_id: pid, state })}`), [pid, state]);
  const outbox = useApi(() => api(`/v1/outbox${q({ project_id: pid })}`), [pid]);
  const { busy, run } = useAction();
  const reconcile = (op: string) => run(op, () => api(`/v1/effects/${op}/reconcile`, { method: "POST", body: { project_id: pid } }).then(() => { effects.reload(); outbox.reload(); }),
    "Reconciled with the destination's record");
  return (
    <Page title="Effects & outbox" sub="The permanent effect journal survives every restore">
      <div className="card">
        <h2>Effects <select value={state} onChange={(e) => setState(e.target.value)} aria-label="Filter by state" style={{ marginLeft: "auto" }}>
          <option value="">All states</option>{["COMPLETED", "FAILED", "UNKNOWN", "DENIED", "RESERVED", "DISPATCHING"].map((s) => <option key={s}>{s}</option>)}
        </select></h2>
        <ErrorBox error={effects.error} retry={effects.reload} />
        {effects.loading && !effects.data ? <Loading /> : null}
        {effects.data?.length === 0 ? <Empty>No effects{state ? ` in state ${state}` : ""}.</Empty> : null}
        <table>
          <thead><tr><th>Operation</th><th>Tool</th><th>State</th><th>Run</th><th>External ref</th><th>Updated</th><th /></tr></thead>
          <tbody>
            {(effects.data || []).map((e: any) => (
              <tr key={e.operation_id}>
                <td className="mono">{short(e.operation_id)}<div className="small muted">attempts {e.attempts}</div></td>
                <td>{e.tool_id}<div className="small muted">{e.effect_class}</div></td>
                <td><Status value={e.state} />{e.error ? <div className="small muted">{e.error.code}</div> : null}</td>
                <td><Link to={`/runs/${e.run_id}`}>{short(e.run_id)}</Link></td>
                <td className="mono small">{e.external_ref ? short(e.external_ref, 14) : "—"}</td>
                <td className="small muted">{when(e.updated_at)}</td>
                <td>{e.state === "UNKNOWN" ? (
                  <div>
                    <div className="small">The tool may have completed. Reconcile its status before retrying.</div>
                    <Btn kind="primary" onClick={() => reconcile(e.operation_id)} busy={busy === e.operation_id}>Reconcile</Btn>
                  </div>
                ) : null}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card">
        <h2>Local outbox (test destination)</h2>
        {outbox.data?.entries?.length === 0 ? <Empty>No messages delivered.</Empty> : null}
        <table>
          <thead><tr><th>Entry</th><th>Recipient</th><th>Artifact</th><th>Operation</th><th>Sent</th></tr></thead>
          <tbody>{(outbox.data?.entries || []).map((x: any) => (
            <tr key={x.entry_id}><td className="mono small">{short(x.entry_id, 12)}</td><td>{x.recipient_id}<div className="small muted">{x.address}</div></td><td className="small">{x.artifact_path} ({x.body_bytes} bytes)</td>
              <td className="mono small">{short(x.operation_id)}</td><td className="small muted">{when(x.sent_at)}</td></tr>
          ))}</tbody>
        </table>
        <p className="small muted">Restoring an agent never unsends these. A retry of the same logical operation reuses its operation ID and is deduplicated.</p>
      </div>
    </Page>
  );
}
