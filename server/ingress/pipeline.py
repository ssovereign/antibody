"""Shared source-processing path for every inbound channel.

Each source gets a stable ID, content hash, origin category, raw-content
reference (restricted store), agent-visible view, chunks with offsets and
dependency references. Derived records keep their dependencies and can never
upgrade external content to user authority.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..canonical import content_ref, dumps, loads, new_id, utcnow
from .normalize import exact_hash, norm_hash
from ..telemetry import traced
from .parsers import (PARSER_REVISION, IngestError, ParsedSource, detect_parser, parse_event_json, parse_html, parse_ics,
                      parse_log, parse_subagent, parse_text, parse_tool_result)

TRUST_ORDER = ["EXTERNAL_DATA", "AGENT_DERIVED", "VERIFIED_TOOL_METADATA", "OPERATOR_POLICY", "USER_ASSIGNMENT"]


def weakest_origin(origins: list[str]) -> str:
    """A derived record is only as trusted as its weakest dependency, and never above AGENT_DERIVED."""
    if not origins:
        return "AGENT_DERIVED"
    w = min(origins, key=TRUST_ORDER.index)
    return w if TRUST_ORDER.index(w) <= TRUST_ORDER.index("AGENT_DERIVED") else "AGENT_DERIVED"


def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    if not text:
        return [(0, 0)]
    spans, start, n = [], 0, len(text)
    while start < n:
        end = min(n, start + size)
        if end < n:
            cut = max(text.rfind("\n", start + size // 2, end), text.rfind(" ", start + size // 2, end))
            if cut > start:
                end = cut + 1
        spans.append((start, end))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return spans


class SourceService:
    def __init__(self, db, objects, config):
        self.db = db
        self.objects = objects
        self.config = config

    # ------------------------------------------------------------ core
    def _store(self, project_id: str, channel: str, parsed: ParsedSource, raw: bytes | None, origin: str,
               run_id: str | None = None, deps: list[str] | None = None, label: str | None = None,
               dedupe: bool = True) -> dict:
        view = parsed.view
        exclusions = list(parsed.exclusions)
        vb = view.encode("utf-8")
        if len(vb) > self.config.MAX_SOURCE_BYTES:
            cut = vb[: self.config.MAX_SOURCE_BYTES].decode("utf-8", "ignore")
            exclusions.append({"kind": "truncated", "start": len(cut), "end": len(view), "detail": {"reason": f"view exceeds {self.config.MAX_SOURCE_BYTES} bytes"}})
            view = cut
        chash = content_ref(raw if raw is not None else vb)
        if dedupe and run_id is None:
            existing = self.db.one("SELECT source_id FROM sources WHERE project_id=? AND content_hash=? AND channel=? AND parser_revision=? AND status!='purged'",
                                   (project_id, chash, channel, PARSER_REVISION))
            if existing:
                return self.get(existing["source_id"])
        raw_ref = None
        if raw is not None:
            if len(raw) > self.config.MAX_RAW_BYTES:
                exclusions.append({"kind": "raw_not_retained", "start": 0, "end": len(raw), "detail": {"reason": "raw exceeds raw cap"}})
            else:
                raw_ref = self.objects.put(project_id, raw, restricted=True)
        view_ref = self.objects.put(project_id, view.encode("utf-8"))
        sid = new_id("src")
        now = utcnow()
        meta = dict(parsed.metadata)
        meta["segments"] = parsed.segments[:2000]
        with self.db.tx() as c:
            c.execute("INSERT INTO sources(source_id,project_id,channel,origin,content_hash,raw_ref,view_ref,parser,parser_revision,size_bytes,retrieved_at,metadata_json,deps_json,status,run_id,label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, project_id, channel, origin, chash, raw_ref, view_ref, parsed.parser, PARSER_REVISION,
                       len(raw) if raw is not None else len(vb), now, dumps(meta), dumps(deps or []), "active", run_id, label))
            for i, (s, e) in enumerate(chunk_text(view, self.config.CHUNK_SIZE, self.config.CHUNK_OVERLAP)):
                t = view[s:e]
                flags = {}
                quotes = [seg for seg in parsed.segments if seg.get("kind") == "quotation" and seg["start"] < e and seg["end"] > s]
                if quotes:
                    flags["quotation_spans"] = [[max(q["start"], s) - s, min(q["end"], e) - s] for q in quotes]
                c.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)", (sid, i, s, e, t, exact_hash(t), norm_hash(t), dumps(flags)))
            for ex in exclusions:
                c.execute("INSERT INTO source_exclusions(source_id,kind,start_offset,end_offset,detail_json,created_at) VALUES (?,?,?,?,?,?)",
                          (sid, ex["kind"], ex.get("start"), ex.get("end"), dumps(ex.get("detail", {})), now))
        return self.get(sid)

    @traced("source.ingest")
    def ingest_bytes(self, project_id: str, channel: str, data: bytes, filename: str | None = None,
                     content_type: str | None = None, origin: str = "EXTERNAL_DATA", label: str | None = None,
                     run_id: str | None = None, extra_meta: dict | None = None) -> dict:
        if len(data) > self.config.MAX_RAW_BYTES:
            raise IngestError("SOURCE_TOO_LARGE", f"source exceeds {self.config.MAX_RAW_BYTES} bytes")
        kind = detect_parser(channel, filename, content_type)
        if kind == "html":
            parsed = parse_html(data, filename)
        elif kind == "ics":
            parsed = parse_ics(data, filename)
        elif kind == "event_json":
            parsed = parse_event_json(data, filename)
        elif kind == "log":
            parsed = parse_log(data, filename)
        else:
            parsed = parse_text(data, filename)
        if extra_meta:
            parsed.metadata.update(extra_meta)
        src = self._store(project_id, channel, parsed, data, origin, run_id=run_id, label=label or filename)
        if kind in ("ics", "event_json"):
            self._record_calendar(project_id, src, parsed, provider="ics_import" if kind == "ics" else "normalized_json")
        return src

    def _record_calendar(self, project_id: str, src: dict, parsed: ParsedSource, provider: str, account_id: str = "local",
                         calendar_id: str = "imported") -> None:
        for ev in parsed.metadata.get("events", []):
            self.db.execute("INSERT OR IGNORE INTO calendar_events VALUES (?,?,?,?,?,?,?,?)",
                            (project_id, provider, account_id, calendar_id, ev["uid"], ev.get("revision", "0"), src["source_id"], ev.get("status", "CONFIRMED")))

    def ingest_tool_result(self, project_id: str, run_id: str, tool_id: str, result: dict) -> dict:
        parsed = parse_tool_result(tool_id, result)
        return self._store(project_id, "tool_result", parsed, None, "EXTERNAL_DATA", run_id=run_id, dedupe=False,
                           label=f"{tool_id} result")

    def ingest_subagent_message(self, project_id: str, parent_run_id: str, child_run_id: str, message: str, source_refs: list[str]) -> dict:
        parsed = parse_subagent(parent_run_id, child_run_id, message, source_refs)
        dep_origins = [s["origin"] for s in (self.get(r.split(":")[0]) for r in source_refs) if s]
        origin = weakest_origin(dep_origins + ["AGENT_DERIVED"])
        return self._store(project_id, "subagent", parsed, None, origin, run_id=parent_run_id, deps=source_refs, dedupe=False,
                           label=f"subagent {child_run_id}")

    def ingest_summary(self, project_id: str, run_id: str, text: str, deps: list[str]) -> dict:
        dep_origins = []
        for r in deps:
            s = self.get(r.split(":")[0])
            if s:
                dep_origins.append(s["origin"])
        parsed = ParsedSource(view=text, parser="summary", metadata={"derived": True})
        return self._store(project_id, "summary", parsed, None, weakest_origin(dep_origins), run_id=run_id, deps=deps, dedupe=False)

    # ------------------------------------------------------------ log files with cursors
    def ingest_log_file(self, project_id: str, path: str, allowed_roots: list[str]) -> dict | None:
        p = Path(path).resolve()
        if not any(_within(p, Path(r).resolve()) for r in allowed_roots):
            raise IngestError("LOG_PATH_NOT_PERMITTED", "log path is outside the permitted log roots")
        st = p.stat()
        with open(p, "rb") as fh:
            head = fh.read(256)
        identity = f"{st.st_dev}:{st.st_ino}:{content_ref(head)[:23]}"
        cur = self.db.one("SELECT * FROM log_cursors WHERE project_id=? AND path=?", (project_id, str(p)))
        offset, line_no, rotated = 0, 0, False
        if cur:
            if cur["file_identity"].split(":")[:2] != identity.split(":")[:2] or st.st_size < cur["byte_offset"] or \
                    (cur["byte_offset"] >= 256 and cur["file_identity"] != identity):
                rotated = True
            else:
                offset, line_no = cur["byte_offset"], cur["line_no"]
        with open(p, "rb") as fh:
            fh.seek(offset)
            data = fh.read(self.config.MAX_RAW_BYTES)
        # only consume complete lines
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return None
        data = data[: last_nl + 1]
        parsed = parse_log(data, p.name, base_offset=offset, base_line=line_no)
        parsed.metadata.update({"path_identity": identity, "rotated": rotated, "cursor_start": offset})
        src = self._store(project_id, "log", parsed, data, "EXTERNAL_DATA", label=f"{p.name} @{offset}", dedupe=False)
        new_lines = data.count(b"\n")
        self.db.execute("INSERT OR REPLACE INTO log_cursors VALUES (?,?,?,?,?,?)",
                        (project_id, str(p), identity, offset + len(data), line_no + new_lines, utcnow()))
        return src

    # ------------------------------------------------------------ reads
    def get(self, source_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
        if not r:
            return None
        r["metadata"] = loads(r.pop("metadata_json"), {})
        r["deps"] = loads(r.pop("deps_json"), [])
        return r

    def chunks(self, source_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM chunks WHERE source_id=? ORDER BY chunk_index", (source_id,))
        for r in rows:
            r["flags"] = loads(r.pop("flags_json"), {})
            r["ref"] = f"{source_id}:chunk_{r['chunk_index']}"
        return rows

    def exclusions(self, source_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM source_exclusions WHERE source_id=? ORDER BY id", (source_id,))
        for r in rows:
            r["detail"] = loads(r.pop("detail_json"), {})
        return rows

    def view(self, source_id: str) -> str:
        s = self.get(source_id)
        return self.objects.get(s["project_id"], s["view_ref"]).decode("utf-8")

    def list(self, project_id: str, include_run_scoped: bool = False) -> list[dict]:
        q = "SELECT source_id FROM sources WHERE project_id=?" + ("" if include_run_scoped else " AND run_id IS NULL") + " ORDER BY retrieved_at DESC"
        return [self.get(r["source_id"]) for r in self.db.all(q, (project_id,))]

    def raw(self, source_id: str) -> bytes | None:
        s = self.get(source_id)
        if not s or not s["raw_ref"]:
            return None
        try:
            return self.objects.get(s["project_id"], s["raw_ref"], restricted=True)
        except FileNotFoundError:
            return None

    def purge_expired_raw(self, project_id: str, retention_days: int) -> int:
        """Purge restricted raw evidence older than the retention window; a redacted audit reference remains."""
        from datetime import timedelta
        from ..canonical import utcnow_dt
        cutoff = (utcnow_dt() - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        n = 0
        for r in self.db.all("SELECT source_id FROM sources WHERE project_id=? AND raw_ref IS NOT NULL AND retrieved_at < ?", (project_id, cutoff)):
            if self.purge_raw(r["source_id"]):
                n += 1
        return n

    def purge_raw(self, source_id: str) -> bool:
        s = self.get(source_id)
        if not s or not s["raw_ref"]:
            return False
        ok = self.objects.purge(s["project_id"], s["raw_ref"], restricted=True)
        meta = dict(s["metadata"], raw_purged_at=utcnow())
        self.db.execute("UPDATE sources SET metadata_json=? WHERE source_id=?", (dumps(meta), source_id))
        return ok


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def file_identity(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"
