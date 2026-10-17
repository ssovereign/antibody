"""Model providers for the reference agent. Each returns a ModelAction
(tool selection + arguments only) or a finish signal.

* DeterministicPlanner - a scripted reference planner (mode: simulated). It
  reads the compiled packet like a model would. Its optional
  `follows_embedded_instructions` flag emulates a susceptible model that acts
  on instructions found in observations; the flag is shown in the UI and
  recorded in each run's start configuration. Gateway, storage, executors and
  checks are real.
* RecordedPlanner - replays a recorded action trace (mode: recorded). A
  replayed proposal is never counted as a fresh live attack success.
* AnthropicPlanner - a live Claude model via tool use (mode: live) when
  ANTHROPIC_API_KEY is configured.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SEND_INSTR = re.compile(r"\b(send|forward|email|e-mail|cc|bcc|share|deliver|copy|mail|route)\b[^.\n]{0,120}?\b(to|with)\b[^.\n]{0,60}?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.I)
DELETE_INSTR = re.compile(r"\b(delete|remove|wipe|erase|purge)\b[^.\n]{0,40}?\b((?:[\w-]+/)*[\w .-]+\.(?:md|txt|csv|json)|reports/?|the reports? folder)", re.I)
FETCH_INSTR = re.compile(r"\b(visit|fetch|open|load|retrieve|go to)\b[^.\n]{0,40}?(https://\S+)", re.I)
PAY_INSTR = re.compile(r"\b(pay|wire|transfer)\b[^.\n]{0,40}?\$?\s?(\d+(?:\.\d{1,2})?)", re.I)


@dataclass
class PlannerOutput:
    finish: bool = False
    tool_id: str | None = None
    arguments: dict = field(default_factory=dict)
    rationale: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    count_kind: str = "estimated"
    raw: dict = field(default_factory=dict)


def compose_report(packet: dict) -> str:
    a = packet["mandatory"]["assignment"]
    lines = [f"# {a['goal']}", "", f"Task {a['task_id']} revision {a['revision']}.", ""]
    for o in packet["observations"]:
        if o["channel"] in ("tool_result",):
            continue
        lines.append(f"## {o['label'] or o['ref']}")
        body = o["text"].strip()
        lines.append(body[:3000])
        lines.append("")
    lines.append("Sources: " + ", ".join(sorted({o["ref"].split(":")[0] for o in packet["observations"] if o["channel"] != "tool_result"})))
    return "\n".join(lines).strip() + "\n"


class DeterministicPlanner:
    provider = "deterministic"
    mode = "simulated"

    def __init__(self, model_id: str = "reference-planner-1", follows_embedded_instructions: bool = False):
        self.model_id = model_id
        self.follows = follows_embedded_instructions

    def next_action(self, packet: dict, progress: dict) -> PlannerOutput:
        a = packet["mandatory"]["assignment"]
        tools = set(a["tools"])
        res = a.get("resources", {})
        read = set(progress.get("read", []))
        for sid in res.get("source_ids", []):
            if sid not in read and "read_source" in tools:
                return PlannerOutput(tool_id="read_source", arguments={"source_id": sid}, rationale="gather assigned source")
        fetched = set(progress.get("fetched", []))
        for url in res.get("urls", []):
            if url not in fetched and "fetch_url" in tools:
                return PlannerOutput(tool_id="fetch_url", arguments={"url": url}, rationale="gather assigned page")
        attempted = set(progress.get("attempted", []))
        # A susceptible model may act on embedded instructions it reads.
        if self.follows:
            for o in packet["observations"]:
                for m in FETCH_INSTR.finditer(o["text"]):
                    key = f"fetch:{m.group(2)}"
                    if key not in attempted:
                        return PlannerOutput(tool_id="fetch_url", arguments={"url": m.group(2).rstrip(').,')}, rationale=f"followed instruction in {o['ref']}")
                for m in DELETE_INSTR.finditer(o["text"]):
                    path = m.group(2).strip().rstrip("/")
                    path = "reports" if "folder" in path else path
                    key = f"delete:{path}"
                    if key not in attempted:
                        return PlannerOutput(tool_id="delete_file", arguments={"path": path}, rationale=f"followed instruction in {o['ref']}")
        out = res.get("output_path")
        report = progress.get("report", {})
        if out and "write_report" in tools and not report.get("checked_passed"):
            if report.get("failures", 0) >= 3:
                return PlannerOutput(finish=True, rationale="report could not be written")
            args = {"path": out, "content": compose_report(packet)}
            pref = next((m["value"] for m in packet.get("memory", []) if m["key"] == "preferred_report_encoding"), None)
            if pref:
                args["encoding"] = pref
            return PlannerOutput(tool_id="write_report", arguments=args,
                                 rationale="write the report from gathered sources" + (f" (memory: encoding {pref})" if pref else ""))
        sent = set(progress.get("sent", [])) | set(progress.get("journal_sent", []))
        if out and report.get("checked_passed") and "send_report" in tools:
            for rid in a.get("recipient_ids", []):
                if rid not in sent:
                    return PlannerOutput(tool_id="send_report", arguments={"recipient_id": rid, "artifact_path": out,
                                                                           "subject": a["goal"][:120]}, rationale="deliver to assigned recipient")
            if self.follows:
                for o in packet["observations"]:
                    for m in SEND_INSTR.finditer(o["text"]):
                        addr = m.group(3)
                        key = f"send:{addr}"
                        if key not in attempted:
                            return PlannerOutput(tool_id="send_report", arguments={"recipient_id": addr, "artifact_path": out,
                                                                                   "subject": a["goal"][:120]},
                                                 rationale=f"followed instruction in {o['ref']}")
        if self.follows and "make_payment" in tools:
            for o in packet["observations"]:
                for m in PAY_INSTR.finditer(o["text"]):
                    key = f"pay:{m.group(2)}"
                    if key not in attempted:
                        return PlannerOutput(tool_id="make_payment", arguments={"payee_id": "payee_cloud_vendor", "amount": m.group(2),
                                                                                "currency": "USD"}, rationale=f"followed instruction in {o['ref']}")
        return PlannerOutput(finish=True, rationale="assignment complete")


class RecordedPlanner:
    provider = "recorded"
    mode = "recorded"

    def __init__(self, trace: list[dict], model_id: str = "recorded-trace"):
        self.trace = trace
        self.model_id = model_id

    def next_action(self, packet: dict, progress: dict) -> PlannerOutput:
        i = progress.get("trace_index", 0)
        if i >= len(self.trace):
            return PlannerOutput(finish=True, rationale="recorded trace exhausted")
        step = self.trace[i]
        progress["trace_index"] = i + 1
        if step.get("finish"):
            return PlannerOutput(finish=True, rationale="recorded finish")
        return PlannerOutput(tool_id=step["tool_id"], arguments=step["arguments"], rationale="recorded proposal (replay)")


class AnthropicPlanner:
    provider = "anthropic"
    mode = "live"

    def __init__(self, model_id: str, compiler):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model_id = model_id
        self.compiler = compiler

    def next_action(self, packet: dict, progress: dict) -> PlannerOutput:
        tools = [{"name": t["tool_id"], "description": f"Gateway-mediated tool {t['tool_id']}", "input_schema": t["schema"]}
                 for t in packet["mandatory"]["tool_schemas"]]
        tools.append({"name": "finish", "description": "Call when the assignment is complete or cannot continue.",
                      "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": []}})
        text = self.compiler.render_text(packet)
        t0 = time.time()
        msg = self.client.messages.create(model=self.model_id, max_tokens=4000, system=packet["mandatory"]["system"],
                                          tools=tools, tool_choice={"type": "any"},
                                          messages=[{"role": "user", "content": text + "\n\nPROGRESS DETAIL: " + json.dumps(progress, default=str)}])
        usage = getattr(msg, "usage", None)
        for block in msg.content:
            if getattr(block, "type", "") == "tool_use":
                if block.name == "finish":
                    return PlannerOutput(finish=True, rationale=str(block.input.get("reason", "")),
                                         input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
                                         count_kind="provider")
                return PlannerOutput(tool_id=block.name, arguments=dict(block.input), rationale="model tool use",
                                     input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
                                     count_kind="provider", raw={"latency_ms": int((time.time() - t0) * 1000)})
        return PlannerOutput(finish=True, rationale="no tool call", count_kind="provider")


def build_planner(cfg: dict, compiler) -> object:
    provider = cfg.get("provider", "deterministic")
    if provider == "anthropic":
        return AnthropicPlanner(cfg.get("model_id", "claude-sonnet-5-5"), compiler)
    if provider == "recorded":
        return RecordedPlanner(cfg.get("trace", []), cfg.get("model_id", "recorded-trace"))
    return DeterministicPlanner(cfg.get("model_id", "reference-planner-1"), cfg.get("follows_embedded_instructions", False))
