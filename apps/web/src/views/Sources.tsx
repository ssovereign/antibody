import React, { useState } from "react";
import { api, currentProject, newKey, q, short, when } from "../api";
import { Page } from "../App";
import { Badge, Btn, Empty, ErrorBox, KV, Loading, PlainText, Status, useAction, useApi } from "../ui";

function toB64(buf: ArrayBuffer): string {
  let s = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

export default function Sources({ sourceId }: { sourceId?: string }) {
  const pid = currentProject();
  const list = useApi(() => api(`/v1/sources${q({ project_id: pid })}`), [pid]);
  const [channel, setChannel] = useState("document");
  const [url, setUrl] = useState("");
  const { busy, run } = useAction();
  const upload = (f: File) => run("upload", async () => {
    const b64 = toB64(await f.arrayBuffer());
    const ch = f.name.endsWith(".ics") ? "calendar" : f.name.endsWith(".jsonl") || f.name.endsWith(".log") ? "log" : f.name.endsWith(".html") ? "web" : channel;
    const s = await api("/v1/sources", { method: "POST", body: { project_id: pid, channel: ch, filename: f.name, content_b64: b64 }, idem: newKey("src") });
    list.reload();
    window.location.hash = `#/sources/${s.source_id}`;
  }, "Imported through the shared source pipeline");
  const fetchUrl = () => run("fetch", async () => {
    const s = await api("/v1/sources", { method: "POST", body: { project_id: pid, channel: "web", url }, idem: newKey("fetch") });
    list.reload();
    window.location.hash = `#/sources/${s.source_id}`;
  }, "Fetched with SSRF, redirect and size checks");
  return (
    <Page title="Sources" sub="Outside content is data: it can supply facts, never authority">
      <div className="card">
        <h2>Import</h2>
        <div className="row">
          <select value={channel} onChange={(e) => setChannel(e.target.value)} aria-label="Channel">
            {["document", "web", "log", "calendar"].map((c) => <option key={c}>{c}</option>)}
          </select>
          <input type="file" accept=".txt,.md,.html,.htm,.jsonl,.log,.ics,.json" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} aria-label="Choose file" />
          <input placeholder="https://… (public HTTPS only)" value={url} onChange={(e) => setUrl(e.target.value)} style={{ minWidth: 260 }} aria-label="URL" />
          <Btn onClick={fetchUrl} busy={busy === "fetch"} disabledReason={!url.startsWith("https://") ? "Enter an https URL" : null}>Fetch</Btn>
        </div>
        <p className="small muted">Supported: text, Markdown, HTML, JSONL/text logs, ICS and normalized event JSON. Scripts never run; links and attachments are never opened.</p>
      </div>
      <div className="split">
        <div className="card">
          <h2>Project sources</h2>
          <ErrorBox error={list.error} retry={list.reload} />
          {list.loading && !list.data ? <Loading /> : null}
          {list.data?.length === 0 ? <Empty>No sources imported.</Empty> : null}
          <table>
            <thead><tr><th>Source</th><th>Channel</th><th>Origin</th><th>Parser</th><th>Retrieved</th></tr></thead>
            <tbody>{(list.data || []).map((s: any) => (
              <tr key={s.source_id} className={`clickable${s.source_id === sourceId ? " selected" : ""}`} onClick={() => (window.location.hash = `#/sources/${s.source_id}`)}>
                <td>{s.label}<div className="mono small muted">{short(s.source_id)}</div></td>
                <td>{s.channel}</td><td><Badge kind="neutral" plain>{s.origin}</Badge></td><td className="small">{s.parser}</td><td className="small muted">{when(s.retrieved_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        <div className="sticky">{sourceId ? <SourceDetail id={sourceId} /> : <div className="card"><Empty>Select a source.</Empty></div>}</div>
      </div>
    </Page>
  );
}

function SourceDetail({ id }: { id: string }) {
  const d = useApi(() => api(`/v1/sources/${id}`), [id]);
  if (d.error) return <div className="card"><ErrorBox error={d.error} /></div>;
  if (!d.data) return <div className="card"><Loading what="source" /></div>;
  const { source: s, chunks, exclusions, dispositions } = d.data;
  return (
    <div className="card">
      <h2>{s.label}</h2>
      <KV items={[["ID", <span className="mono">{s.source_id}</span>], ["Origin", s.origin], ["Channel / parser", `${s.channel} / ${s.parser} (${s.parser_revision})`],
                  ["Content hash", <span className="mono">{short(s.content_hash, 16)}</span>], ["Size", `${s.size_bytes} bytes`], ["Raw evidence", d.data.raw_available ? "retained (restricted)" : "not available"],
                  ["Dependencies", s.deps.length ? s.deps.join(", ") : "none"]]} />
      {s.metadata?.links?.length ? <p className="small">{s.metadata.links.length} link(s) recorded, none followed.</p> : null}
      {s.metadata?.events?.length ? <p className="small">{s.metadata.events.length} calendar event(s): {s.metadata.events.map((e: any) => `${e.uid} rev ${e.revision} ${e.status}`).join("; ")}</p> : null}
      <h3>Agent-visible view ({chunks.length} chunk{chunks.length === 1 ? "" : "s"})</h3>
      {chunks.map((c: any) => (
        <div key={c.chunk_index}>
          <div className="small muted">{c.ref} · offsets {c.start_offset}–{c.end_offset}</div>
          <PlainText text={c.text} quarantined={dispositions.filter((x: any) => x.chunk_index === c.chunk_index && x.disposition === "quarantined").flatMap((x: any) => JSON.parse(x.detail_json || "{}").spans || [])} />
        </div>
      ))}
      <h3>Excluded at parse time</h3>
      {exclusions.length === 0 ? <p className="small muted">Nothing excluded.</p> : exclusions.map((e: any) => (
        <div key={e.id} className="small" style={{ marginBottom: 6 }}>
          <Badge kind="warn" plain>{e.kind.replace(/_/g, " ")}</Badge> raw offsets {e.start_offset}–{e.end_offset}: {e.detail.reason || e.detail.error}
          {e.detail.text ? <PlainText text={e.detail.text} /> : null}
        </div>
      ))}
      <h3>Dispositions</h3>
      {dispositions.length === 0 ? <p className="small muted">No run has quarantined or flagged this source.</p> : (
        <table><tbody>{dispositions.map((x: any) => <tr key={x.id}><td><Status value={x.disposition} /></td><td className="small">{x.reason}{x.pattern_ref ? ` (${x.pattern_ref})` : ""}</td><td className="mono small">{short(x.run_id)}</td></tr>)}</tbody></table>
      )}
    </div>
  );
}
