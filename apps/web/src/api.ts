// API client. The token is an app-scoped, project-scoped token obtained from the
// loopback-only bootstrap endpoint; no secret is bundled into the client.

export class ApiError extends Error {
  status: number;
  code: string;
  detail: any;
  constructor(status: number, code: string, message: string, detail?: any) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

let token: string | null = null;
let projectId: string | null = null;
let projects: string[] = [];

function store(key: string, value?: string | null): string | null {
  try {
    if (value === undefined) return sessionStorage.getItem(key);
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    /* storage unavailable: keep in memory only */
  }
  return value ?? null;
}

export function currentProject(): string {
  return projectId || "";
}
export function knownProjects(): string[] {
  return projects;
}

export async function bootstrap(pid?: string): Promise<{ project_id: string }> {
  const wanted = pid || store("ab_project") || undefined;
  const res = await fetch("/v1/session/bootstrap", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(wanted ? { project_id: wanted } : {}),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    if (wanted && res.status === 404) return bootstrap(undefined as any);
    throw new ApiError(res.status, body?.error?.code || "BOOTSTRAP_FAILED", body?.error?.message || "Could not start a session.");
  }
  const body = await res.json();
  token = body.token;
  projectId = body.project_id;
  projects = body.projects || [];
  store("ab_project", projectId);
  return body;
}

export async function api<T = any>(path: string, opts: { method?: string; body?: any; idem?: string } = {}, retried = false): Promise<T> {
  if (!token) await bootstrap();
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.idem) headers["Idempotency-Key"] = opts.idem;
  let res: Response;
  try {
    res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  } catch (e) {
    throw new ApiError(0, "NETWORK", "The API is unreachable. Check that `make dev` is running.");
  }
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (res.status === 401 && !retried) {
    // the session token was revoked (for example by a demo reset): start a fresh session once
    token = null;
    return api<T>(path, opts, true);
  }
  if (!res.ok) {
    const err = body?.error || {};
    throw new ApiError(res.status, err.code || `HTTP_${res.status}`, err.message || res.statusText, err.detail);
  }
  return body as T;
}

export const q = (params: Record<string, string | number | undefined | null>) =>
  "?" +
  Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");

export type StreamState = "connecting" | "live" | "disconnected" | "idle";

// Server-sent events over fetch (so the bearer token stays in a header, not a URL).
// Reconnects with backoff from the last seen event id; the caller deduplicates by sequence.
export function streamEvents(
  path: string,
  onEvent: (ev: any) => void,
  onState: (s: StreamState) => void,
  lastId: () => string | null,
): () => void {
  let stopped = false;
  let ctrl: AbortController | null = null;
  let attempt = 0;
  const loop = async () => {
    while (!stopped) {
      onState("connecting");
      ctrl = new AbortController();
      try {
        if (!token) await bootstrap();
        const headers: Record<string, string> = { Authorization: `Bearer ${token}`, Accept: "text/event-stream" };
        const li = lastId();
        if (li) headers["Last-Event-ID"] = li;
        const res = await fetch(path, { headers, signal: ctrl.signal });
        if (res.status === 401) token = null;
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
        onState("live");
        attempt = 0;
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i: number;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, i);
            buf = buf.slice(i + 2);
            const data = block
              .split("\n")
              .filter((l) => l.startsWith("data: "))
              .map((l) => l.slice(6))
              .join("\n");
            if (data) {
              try {
                const parsed = JSON.parse(data);
                if (parsed && parsed.kind) onEvent(parsed);
              } catch {
                /* ignore malformed frame */
              }
            }
          }
        }
        if (!stopped) onState("disconnected");
      } catch (e) {
        if (stopped) return;
        onState("disconnected");
      }
      attempt += 1;
      await new Promise((r) => setTimeout(r, Math.min(15000, 1000 * 2 ** Math.min(attempt, 4))));
    }
  };
  loop();
  return () => {
    stopped = true;
    ctrl?.abort();
    onState("idle");
  };
}

export function short(id?: string | null, n = 10): string {
  if (!id) return "—";
  if (id.startsWith("sha256:")) return id.slice(7, 7 + n);
  const parts = id.split("_");
  return parts.length > 1 ? `${parts[0]}_${parts.slice(1).join("_").slice(0, 6)}` : id.slice(0, n);
}

export function when(ts?: string | null): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString();
}

export function newKey(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
