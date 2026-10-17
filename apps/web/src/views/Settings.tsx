import React from "react";
import { api, currentProject, newKey, q } from "../api";
import { Page } from "../App";
import { Badge, Btn, ErrorBox, Json, KV, Loading, useAction, useApi } from "../ui";

export default function Settings() {
  const pid = currentProject();
  const s = useApi(() => api(`/v1/settings${q({ project_id: pid })}`), [pid]);
  const caps = useApi(() => api(`/v1/capabilities`), []);
  const ov = useApi(() => api(`/v1/projects/${pid}`), [pid]);
  const { busy, run } = useAction();
  const toggleAuto = () => run("auto", () => api(`/v1/projects/${pid}/settings`, { method: "PATCH", body: {
    auto_promote_enabled: !s.data.project_settings.auto_promote_enabled, auto_promote_classes: ["procedure:write_report"] } }).then(s.reload),
    "Owner setting updated");
  const addAgent = () => run("agent", () => api("/v1/agents", { method: "POST", body: { project_id: pid, name: `Agent ${new Date().toLocaleTimeString()}`, config: { environment: { export_adapter: "fixture_1" } } } }).then(ov.reload), "Agent created with its own main branch");
  return (
    <Page title="Settings & capabilities">
      <ErrorBox error={s.error || caps.error} />
      <div className="split">
        <div>
          <div className="card">
            <h2>Adapter capability profiles</h2>
            {caps.data ? caps.data.adapters.map((a: any) => (
              <div key={a.adapter} style={{ marginBottom: 12 }}>
                <b>{a.adapter}</b> <span className="small muted">tested by {a.tested_by.join(", ")}</span>
                <div className="row" style={{ marginTop: 4 }}>{Object.entries(a.capabilities).map(([k, v]) => <Badge key={k} kind={v ? "ok" : "bad"}>{k.replace(/_/g, " ")}: {v ? "yes" : "no"}</Badge>)}</div>
                <p className="small">{a.note}</p>
              </div>
            )) : <Loading />}
          </div>
          <div className="card">
            <h2>Integrations</h2>
            <table><tbody>{(caps.data?.integrations || []).map((i: any) => (
              <tr key={i.name}><td><b>{i.name}</b></td><td><Badge kind={i.state === "supported" || i.state === "configured" ? "ok" : i.state === "unconfigured" ? "warn" : "neutral"}>{i.state.replace("_", " ")}</Badge></td><td className="small">{i.note}</td></tr>
            ))}</tbody></table>
          </div>
          <div className="card">
            <h2>Agents</h2>
            <Btn onClick={addAgent} busy={busy === "agent"}>Create reference agent</Btn>
            <p className="small muted">Native Codex, Cursor or Letta adapters are not built; they are not offered here.</p>
          </div>
        </div>
        <div>
          {s.data ? (
            <>
              <div className="card">
                <h2>Learning acceptance</h2>
                <KV items={[["Automatic acceptance", s.data.project_settings.auto_promote_enabled ? `enabled for ${(s.data.project_settings.auto_promote_classes || []).join(", ")}` : "off (assisted validation only)"],
                            ["Registry revision", String(s.data.registry_revision)]]} />
                <div className="row" style={{ marginTop: 8 }}><Btn onClick={toggleAuto} busy={busy === "auto"}>{s.data.project_settings.auto_promote_enabled ? "Disable automatic acceptance" : "Enable for procedure:write_report"}</Btn></div>
                <p className="small muted">Automatic acceptance still requires the fixed checks, compatible scope, current parent head and no protected regression.</p>
              </div>
              <div className="card">
                <h2>Operator policy {s.data.policy.revision}</h2>
                <p className="small muted">Read-only here. The learner and run requests cannot change hard policy.</p>
                <Json value={s.data.policy} />
              </div>
              <div className="card">
                <h2>Budgets and limits</h2>
                <KV items={[["Context presets (tokens)", Object.entries(s.data.context_presets).map(([k, v]) => `${k} ${v}`).join(" · ")],
                            ["Model context limit", String(s.data.config.MODEL_CONTEXT_LIMIT)], ["Max tool steps", String(s.data.config.MAX_TOOL_STEPS)],
                            ["Repair steps", String(s.data.config.MAX_REPAIR_STEPS)], ["Repeat failure limit", String(s.data.config.REPEAT_FAILURE_LIMIT)],
                            ["No-progress limit", String(s.data.config.NO_PROGRESS_LIMIT)], ["Guard timeout", `${s.data.config.GUARD_TIMEOUT_SECONDS}s`],
                            ["Model", `${s.data.config.MODEL_PROVIDER} / ${s.data.config.MODEL_ID}`], ["Auditor", s.data.config.AUDITOR_PROVIDER]]} />
              </div>
              <div className="card">
                <h2>Tools and recipients</h2>
                {ov.data ? <table><tbody>{ov.data.tools.map((t: any) => <tr key={t.tool_id}><td className="mono small">{t.tool_id}</td><td>{t.effect_class}</td><td className="small muted">{t.executor}</td></tr>)}</tbody></table> : <Loading />}
                {ov.data ? <p className="small">Recipients: {ov.data.recipients.map((r: any) => `${r.recipient_id} (${r.address})`).join(", ")}</p> : null}
              </div>
            </>
          ) : <Loading what="settings" />}
        </div>
      </div>
    </Page>
  );
}
