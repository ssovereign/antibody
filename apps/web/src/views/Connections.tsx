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
      <WorkspaceField value={x.settings.workspace_id || ""} onSave={(v: string) => set({ workspace_id: v })} />
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

function WorkspaceField({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value);
  return (
    <div className="row" style={{ marginTop: 4 }}>
      <label className="field" style={{ flex: "1 1 260px" }}>Workspace ID (only for organization-level keys)
        <input id="claude-workspace" value={v} onChange={(e) => setV(e.target.value)} placeholder="wrkspc_…" />
      </label>
      <button style={{ marginTop: 18 }} onClick={() => onSave(v.trim())} disabled={v.trim() === value}>Save workspace</button>
    </div>
  );
}

export function GuardClaudeCode() {
  const pid = currentProject();
  const s = useApi(() => api(`/v1/integrations/claude-code${q({ project_id: pid })}`), [pid]);
  const [dir, setDir] = useState("");
  const [result, setResult] = useState<any>(null);
  const { busy, run } = useAction();
  if (s.error) return <div className="card"><ErrorBox error={s.error} retry={s.reload} /></div>;
  if (!s.data) return <div className="card"><Loading what="Claude Code guard" /></div>;
  const install = (d: string) => run("inst", () => api(`/v1/integrations/claude-code/install`, { method: "POST", body: { project_id: pid, project_dir: d } })
    .then((r: any) => { setResult(r); s.reload(); }), "Guard installed");
  const uninstall = (d: string) => run("un", () => api(`/v1/integrations/claude-code/uninstall`, { method: "POST", body: { project_id: pid, project_dir: d } })
    .then(() => { setResult(null); s.reload(); }), "Guard removed");
  return (
    <div className="card" id="guard-claude-code">
      <h2>Guard Claude Code in a project folder</h2>
      <p className="small">Installs hooks so every tool call Claude Code makes in that folder (shell commands, file edits, reads, web fetches,
        MCP tools) is checked by ANTIBODY first. Allowed calls continue to Claude Code's normal permission prompt; blocked calls come back
        to Claude with the reason; uncertain ones ask you. Typing <code>ab: save</code>, <code>ab: log</code>, <code>ab: diff</code> or{" "}
        <code>ab: restore HEAD~1</code> in Claude Code manages the folder's version history without reaching Claude.</p>
      <div className="row" style={{ marginTop: 8 }}>
        <input id="cc-dir" value={dir} onChange={(e) => setDir(e.target.value)} placeholder="C:\Users\you\Documents\my-project" style={{ flex: "1 1 320px" }}
               aria-label="Project folder" />
        <Btn kind="primary" onClick={() => install(dir.trim())} busy={busy === "inst"} disabledReason={!dir.trim() ? "Enter a folder" : null}>Install guard</Btn>
      </div>
      <p className="small muted">Writes <code>.claude/settings.local.json</code> in that folder (your existing settings are kept). Not allowed for the
        ANTIBODY folder itself. ANTIBODY must be running (<code>make dev</code>); if it is not, changing calls ask you first.</p>
      {result ? <p className="small"><Badge kind="ok">installed</Badge> {result.agent.name}. {result.next}</p> : null}
      <label className="check" style={{ margin: "8px 0" }}>
        <input type="checkbox" checked={!!s.data.auto_accept}
               onChange={(e) => run("aa", () => api(`/v1/claude-code/settings`, { method: "PUT", body: { project_id: pid, auto_accept: e.target.checked } }).then(s.reload), "Saved")} />
        Learn from repeated failed commands automatically (otherwise lessons wait for you in Knowledge)
      </label>
      <p className="small muted">History, restore and merge for these folders: <a href="#/claude-code">Claude Code history</a>.</p>
      {s.data.projects.length ? (
        <table><thead><tr><th>Folder</th><th>Hooks</th><th>Calls checked</th><th>Last session</th><th></th></tr></thead><tbody>
          {s.data.projects.map((x: any) => (
            <tr key={x.agent_id}>
              <td><b>{x.name}</b><div className="mono small muted">{x.project_dir}</div></td>
              <td>{x.installed ? <Badge kind="ok">installed</Badge> : <Badge kind="warn">not installed</Badge>}</td>
              <td className="small">{x.calls} ({x.denied} blocked, {x.asked} asked you)</td>
              <td className="small">{x.last_session ? <a href={`#/runs/${x.last_session.run_id}`}>{when(x.last_session.last_seen)}</a> : "none yet"}</td>
              <td>{x.installed
                ? <button onClick={() => uninstall(x.project_dir)}>Remove</button>
                : <button onClick={() => install(x.project_dir)}>Reinstall</button>}</td>
            </tr>
          ))}
        </tbody></table>
      ) : null}
    </div>
  );
}

export function ConnectComputer() {
  const st = useApi(() => api(`/v1/connector/status`), []);
  const [code, setCode] = useState<any>(null);
  const [copied, setCopied] = useState(false);
  const { busy, run } = useAction();
  const make = () => run("code", () => api(`/v1/connector/code`, { method: "POST", body: { name: "my computer" } }).then(setCode));
  const conns = st.data?.connectors || [];
  const online = (c: any) => Date.now() - new Date(c.last_seen).getTime() < 60_000;
  return (
    <div className="card" id="connect-computer">
      <h2>Connect your computer {conns.some(online) ? <Badge kind="ok">connected</Badge> : <Badge kind="warn">not connected</Badge>}</h2>
      <p className="small">Claude Code runs on your computer, so a small connector links it to this workspace. With it, everything Claude
        Code does there (checked actions, checkpoints, lessons) shows up here, and going back, switching branches and merging from this
        website change the files on your computer.</p>
      <ol className="small">
        <li>Download ANTIBODY: <a href="https://github.com/ssovereign/antibody/archive/refs/heads/main.zip">ZIP</a> (unzip it), or clone{" "}
          <code>github.com/ssovereign/antibody</code>.</li>
        <li>Double-click <b>start-antibody</b> (<code>.cmd</code> on Windows, <code>.command</code> on Mac, <code>.sh</code> on Linux).</li>
        <li>When it asks, paste this connect code. It creates a guarded practice folder and stays connected while the window is open.</li>
      </ol>
      <div className="row">
        <Btn kind="primary" onClick={make} busy={busy === "code"}>{code ? "New connect code" : "Create connect code"}</Btn>
      </div>
      {code ? (
        <div style={{ marginTop: 8 }}>
          <div className="code-box small">{code.code}</div>
          <div className="row" style={{ marginTop: 6 }}>
            <button onClick={() => navigator.clipboard?.writeText(code.code).then(() => setCopied(true))}>{copied ? "Copied" : "Copy code"}</button>
            <span className="small muted">Treat it like a password: it lets that computer act in your workspace.</span>
          </div>
        </div>
      ) : null}
      {conns.length ? (
        <table style={{ marginTop: 10 }}><tbody>{conns.map((c: any) => (
          <tr key={c.name}><td><b>{c.name}</b> <span className="small muted">{c.os}</span></td>
            <td>{online(c) ? <Badge kind="ok">online</Badge> : <Badge kind="neutral">offline</Badge>}</td>
            <td className="small muted">last seen {when(c.last_seen)}</td></tr>
        ))}</tbody></table>
      ) : null}
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
      <p className="small">Optional: lets an outside agent use ANTIBODY's own protected tools over MCP. Every call is checked against the <code>{s.data.task_id}</code> assignment and
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
