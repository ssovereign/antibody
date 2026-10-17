import React, { useEffect, useState } from "react";
import { api, authConfig, bootstrap, currentProject, githubSignInUrl, isHosted, knownProjects, q, signIn, signOut } from "./api";
import { ErrorBox, Loading } from "./ui";
import Overview from "./views/Overview";
import Chat from "./views/Chat";
import ClaudeCode from "./views/ClaudeCode";
import Assignments from "./views/Assignments";
import Runs from "./views/Runs";
import Incidents from "./views/Incidents";
import Knowledge from "./views/Knowledge";
import History from "./views/History";
import Evaluation from "./views/Evaluation";
import Effects from "./views/Effects";
import Sources from "./views/Sources";
import Settings from "./views/Settings";

function useRoute(): string[] {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const h = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", h);
    return () => window.removeEventListener("hashchange", h);
  }, []);
  return hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
}

const NAV: { group: string; items: [string, string, string?][] }[] = [
  { group: "Work", items: [["", "Overview"], ["chat", "Chat"], ["assignments", "Assignments"], ["runs", "Runs"], ["sources", "Sources"]] },
  { group: "Protect & learn", items: [["incidents", "Incidents", "open_incidents"], ["knowledge", "Knowledge"], ["evaluation", "Evaluation"]] },
  { group: "RECEIPT", items: [["claude-code", "Claude Code history"], ["history", "Version history", "versions"], ["effects", "Effects & outbox", "unknown_effects"]] },
  { group: "System", items: [["settings", "Settings & capabilities"]] },
];

export default function App() {
  const route = useRoute();
  const [ready, setReady] = useState(false);
  const [err, setErr] = useState<any>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [theme, setTheme] = useState<string>(() => {
    try {
      return localStorage.getItem("ab_theme") || "";
    } catch {
      return "";
    }
  });

  useEffect(() => {
    bootstrap()
      .then(() => setReady(true))
      .catch(setErr);
  }, []);
  useEffect(() => {
    if (theme) document.documentElement.setAttribute("data-theme", theme);
    else document.documentElement.removeAttribute("data-theme");
    try {
      localStorage.setItem("ab_theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);
  useEffect(() => {
    if (!ready) return;
    const load = () => api(`/v1/projects/${currentProject()}`).then((o) => setCounts(o.counts)).catch(() => {});
    load();
    const h = setInterval(load, 8000);
    return () => clearInterval(h);
  }, [ready, route[0]]);

  if (err && (err.code === "SIGN_IN_REQUIRED" || err.code === "AUTH_INVALID" || err.code === "SIGN_IN_FAILED"))
    return <SignIn error={err.code === "SIGN_IN_REQUIRED" ? null : err} />;
  if (err)
    return (
      <div style={{ padding: 30 }}>
        <ErrorBox error={err} retry={() => window.location.reload()} />
        <p className="muted">
          No project to open yet? Run <code>make demo</code> (or <code>python -m server.cli demo</code>) to seed the labeled demonstration project,
          then reload.
        </p>
      </div>
    );
  if (!ready)
    return (
      <div style={{ padding: 30 }}>
        <Loading what="session" />
      </div>
    );

  const [section, ...rest] = route;
  let view: React.ReactNode;
  switch (section) {
    case "chat": view = <Chat chatId={rest[0]} />; break;
    case "claude-code": view = <ClaudeCode agentId={rest[0]} />; break;
    case "assignments": view = <Assignments />; break;
    case "runs": view = <Runs runId={rest[0]} />; break;
    case "incidents": view = <Incidents incidentId={rest[0]} />; break;
    case "knowledge": view = <Knowledge />; break;
    case "history": view = <History agentId={rest[0]} />; break;
    case "evaluation": view = <Evaluation evaluationId={rest[0]} />; break;
    case "effects": view = <Effects runId={rest[0]} />; break;
    case "sources": view = <Sources sourceId={rest[0]} />; break;
    case "settings": view = <Settings />; break;
    default: view = <Overview />;
  }

  return (
    <div className="app">
      <nav className="sidebar" aria-label="Primary">
        <div className="brand">
          <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">
            <path d="M16 3v10M16 13l-8 8M16 13l8 8M8 21l-4 4M24 21l4 4" stroke="var(--accent)" strokeWidth="3" fill="none" strokeLinecap="round" />
          </svg>
          <div>
            <b>ANTIBODY</b>
            <small>protected agent workspace</small>
          </div>
        </div>
        {NAV.map((g) => (
          <div key={g.group} className="nav">
            <div className="nav-group">{g.group}</div>
            {g.items.map(([path, label, countKey]) => (
              <a key={path} href={`#/${path}`} className={section === path || (!section && !path) ? "active" : ""}>
                <span>{label}</span>
                {countKey && counts[countKey] ? <span className="count">{counts[countKey]}</span> : null}
              </a>
            ))}
          </div>
        ))}
        <div className="foot">
          <div>
            Project <b>{currentProject()}</b>
          </div>
          {isHosted() ? (
            <button className="link-button small" onClick={() => { signOut(); window.location.reload(); }}>Sign out</button>
          ) : null}
          {knownProjects().length > 1 ? (
            <select
              aria-label="Switch project"
              value={currentProject()}
              onChange={(e) => bootstrap(e.target.value).then(() => window.location.reload())}
            >
              {knownProjects().map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          ) : null}
          <div className="row" style={{ marginTop: 8 }}>
            <label className="small">
              Theme{" "}
              <select value={theme} onChange={(e) => setTheme(e.target.value)} aria-label="Theme">
                <option value="">System</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
            </label>
          </div>
          <div style={{ marginTop: 8 }}>Synthetic data. Runs are labeled live, recorded or simulated.</div>
        </div>
      </nav>
      <div className="main">{view}</div>
    </div>
  );
}

export function Page({ title, actions, children, sub }: { title: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <>
      <header className="topbar">
        <h1>{title}</h1>
        {sub ? <span className="muted small">{sub}</span> : null}
        <span className="spacer" />
        {actions}
      </header>
      <div className="content">{children}</div>
    </>
  );
}

export { q };

function SignIn({ error }: { error: any }) {
  const [tok, setTok] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [gh, setGh] = React.useState(false);
  const failed = (window.location.hash.match(/signin-error=([^&]+)/) || [])[1];
  const [e, setE] = React.useState<any>(error || (failed ? { message: `GitHub sign-in did not complete (${failed}). Please try again.` } : null));
  React.useEffect(() => { authConfig().then((c) => setGh(c.github)); }, []);
  const go = () => {
    setBusy(true);
    signIn(tok.trim()).then(() => window.location.reload()).catch((x) => { setE(x); setBusy(false); });
  };
  return (
    <div style={{ maxWidth: 460, margin: "12vh auto", padding: "0 16px" }}>
      <div className="card">
        <h2>Sign in to ANTIBODY</h2>
        {gh ? (
          <>
            <p className="small">Your own workspace: practice agents, Chat with Claude, and your Claude Code projects once you connect your computer.</p>
            <a className="gh-button" href={githubSignInUrl()}>Sign in with GitHub</a>
            <p className="small muted" style={{ marginTop: 16 }}>Or use an access token you were given:</p>
          </>
        ) : (
          <p className="small">Paste the access token you were given. Whoever runs this server creates one with{" "}
            <code>python -m server.cli invite --project &lt;project&gt; --name &lt;you&gt;</code>.</p>
        )}
        <input type="password" autoComplete="off" value={tok} onChange={(x) => setTok(x.target.value)} placeholder="access token"
               aria-label="Access token" style={{ width: "100%", margin: "8px 0" }} onKeyDown={(x) => { if (x.key === "Enter" && tok.trim()) go(); }} />
        <button className="primary" onClick={go} disabled={!tok.trim() || busy}>{busy ? "Signing in…" : "Sign in"}</button>
        {e ? <p className="small" style={{ color: "var(--bad)" }}>{e.message}</p> : null}
      </div>
    </div>
  );
}
