import React, { useEffect, useRef, useState } from "react";
import { api, currentProject, newKey, q, short, StreamState, streamEvents, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, Link, Loading, Status, useAction, useApi } from "../ui";

type Perms = { tools: string[]; recipient_ids: string[]; source_ids: string[]; web: boolean; max_tool_steps?: number };
const DEFAULT_PERMS: Perms = { tools: ["read_source", "list_files", "read_file", "write_report"], recipient_ids: [], source_ids: [], web: false };

export default function Chat({ chatId }: { chatId?: string }) {
  const pid = currentProject();
  const list = useApi(() => api(`/v1/chats${q({ project_id: pid })}`), [pid]);
  const claude = useApi(() => api(`/v1/integrations/claude`), []);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    if (!chatId && list.data && list.data.length) window.location.hash = `#/chat/${list.data[0].conversation_id}`;
  }, [chatId, list.data]);
  const connected = claude.data?.connected;
  return (
    <Page title="Chat" sub="Talk to a Claude agent. Every action it takes goes through ANTIBODY's checks."
          actions={<button className="primary" onClick={() => setCreating(true)}>New conversation</button>}>
      {claude.data && !connected ? (
        <div className="banner warn">Claude is not connected yet. <Link to="/settings">Add your Anthropic API key in Settings →</Link> Version history commands (/save, /branch …) work without it.</div>
      ) : null}
      <div className="chat-layout">
        <aside className="card chat-list">
          <h2>Conversations</h2>
          <ErrorBox error={list.error} retry={list.reload} />
          {list.data?.length === 0 ? <Empty>No conversations yet.</Empty> : null}
          {(list.data || []).map((c: any) => (
            <a key={c.conversation_id} href={`#/chat/${c.conversation_id}`} className={`chat-item${c.conversation_id === chatId ? " on" : ""}`}>
              <b>{c.title}</b>
              <span className="small muted">{(c.last_text || "No messages yet").slice(0, 70)}</span>
            </a>
          ))}
        </aside>
        <section style={{ minWidth: 0 }}>
          {creating ? <NewConversation onDone={(id) => { setCreating(false); list.reload(); window.location.hash = `#/chat/${id}`; }} onCancel={() => setCreating(false)} />
            : chatId ? <Conversation key={chatId} id={chatId} connected={!!connected} onChange={list.reload} />
              : <div className="card"><Empty>Start a conversation. You set its permissions once; every message after that works within them.</Empty></div>}
        </section>
      </div>
    </Page>
  );
}

function PermissionEditor({ value, onChange }: { value: Perms; onChange: (p: Perms) => void }) {
  const pid = currentProject();
  const ov = useApi(() => api(`/v1/projects/${pid}`), [pid]);
  const sources = useApi(() => api(`/v1/sources${q({ project_id: pid })}`), [pid]);
  const toggle = (k: "tools" | "recipient_ids" | "source_ids", v: string) =>
    onChange({ ...value, [k]: value[k].includes(v) ? value[k].filter((x) => x !== v) : [...value[k], v] });
  if (!ov.data) return <Loading what="permissions" />;
  const tools = ov.data.tools.filter((t: any) => !t.tool_id.startsWith("mcp__"));
  return (
    <div className="perm-grid">
      <div>
        <h3>What it may do</h3>
        {tools.map((t: any) => (
          <label key={t.tool_id} className="check" style={{ display: "flex" }}>
            <input type="checkbox" checked={value.tools.includes(t.tool_id)} onChange={() => toggle("tools", t.tool_id)} />
            {t.tool_id} <span className="small muted">({t.effect_class.replace("_", " ")})</span>
          </label>
        ))}
        <label className="check" style={{ display: "flex" }}>
          <input type="checkbox" checked={value.web} onChange={(e) => onChange({ ...value, web: e.target.checked, tools: e.target.checked && !value.tools.includes("fetch_url") ? [...value.tools, "fetch_url"] : value.tools })} />
          read public web pages
        </label>
      </div>
      <div>
        <h3>Who it may send to</h3>
        {ov.data.recipients.filter((r: any) => r.kind !== "payee").map((r: any) => (
          <label key={r.recipient_id} className="check" style={{ display: "flex" }}>
            <input type="checkbox" checked={value.recipient_ids.includes(r.recipient_id)} onChange={() => toggle("recipient_ids", r.recipient_id)} />
            {r.recipient_id} <span className="small muted">{r.address}</span>
          </label>
        ))}
        <p className="small muted">Sending also needs the send_report tool. Nobody else can be added from inside the chat.</p>
      </div>
      <div>
        <h3>Documents it may read</h3>
        <div className="perm-sources">
          {(sources.data || []).map((s: any) => (
            <label key={s.source_id} className="check" style={{ display: "flex" }}>
              <input type="checkbox" checked={value.source_ids.includes(s.source_id)} onChange={() => toggle("source_ids", s.source_id)} />
              {s.label} <span className="small muted">({s.channel})</span>
            </label>
          ))}
        </div>
        <p className="small muted">Add more on the <Link to="/sources">Sources</Link> screen.</p>
      </div>
    </div>
  );
}

function NewConversation({ onDone, onCancel }: { onDone: (id: string) => void; onCancel: () => void }) {
  const pid = currentProject();
  const agents = useApi(() => api(`/v1/agents${q({ project_id: pid })}`), [pid]);
  const [title, setTitle] = useState("");
  const [agent, setAgent] = useState("");
  const [perms, setPerms] = useState<Perms>(DEFAULT_PERMS);
  const { busy, run } = useAction();
  const create = () => run("create", async () => {
    const c = await api("/v1/chats", { method: "POST", body: { project_id: pid, title: title || "New conversation", agent_id: agent || undefined, permissions: perms }, idem: newKey("chat") });
    onDone(c.conversation_id);
  }, "Conversation created");
  const refAgents = (agents.data || []).filter((a: any) => a.adapter === "langgraph_reference");
  return (
    <div className="card">
      <h2>New conversation</h2>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <label className="field" style={{ flex: "1 1 240px" }}>Title<input id="chat-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Q3 reporting" /></label>
        <label className="field" style={{ flex: "1 1 240px" }}>Agent
          <select id="chat-agent" value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">Claude assistant (created if needed)</option>
            {refAgents.map((a: any) => <option key={a.agent_id} value={a.agent_id}>{a.name}</option>)}
          </select>
        </label>
      </div>
      <p className="small">This is the conversation's permission slip. Set it once; each message you send works inside it. Nothing the agent reads can change it.</p>
      <PermissionEditor value={perms} onChange={setPerms} />
      <div className="row" style={{ marginTop: 12 }}>
        <Btn kind="primary" onClick={create} busy={busy === "create"} disabledReason={!perms.tools.length ? "Allow at least one tool" : null}>Start conversation</Btn>
        <button onClick={onCancel}>Cancel</button>
      </div>
      {agent ? <p className="small muted">Messages to a non-Claude agent still run with Claude as the model; the agent provides the memory, lessons and version history.</p> : null}
    </div>
  );
}

function describeEvent(e: any): string | null {
  const p = e.payload || {};
  switch (e.kind) {
    case "model.called": return p.finish ? "Writing the reply" : `Claude wants to use ${p.tool_id}`;
    case "action.decided": return p.outcome === "ALLOW" ? (p.reason_code === "DEDUPLICATED" ? "Already done earlier (not repeated)" : `Allowed: ${p.tool_id}`) : `${p.outcome === "DENY" ? "Blocked" : "Paused"}: ${p.tool_id} (${p.reason_code})`;
    case "effect.failed": return `${p.tool_id} failed: ${p.error?.family}`;
    case "source.quarantined": return "Hid a suspicious sentence from Claude";
    case "observation.received": return `Read ${p.label}${p.quarantined_spans ? ` (${p.quarantined_spans} span hidden)` : ""}`;
    case "lesson.applied": return p.applied ? `Applied lesson ${p.ref}` : null;
    default: return null;
  }
}

function Conversation({ id, connected, onChange }: { id: string; connected: boolean; onChange: () => void }) {
  const [msgs, setMsgs] = useState<any[]>([]);
  const [conv, setConv] = useState<any>(null);
  const [err, setErr] = useState<any>(null);
  const [text, setText] = useState("");
  const [editing, setEditing] = useState(false);
  const [perms, setPerms] = useState<Perms | null>(null);
  const [activity, setActivity] = useState<{ seq: number; text: string; tone: string }[]>([]);
  const [stream, setStream] = useState<StreamState>("idle");
  const { busy, run } = useAction();
  const bottom = useRef<HTMLDivElement>(null);
  const lastSeq = useRef(0);

  const load = async () => {
    try {
      const r = await api(`/v1/chats/${id}/messages${q({ after: lastSeq.current })}`);
      if (r.messages.length) {
        lastSeq.current = r.messages[r.messages.length - 1].seq;
        setMsgs((m) => [...m, ...r.messages]);
      }
      setConv(r.conversation);
      setErr(null);
    } catch (e) {
      setErr(e);
    }
  };
  useEffect(() => { lastSeq.current = 0; setMsgs([]); load(); }, [id]);
  const activeRun = conv?.active_run?.run_id;
  useEffect(() => {
    const h = setInterval(load, activeRun ? 1200 : 5000);
    return () => clearInterval(h);
  }, [id, activeRun]);
  useEffect(() => {
    setActivity([]);
    if (!activeRun) return;
    let last = 0;
    return streamEvents(`/v1/runs/${activeRun}/events?stream=true`, (ev) => {
      if (ev.sequence <= last) return;
      last = ev.sequence;
      const t = describeEvent(ev);
      if (t) setActivity((a) => [...a.slice(-7), { seq: ev.sequence, text: t, tone: /Blocked|Hid|failed/.test(t) ? "bad" : /Paused/.test(t) ? "warn" : "info" }]);
    }, setStream, () => (last ? String(last) : null));
  }, [activeRun]);
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [msgs.length, activity.length]);

  const send = () => {
    const t = text.trim();
    if (!t) return;
    run("send", async () => {
      const r = await api(`/v1/chats/${id}/messages`, { method: "POST", body: { text: t }, idem: newKey("msg") });
      setText("");
      setConv(r.conversation);
      await load();
      onChange();
    });
  };
  const savePerms = () => run("perms", async () => {
    const c = await api(`/v1/chats/${id}/permissions`, { method: "PUT", body: perms });
    setConv(c);
    setEditing(false);
    await load();
  }, "Permissions saved as a new assignment revision");

  if (!conv && !err) return <div className="card"><Loading what="conversation" /></div>;
  const isCommand = text.trim().startsWith("/");
  const sendBlock = !text.trim() ? "Type a message" : !isCommand && !connected ? "Connect Claude in Settings (commands starting with / still work)"
    : !isCommand && activeRun ? "Wait for the current reply" : null;
  return (
    <div className="card chat-main">
      <ErrorBox error={err} retry={load} />
      {conv ? (
        <div className="chat-head">
          <div>
            <h2 style={{ marginBottom: 2 }}>{conv.title}</h2>
            <div className="row small">
              <span>{conv.agent.name}</span>
              <Badge kind="info" plain title="Version history branch this conversation works on">branch {conv.branch.name}</Badge>
              <span className="muted">model {conv.agent.model?.model_id || "—"}</span>
              <span className="muted">· assignment revision {conv.task_revision}</span>
              <Link to={`/history/${conv.agent.agent_id}`}>History →</Link>
            </div>
          </div>
          <div className="row">
            <span className="small muted">May use: {conv.permissions.tools.join(", ")}{conv.permissions.recipient_ids.length ? ` · send to ${conv.permissions.recipient_ids.join(", ")}` : " · no outside recipients"}</span>
            <button onClick={() => { setPerms(conv.permissions); setEditing(!editing); }}>{editing ? "Close" : "Edit permissions"}</button>
          </div>
        </div>
      ) : null}
      {editing && perms ? (
        <div className="perm-box">
          <PermissionEditor value={perms} onChange={setPerms} />
          <div className="row"><Btn kind="primary" onClick={savePerms} busy={busy === "perms"} disabledReason={!perms.tools.length ? "Allow at least one tool" : null}>Save permissions</Btn></div>
        </div>
      ) : null}

      <div className="chat-scroll" aria-live="polite">
        {msgs.map((m) => <Message key={m.seq} m={m} />)}
        {activeRun ? (
          <div className="msg activity">
            <div className="row small"><span className="spin" /> <b>Working</b> <Link to={`/runs/${activeRun}`}>open run →</Link>
              {stream === "disconnected" ? <Badge kind="warn">live view disconnected</Badge> : null}</div>
            <ul>{activity.map((a) => <li key={a.seq}><Badge kind={a.tone} plain>{a.text}</Badge></li>)}</ul>
          </div>
        ) : null}
        <div ref={bottom} />
      </div>

      <div className="composer">
        <textarea id="chat-input" value={text} onChange={(e) => setText(e.target.value)} placeholder={connected ? "Ask the agent to do something, or type /help for version history commands" : "Type /help for version history commands (connect Claude to chat)"}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!sendBlock) send(); } }} rows={2} aria-label="Message" />
        <Btn kind="primary" onClick={send} busy={busy === "send"} disabledReason={sendBlock}>{isCommand ? "Run command" : "Send"}</Btn>
      </div>
      <p className="small muted" style={{ margin: "6px 0 0" }}>Enter to send, Shift+Enter for a new line. Commands: /status /log /save /diff /branch /switch /restore /merge /remember /forget /lessons /effects</p>
    </div>
  );
}

function Message({ m }: { m: any }) {
  if (m.role === "command") {
    return (
      <div className="msg command">
        <div className="mono small">&gt; {m.text}</div>
        <pre className={`plain${m.meta.ok ? "" : " cmd-err"}`}>{m.meta.output}</pre>
      </div>
    );
  }
  if (m.role === "system") {
    return <div className="msg system small">{m.text}{m.run_id ? <> <Link to={`/runs/${m.run_id}`}>open run →</Link></> : null}</div>;
  }
  return (
    <div className={`msg ${m.role}`}>
      <div className="who small">{m.role === "user" ? "You" : "Agent"} · {when(m.created_at)}</div>
      <div className="body">{m.text}</div>
      {m.role === "assistant" ? (
        <div className="row small" style={{ marginTop: 6 }}>
          {m.meta.actions ? <span className="muted">{m.meta.actions} checked action(s)</span> : null}
          {m.meta.blocked ? <Badge kind="bad">{m.meta.blocked} blocked</Badge> : null}
          {m.run_id ? <><Link to={`/runs/${m.run_id}`}>run {short(m.run_id)}</Link> <Link to={`/effects/${m.run_id}`}>what did it do?</Link></> : null}
        </div>
      ) : null}
    </div>
  );
}
