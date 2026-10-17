import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { ApiError } from "./api";

export function Badge({ kind = "neutral", children, plain, title }: { kind?: string; children: React.ReactNode; plain?: boolean; title?: string }) {
  return (
    <span className={`badge b-${kind}${plain ? " plain" : ""}`} title={title}>
      {children}
    </span>
  );
}

const STATUS_KIND: Record<string, string> = {
  Healthy: "ok", Degraded: "warn", Paused: "warn", Quarantined: "bad", Recovering: "violet", Unknown: "warn",
  running: "info", completed: "ok", paused: "warn", recovering: "violet", failed: "bad", superseded: "neutral",
  ALLOW: "ok", DENY: "bad", PAUSE: "warn",
  COMPLETED: "ok", FAILED: "bad", UNKNOWN: "warn", DENIED: "bad", RESERVED: "neutral", DISPATCHING: "info",
  Draft: "neutral", Testing: "info", Accepted: "ok", Rejected: "bad", Superseded: "neutral", Revoked: "bad",
  accepted: "ok", revoked: "bad",
  passed: "ok", failed_eval: "bad", running_eval: "info", incomplete: "warn",
  open: "warn", recovering_inc: "violet", recovered: "ok", resolved: "ok",
  observed: "neutral", hypothesized: "warn", intervention_supported: "violet", verified_outcome: "ok",
  quarantined: "bad", flagged: "warn",
};

const LABELS: Record<string, string> = { failed_eval: "failed", running_eval: "running", recovering_inc: "recovering", intervention_supported: "intervention supported" };

export function Status({ value, title }: { value?: string | null; title?: string }) {
  if (!value) return <Badge>—</Badge>;
  return (
    <Badge kind={STATUS_KIND[value] || "neutral"} title={title}>
      {LABELS[value] || value}
    </Badge>
  );
}

export function ModeBadge({ mode }: { mode?: string }) {
  const kinds: Record<string, string> = { live: "ok", recorded: "violet", simulated: "info" };
  const titles: Record<string, string> = {
    live: "A live model produced these actions.",
    recorded: "Recorded run: a replayed trace. Never counted as a fresh live attack success.",
    simulated: "Simulated model: the deterministic reference planner. Gateway, storage, executors and checks are real.",
  };
  return (
    <span className={`badge mode b-${kinds[mode || ""] || "neutral"}`} title={titles[mode || ""]}>
      {mode || "unknown"} mode
    </span>
  );
}

export function Btn({
  onClick, children, disabledReason, kind, busy, title,
}: { onClick: () => void | Promise<any>; children: React.ReactNode; disabledReason?: string | null; kind?: "primary" | "danger"; busy?: boolean; title?: string }) {
  const disabled = !!disabledReason || !!busy;
  return (
    <span className="row" style={{ gap: 6, display: "inline-flex" }}>
      <button
        className={kind}
        aria-disabled={disabled}
        title={disabledReason || title}
        onClick={() => {
          if (!disabled) onClick();
        }}
      >
        {busy ? <span className="spin" aria-label="working" /> : null} {children}
      </button>
      {disabledReason ? <span className="why">{disabledReason}</span> : null}
    </span>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading({ what }: { what?: string }) {
  return (
    <div className="muted small" role="status">
      <span className="spin" /> Loading {what || ""}…
    </div>
  );
}

export function ErrorBox({ error, retry }: { error: any; retry?: () => void }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div className="banner bad" role="alert">
      <b>{e.code || "Error"}</b> <span>{e.message || String(error)}</span>
      {retry ? <button onClick={retry}>Retry</button> : null}
    </div>
  );
}

export function KV({ items }: { items: [string, React.ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

// Hostile content is always rendered as escaped plain text. Optional spans are highlighted, never parsed.
export function PlainText({ text, quarantined = [], anchors = [] }: { text: string; quarantined?: number[][]; anchors?: string[] }) {
  if (!quarantined.length && !anchors.length) return <pre className="plain">{text}</pre>;
  const marks: { s: number; e: number; c: string }[] = [];
  for (const [s, e] of quarantined) marks.push({ s, e, c: "q" });
  for (const a of anchors) {
    if (!a) continue;
    let i = text.toLowerCase().indexOf(a.toLowerCase());
    while (i >= 0) {
      marks.push({ s: i, e: i + a.length, c: "a" });
      i = text.toLowerCase().indexOf(a.toLowerCase(), i + a.length);
    }
  }
  marks.sort((x, y) => x.s - y.s);
  const out: React.ReactNode[] = [];
  let pos = 0;
  marks.forEach((m, idx) => {
    if (m.s < pos) return;
    out.push(text.slice(pos, m.s));
    out.push(
      <mark key={idx} className={m.c} title={m.c === "q" ? "Quarantined span (excluded from model context)" : "Argument anchor"}>
        {text.slice(m.s, m.e)}
      </mark>,
    );
    pos = m.e;
  });
  out.push(text.slice(pos));
  return <pre className="plain">{out}</pre>;
}

export function Json({ value }: { value: any }) {
  return <pre className="plain">{JSON.stringify(value, null, 2)}</pre>;
}

export function Tabs({ tabs, value, onChange }: { tabs: string[]; value: string; onChange: (t: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t} role="tab" aria-selected={t === value} className={t === value ? "on" : ""} onClick={() => onChange(t)}>
          {t}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- data hook
export function useApi<T = any>(fn: () => Promise<T>, deps: any[]): { data: T | null; error: any; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fn()
      .then((d) => alive && (setData(d), setError(null)))
      .catch((e) => alive && setError(e))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload };
}

// ---------------------------------------------------------------- toasts
const ToastCtx = createContext<(msg: string, err?: boolean) => void>(() => {});
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [t, setT] = useState<{ msg: string; err?: boolean } | null>(null);
  useEffect(() => {
    if (!t) return;
    const h = setTimeout(() => setT(null), 4200);
    return () => clearTimeout(h);
  }, [t]);
  return (
    <ToastCtx.Provider value={(msg, err) => setT({ msg, err })}>
      {children}
      {t ? (
        <div className={`toast${t.err ? " err" : ""}`} role="status" aria-live="polite">
          {t.msg}
        </div>
      ) : null}
    </ToastCtx.Provider>
  );
}
export const useToast = () => useContext(ToastCtx);

export function useAction() {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (key: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(key);
    try {
      const r = await fn();
      if (ok) toast(ok);
      return r;
    } catch (e: any) {
      toast(`${e.code || "Error"}: ${e.message}`, true);
      return undefined;
    } finally {
      setBusy(null);
    }
  };
  return { busy, run };
}

export function Link({ to, children }: { to: string; children: React.ReactNode }) {
  return <a href={`#${to}`}>{children}</a>;
}
