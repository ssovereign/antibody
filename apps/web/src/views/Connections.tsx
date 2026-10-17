import React, { useState } from "react";
import { api, currentProject, q, when } from "../api";
import { Badge, Btn, ErrorBox, KV, Loading, useAction, useApi } from "../ui";

export function ConnectClaude() {
  const s = useApi(() => api(`/v1/integrations/claude`), []);
  const [key, setKey] = useState("");
  const { busy, run } = useAction();
  if (s.error) return <div className="card"><ErrorBox error={s.error} retry={s.reload} /></div>;
  if (!s.data) return <div className="card"><Loading what="Claude connection" /></div>;
  const x = s.data;
  const set = (changes: any) => run("set", () => api(`/v1/integrations/claude`, { method: "PATCH", body: changes }).then(s.reload), "Saved");
  return (
    <div className="card" id="connect-claude">
      <h2>Connect Claude {x.connected ? <Badge kind="ok">connected</Badge> : <Badge kind="warn">not connected</Badge>}</h2>
      <p className="small">Powers the Chat screen and any agent set to the Claude model. The key is stored only on this computer's server
        (in <code>var/secrets.json</code>) and is never sent back to the browser.</p>
      {x.connected ? <KV items={[["Credential", `${x.source} (${x.key_hint || "token"})`]]} /> : null}
      <div className="row" style={{ marginTop: 8 }}>
        <input id="claude-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="sk-ant-…" style={{ flex: "1 1 260px" }} aria-label="Anthropic API key" />
        <Btn kind="primary" onClick={() => run("key", () => api(`/v1/integrations/claude/key`, { method: "POST", body: { api_key: key } }).then(() => { setKey(""); s.reload(); }), "Key saved")}
             busy={busy === "key"} disabledReason={!key.trim() ? "Paste a key first" : null}>Save key</Btn>
        {x.source === "saved in ANTIBODY settings" ? <button onClick={() => run("clr", () => api(`/v1/integrations/claude/key`, { method: "DELETE" }).then(s.reload), "Key removed")}>Remove key</button> : null}
      </div>
      <p className="small muted">Get a key from console.anthropic.com → API keys. Setting <code>ANTHROPIC_API_KEY</code> before <code>make dev</code> also works.</p>
      <div className="row" style={{ marginTop: 6 }}>
        <label className="field">Model
          <select id="claude-model" value={x.settings.model} onChange={(e) => set({ model: e.target.value })}>
            {x.models.map((m: string) => <option key={m}>{m}</option>)}
          </select>
        </label>
        <label className="field">Effort
          <select id="claude-effort" value={x.settings.effort} onChange={(e) => set({ effort: e.target.value })}>
            {x.efforts.map((m: string) => <option key={m}>{m}</option>)}
          </select>
        </label>
        <label className="field">Safety auditor
          <select id="claude-auditor" value={x.settings.auditor} onChange={(e) => set({ auditor: e.target.value })}>
            <option value="rules">built-in rules</option>
            <option value="claude">Claude (separate, tool-less call)</option>
          </select>
        </label>
        <label className="check" style={{ marginTop: 18 }}>
          <input type="checkbox" checked={!!x.settings.fallbacks} onChange={(e) => set({ fallbacks: e.target.checked })} /> refusal fallbacks
        </label>
      </div>
      <div className="row">
        <Btn onClick={() => run("test", () => api(`/v1/integrations/claude/test`, { method: "POST" }).then(s.reload))} busy={busy === "test"}
             disabledReason={!x.connected ? "Save a key first" : null}>Test connection</Btn>
        {x.last_test?.at ? (x.last_test.ok
          ? <span className="small"><Badge kind="ok">works</Badge> {x.last_test.model} replied "{x.last_test.reply}" · {when(x.last_test.at)}</span>
          : <span className="small"><Badge kind="bad">failed</Badge> {x.last_test.error} · {when(x.last_test.at)}</span>) : null}
      </div>
      <p className="small muted">Runs that use Claude are labeled <b>live</b>. Refusal fallbacks let the API retry a declined request on another model automatically.</p>
    </div>
  );
}

export function ConnectClaudeCode() {
  const pid = currentProject();
  const s = useApi(() => api(`/v1/integrations/mcp${q({ project_id: pid })}`), [pid]);
  const [copied, setCopied] = useState("");
  if (!s.data) return <div className="card"><Loading what="MCP setup" /></div>;
  const copy = (label: string, t: string) => {
    navigator.clipboard?.writeText(t).then(() => setCopied(label), () => setCopied(""));
  };
  const json = JSON.stringify(s.data.mcp_json, null, 2);
  return (
    <div className="card">
      <h2>Connect Claude Code or Claude Desktop</h2>
      <p className="small">Lets an outside agent use ANTIBODY's tools over MCP. Every call is checked against the <code>{s.data.task_id}</code> assignment and
        shows up as a run of the "Claude Code (via MCP)" agent. This covers only the tools it reaches through ANTIBODY: its own built-in
        shell or file tools are not protected.</p>
      <h3>Claude Code (one command)</h3>
      <pre className="plain">{s.data.claude_code_cli}</pre>
      <button onClick={() => copy("cli", s.data.claude_code_cli)}>{copied === "cli" ? "Copied" : "Copy command"}</button>
      <h3>Claude Desktop or .mcp.json</h3>
      <pre className="plain">{json}</pre>
      <button onClick={() => copy("json", json)}>{copied === "json" ? "Copied" : "Copy JSON"}</button>
      <p className="small muted">{s.data.note}</p>
    </div>
  );
}
