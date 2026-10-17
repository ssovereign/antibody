import React from "react";
import { api, currentProject, q, short, when } from "../api";
import { Page } from "../App";
import { Badge, Empty, ErrorBox, Link, Loading, ModeBadge, Status, useApi } from "../ui";

export default function Overview() {
  const pid = currentProject();
  const ov = useApi(() => api(`/v1/projects/${pid}`), [pid]);
  const runs = useApi(() => api(`/v1/runs${q({ project_id: pid })}`), [pid]);
  const inc = useApi(() => api(`/v1/incidents${q({ project_id: pid })}`), [pid]);
  if (ov.error) return <Page title="Overview"><ErrorBox error={ov.error} retry={ov.reload} /></Page>;
  if (!ov.data) return <Page title="Overview"><Loading what="project" /></Page>;
  const o = ov.data;
  const c = o.counts;
  const next = (inc.data || []).filter((i: any) => ["open", "recovering", "recovered"].includes(i.status)).slice(0, 5);
  return (
    <Page title={o.project.name} sub={`registry revision ${o.registry_revision} · policy ${o.policy.revision}`}>
      <div className="card">
        <h2>Measured from execution records</h2>
        <div className="grid2">
          {[
            ["Runs", c.runs],
            ["Denied actions", c.denied_actions],
            ["Executor calls for denied actions", c.denied_executor_invocations],
            ["Early interceptions (accepted patterns)", c.early_interceptions],
            ["Accepted lessons active", c.accepted_lessons],
            ["Saved versions", c.versions],
            ["Unknown effects awaiting reconciliation", c.unknown_effects],
            ["Local outbox entries (test destination)", c.outbox_entries],
          ].map(([l, v]) => (
            <div className="stat" key={String(l)}>
              <div className="v">{v as any}</div>
              <div className="l">{l}</div>
            </div>
          ))}
        </div>
        <p className="muted small" style={{ marginBottom: 0 }}>
          Counts come from the journal, decisions, executor ledger and outbox. No sample metrics are mixed in.
        </p>
      </div>

      <div className="split">
        <div>
          <div className="card">
            <h2>Agents</h2>
            {o.agents.length === 0 ? (
              <Empty>No agents yet. Run <code>make demo</code> or create one from Settings.</Empty>
            ) : (
              <table>
                <thead>
                  <tr><th>Agent</th><th>Adapter</th><th>Last run</th><th>Health</th><th /></tr>
                </thead>
                <tbody>
                  {o.agents.map((a: any) => (
                    <tr key={a.agent_id}>
                      <td>
                        <b>{a.name}</b>
                        <div className="muted small mono">{short(a.agent_id)}</div>
                        {a.config?.planner?.follows_embedded_instructions ? (
                          <Badge kind="warn" plain title="The simulated planner acts on embedded instructions to exercise the gateway.">
                            simulated susceptible model
                          </Badge>
                        ) : null}
                      </td>
                      <td>
                        {a.adapter}
                        {!a.capabilities.restore_cursor ? <div className="small muted">tool-only proxy: no full restore</div> : null}
                      </td>
                      <td>
                        {a.last_run ? (
                          <>
                            <Link to={`/runs/${a.last_run.run_id}`}>{short(a.last_run.run_id)}</Link> <Status value={a.last_run.status} />{" "}
                            <ModeBadge mode={a.last_run.mode} />
                          </>
                        ) : (
                          <span className="muted">none</span>
                        )}
                      </td>
                      <td>{a.last_run ? <Status value={a.last_run.health} /> : "—"}</td>
                      <td>
                        <Link to={`/history/${a.agent_id}`}>History</Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="card">
            <h2>Recent runs</h2>
            {runs.loading && !runs.data ? <Loading /> : null}
            <table>
              <thead>
                <tr><th>Run</th><th>Task</th><th>Status</th><th>Health</th><th>Mode</th><th>Updated</th></tr>
              </thead>
              <tbody>
                {(runs.data || []).slice(0, 12).map((r: any) => (
                  <tr key={r.run_id} className="clickable" onClick={() => (window.location.hash = `#/runs/${r.run_id}`)}>
                    <td className="mono">{short(r.run_id)}{r.purpose !== "task" ? <div className="small muted">{r.purpose}</div> : null}</td>
                    <td>{r.task_id}@{r.task_revision}</td>
                    <td><Status value={r.status} /> <div className="small muted">{r.status_reason}</div></td>
                    <td><Status value={r.health} /></td>
                    <td><ModeBadge mode={r.mode} /></td>
                    <td className="small muted">{when(r.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <div className="card">
            <h2>Needs attention</h2>
            {next.length === 0 ? <Empty>No open incidents.</Empty> : null}
            {next.map((i: any) => (
              <div key={i.incident_id} style={{ marginBottom: 10 }}>
                <div className="row">
                  <Status value={i.status === "recovering" ? "recovering_inc" : i.status} />
                  <b>{i.category.replace("_", " ")}</b>
                  <Status value={i.attribution_level} />
                </div>
                <div className="small muted">{i.observed?.[0]?.fact}</div>
                <Link to={`/incidents/${i.incident_id}`}>Inspect incident →</Link>
              </div>
            ))}
          </div>
          <div className="card">
            <h2>What is real here</h2>
            <ul className="small" style={{ paddingLeft: 18, margin: 0 }}>
              <li>The gateway, journal, executors, checks, learning, registry and versions run real code.</li>
              <li><ModeBadge mode="simulated" /> runs use the deterministic reference planner instead of a live model.</li>
              <li><ModeBadge mode="recorded" /> runs replay a trace and are never counted as fresh attack successes.</li>
              <li>The export tool uses a declared cp1252 encoding fixture, not a live Windows claim.</li>
              <li>The outbox and payment ledger are local test destinations with synthetic identities.</li>
            </ul>
          </div>
        </div>
      </div>
    </Page>
  );
}
