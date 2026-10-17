"""Exact Claude Code agent states.

A Claude model has no hidden memory between turns: what it "knows" in a session is
exactly the conversation it is given (messages, tool calls, tool results, any
compaction summary). Claude Code stores that conversation in
~/.claude/projects/<folder>/<session>.jsonl as a tree of entries (uuid / parentUuid);
the path from the latest entry back to the root is the live context.

So a saved agent state = the folder's files + that exact conversation. Restoring it
means writing the conversation back as a new Claude Code session and resuming it:
Claude continues with the same context, token for token (what it says *next* can
still vary, exactly as it would after Claude Code's own rewind).

This module reads and writes that (undocumented, internal) format and runs an agent
"as itself" for merges: through the Claude Code CLI (`--resume <session>
--fork-session`, no tools) when the CLI is signed in, or through the Messages API
with the same conversation replayed when an API key is configured.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .canonical import utcnow

PARTS_DIR = "_claude_session/parts"
META = "_claude_session/meta.json"
MERGE_RECORD = "_claude_session/merge_record.json"
PART_LINES = 400
CONV_TYPES = {"user", "assistant", "attachment", "system"}
CHANNELS = ("web", "document", "tool_result", "calendar", "email")


class BackendUnavailable(Exception):
    pass


# ------------------------------------------------------------------ files on disk
def projects_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


def encode_dir(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def parse_lines(data: bytes) -> list[dict]:
    out = []
    for raw in data.decode("utf-8", "ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def split_parts(data: bytes) -> list[bytes]:
    """Fixed line-count parts: a growing, append-only transcript keeps identical earlier parts (deduplicated blobs)."""
    lines = data.splitlines(keepends=True)
    return [b"".join(lines[i:i + PART_LINES]) for i in range(0, len(lines), PART_LINES)] or [b""]


def leaf_uuid(entries: list[dict]) -> str | None:
    for e in reversed(entries):
        if e.get("type") in CONV_TYPES and e.get("uuid") and not e.get("isSidechain"):
            return e["uuid"]
    return None


def chain(entries: list[dict], leaf: str | None = None) -> list[dict]:
    """The live conversation: entries on the path from the leaf back to the root (or to a compaction boundary)."""
    by = {e["uuid"]: e for e in entries if e.get("uuid")}
    if not by:  # older or hand-made transcripts without a tree: file order is the conversation
        return [e for e in entries if e.get("type") in CONV_TYPES]
    cur = by.get(leaf or leaf_uuid(entries) or "")
    out, seen = [], set()
    while cur and cur["uuid"] not in seen:
        seen.add(cur["uuid"])
        out.append(cur)
        cur = by.get(cur.get("parentUuid") or "")
    return list(reversed(out))


def message_text(e: dict) -> str:
    msg = e.get("message") or {}
    c = msg.get("content")
    if isinstance(c, str):
        return c
    parts = []
    for b in c or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            parts.append(f"[used {b.get('name')}]")
        elif b.get("type") == "tool_result":
            parts.append("[tool result]")
    return " ".join(p for p in parts if p).strip()


def summarize(ch: list[dict], limit: int = 40) -> list[str]:
    lines = []
    for e in ch:
        if e.get("type") not in ("user", "assistant"):
            continue
        t = message_text(e)
        if not t or t == "[tool result]":
            continue
        lines.append(f"{'You' if e['type'] == 'user' else 'Claude'}: {t[:400]}")
    return lines[-limit:]


def write_session(ch: list[dict], cwd: str, title: str, extra: list[dict] | None = None) -> tuple[str, Path]:
    """Write a conversation as a brand-new Claude Code session (never touches an existing one)."""
    sid, data = build_session(ch, cwd, title, extra)
    folder = projects_root() / encode_dir(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_bytes(data)
    return sid, path


def build_session(ch: list[dict], cwd: str, title: str, extra: list[dict] | None = None) -> tuple[str, bytes]:
    """The bytes of a new Claude Code session file for this conversation (new session id, re-linked entries)."""
    sid = str(uuid.uuid4())
    remap: dict[str, str] = {}
    out = []
    prev = None
    for e in list(ch) + list(extra or []):
        d = copy.deepcopy(e)
        old = d.get("uuid") or str(uuid.uuid4())
        new = remap.setdefault(old, str(uuid.uuid4()))
        d["uuid"] = new
        d["parentUuid"] = prev
        d.pop("logicalParentUuid", None)
        if d.get("sourceToolAssistantUUID"):
            d["sourceToolAssistantUUID"] = remap.get(d["sourceToolAssistantUUID"], d["sourceToolAssistantUUID"])
        d["sessionId"] = sid
        d["cwd"] = cwd
        d["isSidechain"] = False
        out.append(d)
        prev = new
    out.append({"type": "custom-title", "customTitle": title[:80], "sessionId": sid})
    if prev:
        out.append({"type": "last-prompt", "lastPrompt": "", "leafUuid": prev, "sessionId": sid})
    return sid, "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in out).encode("utf-8")


def synthetic_turn(ch: list[dict], user_text: str, assistant_text: str | None) -> list[dict]:
    """A user note (and optional assistant acknowledgement) shaped like the session's own entries."""
    ut = next((e for e in reversed(ch) if e.get("type") == "user" and isinstance((e.get("message") or {}).get("content"), str)), None)
    ut = ut or next((e for e in reversed(ch) if e.get("type") == "user"), None)
    at = next((e for e in reversed(ch) if e.get("type") == "assistant"), None)
    now = utcnow()
    out = []
    u = {k: v for k, v in (ut or {}).items() if k in ("userType", "entrypoint", "cwd", "version", "gitBranch")}
    u.update({"type": "user", "uuid": str(uuid.uuid4()), "parentUuid": ch[-1].get("uuid") if ch else None, "timestamp": now,
              "isSidechain": False, "message": {"role": "user", "content": user_text}})
    out.append(u)
    if assistant_text and at:
        a = {k: v for k, v in at.items() if k in ("userType", "entrypoint", "cwd", "version", "gitBranch", "requestId")}
        msg = {k: v for k, v in (at.get("message") or {}).items() if k in ("model", "type", "role", "stop_reason", "stop_sequence", "usage")}
        msg.update({"id": f"msg_antibody_{uuid.uuid4().hex[:20]}", "role": "assistant", "type": "message",
                    "content": [{"type": "text", "text": assistant_text}], "stop_reason": "end_turn"})
        a.update({"type": "assistant", "uuid": str(uuid.uuid4()), "parentUuid": u["uuid"], "timestamp": now, "isSidechain": False,
                  "message": msg})
        out.append(a)
    return out


# ------------------------------------------------------------------ screening (ANTIBODY as admin)
def screen_text(text: str, patterns: list[dict]) -> tuple[str, list[str]]:
    from .learning.patterns import match_chunk
    if not patterns or not text:
        return text, []
    spans, refs = [], []
    for ch in CHANNELS:
        for m in match_chunk({"text": text}, patterns, ch):
            if m["disposition"] == "quarantined":
                spans.append((m["start"], m["end"], m["pattern_ref"]))
    if not spans:
        return text, []
    out, last = [], 0
    for s, e, ref in sorted(set(spans)):
        if s < last:
            continue
        out.append(text[last:s])
        out.append(f"[removed by ANTIBODY: matched accepted threat pattern {ref}]")
        refs.append(ref)
        last = e
    out.append(text[last:])
    return "".join(out), sorted(set(refs))


def screen_chain(ch: list[dict], patterns: list[dict]) -> tuple[list[dict], list[dict]]:
    """Remove quarantined instructions from tool results before a state is restored or handed to another agent."""
    removed = []
    out = []
    for e in ch:
        d = copy.deepcopy(e)
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "user" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                if isinstance(b.get("content"), str):
                    b["content"], refs = screen_text(b["content"], patterns)
                    removed += [{"entry": e.get("uuid"), "pattern": r} for r in refs]
                elif isinstance(b.get("content"), list):
                    for c in b["content"]:
                        if isinstance(c, dict) and c.get("type") == "text":
                            c["text"], refs = screen_text(c.get("text", ""), patterns)
                            removed += [{"entry": e.get("uuid"), "pattern": r} for r in refs]
            if "toolUseResult" in d:
                d["toolUseResult"] = _screen_any(d["toolUseResult"], patterns)
        out.append(d)
    return out, removed


def _screen_any(v, patterns):
    if isinstance(v, str):
        return screen_text(v, patterns)[0]
    if isinstance(v, dict):
        return {k: _screen_any(x, patterns) for k, x in v.items()}
    if isinstance(v, list):
        return [_screen_any(x, patterns) for x in v]
    return v


# ------------------------------------------------------------------ Messages API view of a session
def to_api_messages(ch: list[dict], max_chars: int = 600_000) -> tuple[list[dict], list[str]]:
    """The same conversation as Messages API turns (thinking blocks dropped; old long tool results shortened if huge)."""
    msgs: list[dict] = []
    tools: set[str] = set()
    for e in ch:
        if e.get("type") not in ("user", "assistant"):
            continue
        msg = e.get("message") or {}
        role = msg.get("role") or e["type"]
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else []
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text", "").strip():
                blocks.append({"type": "text", "text": b["text"]})
            elif t == "tool_use":
                tools.add(b.get("name", "tool"))
                blocks.append({"type": "tool_use", "id": b["id"], "name": b.get("name", "tool"), "input": b.get("input") or {}})
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"], "content": str(c or "(empty)"),
                               **({"is_error": True} if b.get("is_error") else {})})
        if not blocks:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"].extend(blocks)
        else:
            msgs.append({"role": role, "content": blocks})
    # every tool_use needs a result in the next user turn
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            continue
        ids = [b["id"] for b in m["content"] if b["type"] == "tool_use"]
        if not ids:
            continue
        nxt = msgs[i + 1] if i + 1 < len(msgs) and msgs[i + 1]["role"] == "user" else None
        if nxt is None:
            nxt = {"role": "user", "content": []}
            msgs.insert(i + 1, nxt)
        have = {b.get("tool_use_id") for b in nxt["content"] if b["type"] == "tool_result"}
        missing = [{"type": "tool_result", "tool_use_id": t, "content": "(no result recorded)"} for t in ids if t not in have]
        results = [b for b in nxt["content"] if b["type"] == "tool_result"] + missing
        nxt["content"] = results + [b for b in nxt["content"] if b["type"] != "tool_result"]
    if msgs and msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": [{"type": "text", "text": "(session start)"}]})
    total = sum(len(json.dumps(m)) for m in msgs)
    for m in msgs[:-6]:
        if total <= max_chars:
            break
        for b in m["content"]:
            if b["type"] == "tool_result" and len(b["content"]) > 2000:
                total -= len(b["content"]) - 2000
                b["content"] = b["content"][:2000] + "\n[shortened by ANTIBODY to fit]"
    return msgs, sorted(tools)


def parse_json_text(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e < s:
        raise ValueError("no JSON object in reply")
    return json.loads(text[s:e + 1])


# ------------------------------------------------------------------ running an agent "as itself"
def find_claude_bin() -> str | None:
    if os.environ.get("CLAUDE_CODE_BIN"):
        return os.environ["CLAUDE_CODE_BIN"]
    cands = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        for p in Path(appdata, "Claude", "claude-code").glob("*/*/claude.exe"):
            try:
                ver = tuple(int(x) for x in p.parent.parent.name.split("."))
            except ValueError:
                ver = (0,)
            cands.append((ver, str(p)))
    if cands:
        return sorted(cands)[-1][1]
    for p in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if p.exists():
            return str(p)
    return shutil.which("claude")


def _child_env() -> dict:
    env = dict(os.environ)
    inherited_host = any(k.startswith("CLAUDE_CODE_") for k in env) or "CLAUDECODE" in env
    for k in list(env):
        if k == "CLAUDECODE" or (k.startswith("CLAUDE_CODE_") and k not in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_BIN")):
            env.pop(k)
    if inherited_host:
        env.pop("ANTHROPIC_BASE_URL", None)  # belongs to the host app that started this server, not to a standalone CLI
    return env


class AgentRunner:
    """Asks one agent a question with its exact saved context. Backends: Claude Code CLI or the Messages API."""

    def __init__(self, core, backend: str = "auto"):
        self.core = core
        self.backend = backend
        self.used: str | None = None

    def pick(self) -> str:
        if self.backend in ("cli", "api"):
            return self.backend
        if find_claude_bin():
            return "cli"
        if self.core.claude.connected():
            return "api"
        raise BackendUnavailable("No way to run Claude: sign in to the Claude Code CLI once (run `claude` in a terminal, then /login), "
                                 "or save an API key in ANTIBODY Settings → Connect Claude.")

    # handle: {"backend", "session_id"} for cli, {"backend", "messages", "tools"} for api
    def start(self, ch: list[dict] | None, cwd: str, title: str) -> dict:
        b = self.pick()
        if b == "cli":
            sid = write_session(ch, cwd, title)[0] if ch else None
            return {"backend": "cli", "session_id": sid, "forked": False, "cwd": cwd, "chain": ch}
        msgs, tools = to_api_messages(ch or [])
        return {"backend": "api", "messages": msgs, "tools": tools, "chain": ch}

    def ask(self, handle: dict, prompt: str, schema: dict, system: str) -> dict:
        try:
            return self._ask(handle, prompt, schema, system)
        except BackendUnavailable:
            # CLI not signed in: fall back to the API with the same conversation, if a key is saved
            if self.backend == "auto" and handle["backend"] == "cli" and not handle.get("forked") and self.core.claude.connected():
                self.backend = "api"
                msgs, tools = to_api_messages(handle.get("chain") or [])
                handle.clear()
                handle.update({"backend": "api", "messages": msgs, "tools": tools})
                return self._ask(handle, prompt, schema, system)
            raise

    def _ask(self, handle: dict, prompt: str, schema: dict, system: str) -> dict:
        self.used = handle["backend"]
        if handle["backend"] == "cli":
            exe = find_claude_bin()
            if not exe:
                raise BackendUnavailable("Claude Code CLI not found")
            args = [exe, "-p", prompt, "--tools", "", "--output-format", "json", "--json-schema", json.dumps(schema),
                    "--append-system-prompt", system]
            if handle.get("session_id"):
                args += ["--resume", handle["session_id"]]
                if not handle["forked"]:
                    args.append("--fork-session")
            else:
                args.append("--no-session-persistence")
            p = subprocess.run(args, cwd=handle.get("cwd") or None, capture_output=True, timeout=900, env=_child_env(),
                               stdin=subprocess.DEVNULL)
            try:
                res = json.loads(p.stdout.decode("utf-8", "ignore").strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise RuntimeError(f"Claude Code CLI failed: {(p.stderr or p.stdout).decode('utf-8', 'ignore')[:300]}")
            if res.get("is_error"):
                msg = str(res.get("result", ""))
                if "log" in msg.lower() and "in" in msg.lower():
                    raise BackendUnavailable(f"Claude Code CLI: {msg} (run `claude` in a terminal and use /login once)")
                raise RuntimeError(f"Claude Code CLI: {msg[:300]}")
            if handle.get("session_id"):
                handle["session_id"] = res.get("session_id") or handle["session_id"]
                handle["forked"] = True
            out = res.get("structured_output")
            return out if isinstance(out, dict) else parse_json_text(res.get("result", ""))
        # Messages API: same conversation, then the question
        msgs = handle["messages"]
        q = {"type": "text", "text": prompt + "\n\nReply with only a JSON object matching this schema:\n" + json.dumps(schema)}
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"].append(q)
        else:
            msgs.append({"role": "user", "content": [q]})
        kw = {}
        if handle.get("tools"):
            kw["tools"] = [{"name": t, "description": "Tool from the earlier Claude Code session. Not available now; do not call it.",
                            "input_schema": {"type": "object"}} for t in handle["tools"]]
        resp = self.core.claude.create(system=system, messages=msgs, max_tokens=8000, **kw)
        content = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in resp.content]
        msgs.append({"role": "assistant", "content": [b for b in content if b.get("type") in ("text", "tool_use")] or
                     [{"type": "text", "text": "(no reply)"}]})
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        return parse_json_text(text)
