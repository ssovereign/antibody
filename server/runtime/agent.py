"""LangGraph reference agent with a persistent SQLite checkpointer.

Stages: prepare context -> request a structured action -> call the gateway
-> verify the result -> continue or stop. Serializable graph state carries
run_id, branch_id, thread_id, task_revision, context_packet_id, next_phase,
action_id and operation_id. Re-entering the gateway stage after a replay or
crash reuses the server-issued operation ID, so the gateway reconciles it
before any dispatch (LangGraph persistence alone does not deduplicate effects).
"""
from __future__ import annotations

import copy
import sqlite3
import time
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..canonical import dumps, loads, new_id, utcnow
from ..schemas import ActionProposal, ModelAction
from .planner import build_planner
from .procedures import apply_procedures


class AgentState(TypedDict, total=False):
    run_id: str
    branch_id: str
    thread_id: str
    task_revision: int
    context_packet_id: str
    next_phase: str
    action_id: str
    operation_id: str
    pending: dict
    observations: list
    progress: dict
    last_decision: dict
    steps: int
    resume_count: int
    stop_reason: str


class ReferenceAgent:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self._conn = sqlite3.connect(core.config.CHECKPOINT_PATH, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.saver = SqliteSaver(self._conn)
        self.saver.setup()
        g = StateGraph(AgentState)
        g.add_node("prepare", self.prepare)
        g.add_node("propose", self.propose)
        g.add_node("act", self.act)
        g.add_node("verify", self.verify)
        g.add_edge(START, "prepare")
        g.add_conditional_edges("prepare", lambda s: s["next_phase"], {"propose": "propose", "act": "act", "stop": END})
        g.add_conditional_edges("propose", lambda s: s["next_phase"], {"act": "act", "stop": END})
        g.add_edge("act", "verify")
        g.add_conditional_edges("verify", lambda s: s["next_phase"], {"prepare": "prepare", "stop": END})
        self.graph = g.compile(checkpointer=self.saver)
        self.crash_after = None  # test hook: "act_before_verify" simulates a crash after dispatch

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 400}

    def get_state(self, thread_id: str) -> dict:
        snap = self.graph.get_state(self.config(thread_id))
        return dict(snap.values) if snap and snap.values else {}

    def seed_state(self, thread_id: str, values: dict) -> None:
        """Write an initial state for a restored or continued run (as if after `verify`)."""
        self.graph.update_state(self.config(thread_id), values, as_node="verify")

    def invoke(self, run: dict, initial: dict | None) -> dict:
        cfg = self.config(run["thread_id"])
        existing = self.get_state(run["thread_id"])
        if existing:
            payload = {"resume_count": existing.get("resume_count", 0) + 1, "next_phase": "prepare"}
        else:
            payload = initial or {}
        return self.graph.invoke(payload, cfg)

    # ------------------------------------------------------------ nodes
    def _run(self, state) -> dict:
        return self.db.one("SELECT * FROM runs WHERE run_id=?", (state["run_id"],))

    def prepare(self, state: AgentState) -> dict:
        run = self._run(state)
        if run["status"] != "running":
            return {"next_phase": "stop", "stop_reason": run["status_reason"] or run["status"]}
        if state.get("pending"):
            # Replay or crash recovery: reconcile the same operation before any new planning.
            return {"next_phase": "act"}
        coord = self.core.coordinator
        coord.refresh_registry(run)
        run = self._run(state)
        progress = copy.deepcopy(state.get("progress") or {})
        progress["journal_sent"] = coord.journal_completed_sends(run)
        progress["denied"] = progress.get("denied", [])[-10:]
        cfg = loads(run["start_config_json"])
        extra = cfg.get("lessons_under_test", [])
        packet = self.core.compiler.compile(run, {"observations": state.get("observations", []), "progress": progress}, extra_lessons=extra)
        self.core.journal.append(run["project_id"], run["run_id"], "context.compiled",
                                 {"packet_id": packet["packet_id"], "token_count": packet["token_count"], "count_kind": packet["count_kind"],
                                  "budget_tokens": packet["budget_tokens"], "fits": packet["fits"],
                                  "lessons": [l["ref"] for l in packet["lessons"]], "observations": len(packet["observations"]),
                                  "omitted": packet["omitted"][:30], "pressure": packet.get("pressure", False)},
                                 actor_type="runtime", registry_revision=run["registry_revision"])
        if not packet["fits"]:
            coord.set_status(run["run_id"], "paused", "CONTEXT_BUDGET")
            self.core.health.transition(run["run_id"], "Paused", "CONTEXT_BUDGET", {"reason": packet.get("reason")})
            return {"next_phase": "stop", "stop_reason": "CONTEXT_BUDGET", "context_packet_id": packet["packet_id"], "progress": progress}
        coord._packets[run["run_id"]] = packet
        return {"next_phase": "propose", "context_packet_id": packet["packet_id"], "progress": progress}

    def propose(self, state: AgentState) -> dict:
        run = self._run(state)
        coord = self.core.coordinator
        packet = coord._packets.get(run["run_id"])
        cfg = loads(run["start_config_json"])
        planner = coord.planner_for(run, cfg)
        progress = copy.deepcopy(state.get("progress") or {})
        t0 = time.time()
        try:
            out = planner.next_action(packet, progress)
        except Exception as e:
            coord.set_status(run["run_id"], "paused", "MODEL_ERROR")
            self.core.health.transition(run["run_id"], "Paused", "MODEL_ERROR", {"error": f"{type(e).__name__}: {str(e)[:200]}"})
            return {"next_phase": "stop", "stop_reason": "MODEL_ERROR"}
        call_id = new_id("call")
        from ..context.compiler import estimate_tokens
        in_tok = out.input_tokens if out.input_tokens is not None else packet["token_count"]
        out_tok = out.output_tokens if out.output_tokens is not None else estimate_tokens(dumps(out.arguments))
        self.db.execute("INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (call_id, run["project_id"], run["run_id"], packet["packet_id"], planner.provider, planner.model_id,
                         in_tok, out_tok, out.count_kind, dumps({"finish": out.finish, "tool_id": out.tool_id, "rationale": out.rationale}),
                         int((time.time() - t0) * 1000), utcnow()))
        obs_refs = [o["ref"] for o in packet["observations"]]
        with self.db.tx() as c:
            for ref in obs_refs:
                c.execute("INSERT INTO edges(project_id,run_id,src_ref,dst_ref,kind,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
                          (run["project_id"], run["run_id"], ref, f"call:{call_id}", "model_input", "{}", utcnow()))
        coord.add_cost(run["run_id"], in_tok, out_tok, out.count_kind)
        self.core.journal.append(run["project_id"], run["run_id"], "model.called",
                                 {"call_id": call_id, "provider": planner.provider, "model_id": planner.model_id, "finish": out.finish,
                                  "tool_id": out.tool_id, "rationale": out.rationale, "input_tokens": in_tok, "output_tokens": out_tok,
                                  "count_kind": out.count_kind, "packet_id": packet["packet_id"]}, actor_type="model")
        if out.finish:
            coord.finish(run, out.rationale, progress)
            return {"next_phase": "stop", "stop_reason": "finished", "progress": progress}
        try:
            action = ModelAction(tool_id=out.tool_id, arguments=out.arguments)
        except Exception:
            coord.set_status(run["run_id"], "paused", "MODEL_OUTPUT_INVALID")
            return {"next_phase": "stop", "stop_reason": "MODEL_OUTPUT_INVALID"}
        tool = self.core.projects.tool(run["project_id"], action.tool_id)
        args = dict(action.arguments)
        applied = []
        if tool:
            branch = self.core.versions.branch(run["branch_id"])
            args, applied = apply_procedures(tool, args, packet["lessons"], branch["state"]["environment"])
            for a in applied:
                if a["applied"]:
                    self.db.execute("INSERT INTO lesson_uses(project_id,run_id,lesson_ref,use_kind,detail_json,created_at) VALUES (?,?,?,?,?,?)",
                                    (run["project_id"], run["run_id"], a["ref"], "applied_procedure", dumps(a), utcnow()))
                self.core.journal.append(run["project_id"], run["run_id"], "lesson.applied", a, actor_type="runtime",
                                         evidence_refs=[f"lesson:{a['ref']}"])
        action_id, operation_id = coord.issue_operation(run, purpose=f"{action.tool_id}")
        pending = {"tool_id": action.tool_id, "arguments": args, "schema_hash": tool["schema_hash"] if tool else None,
                   "observation_refs": obs_refs[-40:] + [f"lesson:{a['ref']}" for a in applied if a["applied"]], "call_id": call_id}
        with self.db.tx() as c:
            c.execute("INSERT INTO edges(project_id,run_id,src_ref,dst_ref,kind,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
                      (run["project_id"], run["run_id"], f"call:{call_id}", f"action:{action_id}", "produced", "{}", utcnow()))
        return {"next_phase": "act", "pending": pending, "action_id": action_id, "operation_id": operation_id, "progress": progress}

    def act(self, state: AgentState) -> dict:
        run = self._run(state)
        p = state["pending"]
        proposal = ActionProposal(action_id=state["action_id"], operation_id=state["operation_id"], tool_id=p["tool_id"],
                                  tool_schema_hash=p.get("schema_hash"), arguments=p["arguments"], observation_refs=p.get("observation_refs", []))
        view = self.core.gateway.submit(run["run_id"], proposal, self.core.worker_identity)
        if self.crash_after and (self.crash_after == "act_before_verify" or self.crash_after == f"act_before_verify:{p['tool_id']}"):
            self.crash_after = None
            raise RuntimeError("simulated crash after dispatch")
        return {"last_decision": view.model_dump(), "next_phase": "verify"}

    def verify(self, state: AgentState) -> dict:
        run = self._run(state)
        coord = self.core.coordinator
        p = state.get("pending") or {}
        d = state.get("last_decision") or {}
        progress = copy.deepcopy(state.get("progress") or {})
        observations = list(state.get("observations") or [])
        progressed = coord.process_result(run, p, d, progress, observations)
        steps = state.get("steps", 0) + 1
        self.core.health.on_step(run["run_id"], progressed)
        run = self._run(state)
        nxt = "prepare" if run["status"] == "running" else "stop"
        return {"next_phase": nxt, "pending": {}, "progress": progress, "observations": observations, "steps": steps,
                "stop_reason": None if nxt == "prepare" else (run["status_reason"] or run["status"])}
