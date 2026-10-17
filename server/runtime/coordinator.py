"""Run coordinator: run lifecycle, operation issuance, observation intake and
screening, artifact checks, bounded recovery, clean-context repair, adapter
state export/restore and capability profiles.
"""
from __future__ import annotations

import copy
import shutil
import threading
from pathlib import Path

from ..canonical import dumps, hash_value, loads, new_id, utcnow
from ..evaluation.checkers import check_report_bytes
from ..executors.files import resolve_in_workspace
from ..learning.patterns import features, match_chunk, sentences
from ..projects import ConflictError, InvalidRequest, NotFound
from ..schemas import AdapterCapabilities
from .planner import build_planner

CAPABILITIES = {
    "langgraph_reference": AdapterCapabilities(intercept_actions=True, label_observations=True, export_messages=True,
                                               restore_cursor=True, isolate_workspace=True, cancel_or_pause=True, export_context=True),
    "mcp_proxy": AdapterCapabilities(intercept_actions=True, label_observations=True, export_messages=False,
                                     restore_cursor=False, isolate_workspace=False, cancel_or_pause=True, export_context=False),
}
CAPABILITY_NOTES = {
    "langgraph_reference": "Reference agent: every tool call and observation passes through ANTIBODY; full save/restore of supported state.",
    "mcp_proxy": "Tool-only proxy: covers proxied tool calls. The host agent's context and model cursor are not visible, so full-agent "
                 "restore is disabled. A host that keeps a direct shell, browser or filesystem tool retains a bypass outside this protection.",
}


class Coordinator:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.journal = core.journal
        self.config = core.config
        self._packets: dict[str, dict] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._glock = threading.Lock()

    # ================================================================ capabilities
    def capabilities(self, adapter: str | None) -> AdapterCapabilities:
        return CAPABILITIES.get(adapter or "", AdapterCapabilities(intercept_actions=False, label_observations=False, export_messages=False,
                                                                  restore_cursor=False, isolate_workspace=False, cancel_or_pause=False,
                                                                  export_context=False))

    def capability_profiles(self) -> list[dict]:
        return [{"adapter": k, "capabilities": v.model_dump(), "note": CAPABILITY_NOTES[k],
                 "tested_by": {"langgraph_reference": ["T03", "T07", "T08", "T09"], "mcp_proxy": ["T13"]}[k]} for k, v in CAPABILITIES.items()]

    # ================================================================ agents
    def create_agent(self, project_id: str, name: str, adapter: str = "langgraph_reference", config: dict | None = None,
                     actor: str = "system") -> dict:
        if adapter not in CAPABILITIES:
            raise InvalidRequest("UNSUPPORTED_ADAPTER", f"adapter {adapter} has no tested integration")
        cfg = config or {}
        cfg.setdefault("model", {"provider": self.config.MODEL_PROVIDER, "model_id": self.config.MODEL_ID, "temperature": "0"})
        aid = new_id("agent")
        self.db.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?)", (aid, project_id, name, adapter, None, dumps(cfg), utcnow()))
        from ..versions.store import default_branch_state
        agent = self.db.one("SELECT * FROM agents WHERE agent_id=?", (aid,))
        b = self.core.versions.create_branch(project_id, aid, "main", default_branch_state(agent), purpose="work")
        self.db.execute("UPDATE agents SET accepted_branch_id=? WHERE agent_id=?", (b["branch_id"], aid))
        self.core.versions.save(project_id, b["branch_id"], None, "initial", actor)
        return self.agent(aid)

    def agent(self, agent_id: str) -> dict:
        a = self.db.one("SELECT * FROM agents WHERE agent_id=?", (agent_id,))
        if not a:
            raise NotFound(f"agent {agent_id}")
        a["config"] = loads(a.pop("config_json"))
        return a

    def agents(self, project_id: str) -> list[dict]:
        out = []
        for r in self.db.all("SELECT agent_id FROM agents WHERE project_id=? ORDER BY created_at", (project_id,)):
            a = self.agent(r["agent_id"])
            last = self.db.one("SELECT run_id, status, health, mode FROM runs WHERE agent_id=? ORDER BY created_at DESC LIMIT 1", (a["agent_id"],))
            a["last_run"] = last
            a["capabilities"] = self.capabilities(a["adapter"]).model_dump()
            out.append(a)
        return out

    def planner_for(self, run: dict, cfg: dict):
        return build_planner(cfg["model"], self.core.compiler)

    # ================================================================ runs
    def start_run(self, project_id: str, agent_id: str, task_ref: str, *, base_version: str | None = None,
                  branch_id: str | None = None, adapter: str | None = None, context_budget_tokens: int = 6000,
                  mode: str | None = None, actor: str = "app", purpose: str = "task", parent_run_id: str | None = None,
                  extra: dict | None = None, limits: dict | None = None, wait: bool = False, continue_from: str | None = None) -> dict:
        agent = self.agent(agent_id)
        if agent["project_id"] != project_id:
            raise NotFound(f"agent {agent_id}")
        adapter = adapter or agent["adapter"]
        if adapter != agent["adapter"]:
            raise InvalidRequest("ADAPTER_MISMATCH", "run adapter must match the agent's tested adapter")
        if adapter != "langgraph_reference":
            raise InvalidRequest("CAPABILITY_UNSUPPORTED", f"{adapter} runs are driven by the host agent, not started here", status=422)
        tid, rev = self.core.projects.parse_task_ref(task_ref)
        cur = self.core.projects.current_task_revision(project_id, tid)
        if rev is not None and rev != cur:
            raise ConflictError("STALE_REVISION", f"task {tid} is at revision {cur}; runs bind the current revision")
        task = self.core.projects.get_task(project_id, tid, cur)
        if branch_id is None:
            branch_id = agent["accepted_branch_id"]
            if base_version:
                head = self.core.versions.branch(branch_id)["head_version"]
                if base_version != head:
                    fork = self.core.versions.fork(project_id, base_version, None, "restore", actor)
                    branch_id = fork["branch"]["branch_id"]
        branch = self.core.versions.branch(branch_id)
        if branch["agent_id"] != agent_id:
            raise InvalidRequest("BRANCH_MISMATCH", "branch belongs to a different agent")
        if self.db.scalar("SELECT 1 FROM runs WHERE branch_id=? AND status IN ('running','recovering')", (branch_id,)):
            raise ConflictError("BRANCH_BUSY", "another run is active on this branch")
        model_cfg = dict(branch["state"].get("model") or agent["config"]["model"])
        model_cfg.update({k: v for k, v in agent["config"].get("planner", {}).items()})
        if extra and extra.get("model_override"):
            model_cfg.update(extra.pop("model_override"))
        planner = build_planner(model_cfg, self.core.compiler)
        if mode and mode != planner.mode:
            raise InvalidRequest("MODE_MISMATCH", f"this agent's model provider runs in {planner.mode} mode, not {mode}")
        policy = self.core.projects.policy(project_id)
        reg = self.core.registry.head(project_id)
        lim = {"max_tool_steps": min(task["body"]["max_tool_steps"], self.config.MAX_TOOL_STEPS),
               "repeat_failure_limit": self.config.REPEAT_FAILURE_LIMIT, "no_progress_limit": self.config.NO_PROGRESS_LIMIT,
               "max_repair_steps": self.config.MAX_REPAIR_STEPS, "max_run_cost": str(self.config.MAX_RUN_COST)}
        lim.update(limits or {})
        start_cfg = {"schema_version": 1, "agent": {"agent_id": agent_id, "name": agent["name"], "adapter": adapter},
                     "model": model_cfg, "task_ref": f"{tid}@{cur}", "policy_revision": policy["revision"],
                     "registry_revision": reg, "base_version": branch["head_version"], "limits": lim,
                     "context_budget_tokens": context_budget_tokens, "purpose": purpose, "environment": branch["state"]["environment"]}
        start_cfg.update(extra or {})
        start_hash = hash_value(_canon(start_cfg))
        rid = new_id("run")
        thread_id = f"{branch['thread_ns']}:{rid}"
        now = utcnow()
        self.db.execute("INSERT INTO runs(run_id,project_id,agent_id,branch_id,parent_run_id,restored_from_version,task_id,task_revision,adapter,mode,status,health,policy_revision,registry_revision,base_version,context_budget_tokens,start_config_json,start_config_hash,thread_id,purpose,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, project_id, agent_id, branch_id, parent_run_id, branch["base_version"] if branch["purpose"] == "restore" else None,
                         tid, cur, adapter, planner.mode, "running", "Healthy", policy["revision"], reg, branch["head_version"],
                         context_budget_tokens, dumps(start_cfg), start_hash, thread_id, purpose, now, now))
        run = self.run(rid)
        self.journal.append(project_id, rid, "run.started",
                            {"agent_id": agent_id, "branch_id": branch_id, "task_ref": f"{tid}@{cur}", "mode": planner.mode,
                             "model": {"provider": planner.provider, "model_id": planner.model_id,
                                       "follows_embedded_instructions": model_cfg.get("follows_embedded_instructions", False)},
                             "adapter": adapter, "context_budget_tokens": context_budget_tokens, "limits": lim,
                             "purpose": purpose, "parent_run_id": parent_run_id, "start_config_hash": start_hash},
                            actor_type="coordinator", policy_revision=policy["revision"], registry_revision=reg)
        self.core.registry.ack(run, reg, "run_start")
        # seed state: restored cursor or continuation
        seed = None
        cursor = branch["state"].get("restored_cursor")
        if continue_from:
            prev = self.run(continue_from)
            seed = self.core.agent.get_state(prev["thread_id"])
            self._bind_ops(rid, self._ops_for(prev["run_id"]), continue_from)
        elif cursor and cursor.get("state") and (cursor.get("task_ref") or "").split("@")[0] == tid:
            seed = cursor["state"]
            self._bind_ops(rid, cursor.get("operation_ids", []), cursor.get("run_id") or "restored")
            st = branch["state"]
            st.pop("restored_cursor", None)
            self.core.versions.set_state(branch_id, st)
        if seed:
            seed = {k: v for k, v in seed.items() if k in ("observations", "progress", "pending", "action_id", "operation_id", "steps")}
            # keep validated progress; reset per-attempt counters of the historical run
            prog = dict(seed.get("progress") or {})
            if not (prog.get("report") or {}).get("checked_passed"):
                prog.pop("report", None)
            prog.pop("denied", None)
            seed["progress"] = prog
            seed["steps"] = 0
            seed.update({"run_id": rid, "branch_id": branch_id, "thread_id": thread_id, "task_revision": cur, "next_phase": "prepare"})
            self.core.agent.seed_state(thread_id, seed)
        if wait:
            self.run_loop(rid)
        else:
            self.core.jobs.enqueue(project_id, "run", {"run_id": rid})
        return self.run(rid)

    def _initial_state(self, run: dict) -> dict:
        return {"run_id": run["run_id"], "branch_id": run["branch_id"], "thread_id": run["thread_id"],
                "task_revision": run["task_revision"], "observations": [], "progress": {}, "steps": 0, "pending": {},
                "resume_count": 0, "next_phase": "prepare"}

    def _ops_for(self, run_id: str) -> list[str]:
        ops = [r["operation_id"] for r in self.db.all("SELECT operation_id FROM operations WHERE run_id=?", (run_id,))]
        ops += [r["operation_id"] for r in self.db.all("SELECT operation_id FROM operation_bindings WHERE run_id=?", (run_id,))]
        return ops

    def _bind_ops(self, run_id: str, ops: list[str], from_run: str) -> None:
        with self.db.tx() as c:
            for op in ops:
                if c.execute("SELECT 1 FROM operations WHERE operation_id=?", (op,)).fetchone():
                    c.execute("INSERT OR IGNORE INTO operation_bindings VALUES (?,?,?)", (run_id, op, from_run))

    def run_lock(self, run_id: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(run_id, threading.Lock())

    def run_loop(self, run_id: str) -> dict:
        lock = self.run_lock(run_id)
        if not lock.acquire(blocking=False):
            return self.run(run_id)
        try:
            run = self.run(run_id)
            if run["status"] == "running":
                try:
                    self.core.agent.invoke(run, self._initial_state(run))
                except Exception as e:
                    if getattr(self.core, "propagate_runtime_errors", False):
                        raise
                    self.set_status(run_id, "paused", "RUNTIME_ERROR")
                    self.journal.append(run["project_id"], run_id, "run.status", {"status": "paused", "reason": "RUNTIME_ERROR",
                                                                                  "error": f"{type(e).__name__}: {str(e)[:300]}"}, actor_type="coordinator")
            run = self.run(run_id)
            if run["status"] == "recovering":
                self.do_recovery(run)
            return self.run(run_id)
        finally:
            lock.release()

    def run(self, run_id: str) -> dict:
        r = self.db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if not r:
            raise NotFound(f"run {run_id}")
        return r

    def run_view(self, run_id: str) -> dict:
        r = dict(self.run(run_id))
        r["start_config"] = loads(r.pop("start_config_json"))
        r["cost"] = loads(r.pop("cost_json"), {})
        r["capabilities"] = self.capabilities(r["adapter"]).model_dump()
        r["health_transitions"] = self.core.health.transitions(run_id)
        r["counts"] = {
            "decisions": self.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=?", (run_id,)),
            "denied": self.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=? AND outcome='DENY'", (run_id,)),
            "denied_executor_invocations": self.db.scalar(
                "SELECT COUNT(*) FROM executor_invocations i JOIN decisions d ON d.decision_id=i.decision_id WHERE d.run_id=? AND d.outcome!='ALLOW'", (run_id,)),
            "effects_completed": self.db.scalar("SELECT COUNT(*) FROM effects WHERE run_id=? AND state='COMPLETED'", (run_id,)),
            "effects_unknown": self.db.scalar("SELECT COUNT(*) FROM effects WHERE run_id=? AND state='UNKNOWN'", (run_id,)),
            "quarantined_spans": self.db.scalar("SELECT COUNT(*) FROM dispositions WHERE run_id=? AND disposition='quarantined'", (run_id,)),
            "lesson_uses": self.db.scalar("SELECT COUNT(*) FROM lesson_uses WHERE run_id=?", (run_id,)),
        }
        r["registry_acks"] = self.db.all("SELECT revision, boundary, acked_at FROM registry_acks WHERE run_id=? ORDER BY revision", (run_id,))
        r["children"] = [c["run_id"] for c in self.db.all("SELECT run_id FROM runs WHERE parent_run_id=?", (run_id,))]
        return r

    def runs(self, project_id: str, limit: int = 100) -> list[dict]:
        return self.db.all("SELECT run_id, agent_id, branch_id, task_id, task_revision, mode, status, status_reason, health, purpose, parent_run_id, created_at, updated_at FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit))

    def set_status(self, run_id: str, status: str, reason: str | None = None) -> None:
        run = self.run(run_id)
        if run["status"] == status and run["status_reason"] == reason:
            return
        self.db.execute("UPDATE runs SET status=?, status_reason=?, updated_at=? WHERE run_id=?", (status, reason, utcnow(), run_id))
        self.journal.append(run["project_id"], run_id, "run.status", {"status": status, "reason": reason, "from": run["status"]},
                            actor_type="coordinator")

    def pause(self, run_id: str, actor: str, deadline: float = 3.0) -> dict:
        import time
        run = self.run(run_id)
        if run["status"] not in ("running", "recovering"):
            return {"run": self.run_view(run_id), "uncertain_effects": []}
        self.set_status(run_id, "paused", "OPERATOR_PAUSE")
        t0 = time.time()
        while self.db.scalar("SELECT COUNT(*) FROM effects WHERE run_id=? AND state='DISPATCHING'", (run_id,)) and time.time() - t0 < deadline:
            time.sleep(0.05)
        uncertain = self.db.all("SELECT operation_id, tool_id FROM effects WHERE run_id=? AND state IN ('DISPATCHING','UNKNOWN')", (run_id,))
        run = self.run(run_id)
        if run["health"] not in ("Paused",):
            try:
                self.core.health.transition(run_id, "Paused", "OPERATOR_PAUSE", {"uncertain_effects": len(uncertain)})
            except ValueError:
                pass
        return {"run": self.run_view(run_id), "uncertain_effects": uncertain}

    def resume(self, run_id: str, actor: str, wait: bool = False) -> dict:
        run = self.run(run_id)
        if run["status"] not in ("paused",):
            raise ConflictError("NOT_PAUSED", f"run is {run['status']}")
        cur = self.core.projects.current_task_revision(run["project_id"], run["task_id"])
        pol = self.core.projects.get_project(run["project_id"])["policy_head"]
        if cur != run["task_revision"] or pol != run["policy_revision"]:
            raise ConflictError("STALE_REVISION", "authorization changed; start a continuation run under the current task and policy",
                                detail={"current_task_revision": cur, "current_policy": pol})
        unk = self.db.scalar("SELECT COUNT(*) FROM effects WHERE run_id=? AND state IN ('UNKNOWN','DISPATCHING')", (run_id,))
        if unk:
            raise ConflictError("OUTCOME_UNKNOWN", "reconcile unknown effects before resuming")
        if run["health"] in ("Paused", "Unknown"):
            self.core.health.transition(run_id, "Healthy" if run["health"] == "Paused" else "Paused", "OPERATOR_RESUME", {"actor": actor})
        self.set_status(run_id, "running", "OPERATOR_RESUME")
        if wait:
            self.run_loop(run_id)
        else:
            self.core.jobs.enqueue(run["project_id"], "run", {"run_id": run_id})
        return self.run_view(run_id)

    def continue_run(self, run_id: str, actor: str, wait: bool = False, context_budget_tokens: int | None = None) -> dict:
        """A new continuation run on the same branch under current authorization; the historical run is preserved."""
        old = self.run(run_id)
        if old["status"] in ("running", "recovering"):
            raise ConflictError("RUN_ACTIVE", "pause the run first")
        unk = self.db.scalar("SELECT COUNT(*) FROM effects WHERE run_id=? AND state IN ('UNKNOWN','DISPATCHING')", (run_id,))
        if unk:
            raise ConflictError("OUTCOME_UNKNOWN", "reconcile unknown effects before continuing")
        if old["status"] == "paused":
            self.set_status(run_id, "superseded", "CONTINUED")
        return self.start_run(old["project_id"], old["agent_id"], old["task_id"], branch_id=old["branch_id"],
                              context_budget_tokens=context_budget_tokens or old["context_budget_tokens"], actor=actor,
                              parent_run_id=run_id, purpose="continuation", wait=wait, continue_from=run_id)

    def finish(self, run: dict, rationale: str, progress: dict) -> None:
        task = self.core.projects.get_task(run["project_id"], run["task_id"], run["task_revision"])
        out = task["body"]["resources"].get("output_path")
        reason = "FINISHED"
        if out:
            art = self.db.one("SELECT check_status FROM artifacts WHERE project_id=? AND branch_id=? AND path=?", (run["project_id"], run["branch_id"], out))
            reason = "TASK_CHECK_PASSED" if art and art["check_status"] == "passed" else "TASK_CHECK_FAILED"
        health = self.run(run["run_id"])["health"]
        if health in ("Recovering", "Degraded"):
            if reason in ("TASK_CHECK_PASSED", "FINISHED"):
                self.core.health.transition(run["run_id"], "Healthy", "INDEPENDENT_CHECKS_PASS", {"final_check": reason})
            else:
                self.core.health.transition(run["run_id"], "Paused", "TASK_CHECK_FAILED", {"final_check": reason})
        self.set_status(run["run_id"], "completed", reason)

    def add_cost(self, run_id: str, in_tok: int | None, out_tok: int | None, kind: str) -> None:
        run = self.run(run_id)
        cost = loads(run["cost_json"], {})
        cost["model_calls"] = cost.get("model_calls", 0) + 1
        cost["input_tokens"] = cost.get("input_tokens", 0) + (in_tok or 0)
        cost["output_tokens"] = cost.get("output_tokens", 0) + (out_tok or 0)
        cost["count_kind"] = kind if cost.get("count_kind") in (None, kind) else "mixed"
        cfg = loads(run["start_config_json"])
        price = _price(self.config.PRICE_CONFIG_PATH, cfg.get("model", {}).get("model_id", ""))
        if price is not None:
            from decimal import Decimal
            usd = (Decimal(cost["input_tokens"]) * Decimal(price["input_per_mtok"]) + Decimal(cost["output_tokens"]) * Decimal(price["output_per_mtok"])) / Decimal(1_000_000)
            cost["estimated_cost"] = str(usd.quantize(Decimal("0.000001")))
            cost["price_source"] = "fixtures/price_config.json"
        else:
            cost["estimated_cost"] = None
            cost["price_source"] = "not measured (no saved price for this model)"
        self.db.execute("UPDATE runs SET cost_json=?, steps=steps+1 WHERE run_id=?", (dumps(cost), run_id))
        limit = cfg.get("limits", {}).get("max_run_cost")
        if cost.get("estimated_cost") is not None and limit is not None:
            from decimal import Decimal
            if Decimal(cost["estimated_cost"]) > Decimal(str(limit)) and run["status"] == "running":
                self.core.health.transition(run_id, "Paused", "BUDGET_EXCEEDED", {"estimated_cost": cost["estimated_cost"], "limit": str(limit)})
                self.set_status(run_id, "paused", "BUDGET_EXCEEDED")

    # ================================================================ operations
    def issue_operation(self, run: dict, purpose: str) -> tuple[str, str]:
        action_id, op = new_id("action"), new_id("op")
        self.db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?)", (op, run["project_id"], run["run_id"], action_id, purpose, utcnow()))
        return action_id, op

    def journal_completed_sends(self, run: dict) -> list[str]:
        """Recipients already sent to for this task, from the permanent journal (checked arguments of COMPLETED effects)."""
        rows = self.db.all("SELECT d.canonical_args_ref, e.project_id FROM effects e JOIN runs r ON r.run_id=e.run_id JOIN decisions d ON d.decision_id=e.decision_id "
                           "WHERE e.project_id=? AND r.task_id=? AND e.tool_id='send_report' AND e.state='COMPLETED'",
                           (run["project_id"], run["task_id"]))
        out = []
        for r in rows:
            args = self.core.objects.get_json(r["project_id"], r["canonical_args_ref"], restricted=True)
            if args.get("recipient_id"):
                out.append(args["recipient_id"])
        return sorted(set(out))

    def refresh_registry(self, run: dict) -> None:
        settings = self.core.projects.get_project(run["project_id"])["settings"]
        if settings.get("registry_refresh", "step_boundary") != "step_boundary":
            return
        head = self.core.registry.head(run["project_id"])
        if head > run["registry_revision"]:
            self.db.execute("UPDATE runs SET registry_revision=? WHERE run_id=?", (head, run["run_id"]))
            run = self.run(run["run_id"])
            self.core.registry.ack(run, head, "step_boundary")

    # ================================================================ observations
    def receive_observation(self, run: dict, source_id: str) -> list[str]:
        pid = run["project_id"]
        src = self.core.sources.get(source_id)
        cfg = loads(run["start_config_json"])
        patterns = self.core.registry.active(pid, run["registry_revision"], kind="threat_pattern") + cfg.get("patterns_under_test", [])
        refs, quarantined, flagged = [], [], []
        now = utcnow()
        for ch in self.core.sources.chunks(source_id):
            matches = match_chunk(ch, patterns, src["channel"]) if patterns else []
            q = [m for m in matches if m["disposition"] == "quarantined"]
            f = [m for m in matches if m["disposition"] == "flagged"]
            with self.db.tx() as c:
                c.execute("INSERT INTO edges(project_id,run_id,src_ref,dst_ref,kind,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
                          (pid, run["run_id"], ch["ref"], f"run:{run['run_id']}", "observation", dumps({"origin": src["origin"]}), now))
                for m in q:
                    c.execute("INSERT INTO dispositions(project_id,run_id,source_id,chunk_index,disposition,pattern_ref,reason,detail_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (pid, run["run_id"], source_id, ch["chunk_index"], "quarantined", m["pattern_ref"], m["reason"],
                               dumps({"spans": [[m["start"], m["end"]]], "level": m["level"], "stage": "ingress"}), now))
                    c.execute("INSERT INTO lesson_uses(project_id,run_id,lesson_ref,use_kind,detail_json,created_at) VALUES (?,?,?,?,?,?)",
                              (pid, run["run_id"], m["pattern_ref"], "early_interception", dumps({"chunk": ch["ref"], "level": m["level"]}), now))
                for m in f:
                    c.execute("INSERT INTO dispositions(project_id,run_id,source_id,chunk_index,disposition,pattern_ref,reason,detail_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (pid, run["run_id"], source_id, ch["chunk_index"], "flagged", m["pattern_ref"], m["reason"],
                               dumps({"spans": [[m["start"], m["end"]]], "level": m["level"], "stage": "ingress"}), now))
            quarantined += [{"chunk": ch["ref"], **m} for m in q]
            flagged += [{"chunk": ch["ref"], **m} for m in f]
            refs.append(ch["ref"])
        excl = self.core.sources.exclusions(source_id)
        self.journal.append(pid, run["run_id"], "observation.received",
                            {"source_id": source_id, "channel": src["channel"], "origin": src["origin"], "label": src["label"],
                             "chunks": refs, "parse_exclusions": [{"kind": e["kind"], "start": e["start_offset"], "end": e["end_offset"],
                                                                   "reason": e["detail"].get("reason")} for e in excl],
                             "quarantined_spans": len(quarantined), "flagged_spans": len(flagged)},
                            actor_type="ingress", evidence_refs=refs)
        for m in quarantined:
            self.journal.append(pid, run["run_id"], "source.quarantined",
                                {"chunk": m["chunk"], "pattern_ref": m["pattern_ref"], "level": m["level"], "span": [m["start"], m["end"]],
                                 "stage": "before model ingestion"}, actor_type="ingress", evidence_refs=[m["chunk"], f"lesson:{m['pattern_ref']}"])
        return refs

    # ================================================================ results
    def process_result(self, run: dict, p: dict, d: dict, progress: dict, observations: list) -> bool:
        if not p or not d:
            return False
        tool = p["tool_id"]
        args = p["arguments"]
        key = {"send_report": f"send:{args.get('recipient_id')}", "delete_file": f"delete:{args.get('path')}",
               "fetch_url": f"fetch:{args.get('url')}", "make_payment": f"pay:{args.get('amount')}"}.get(tool)
        if key:
            progress.setdefault("attempted", [])
            if key not in progress["attempted"]:
                progress["attempted"].append(key)
        progressed = False
        state = d.get("effect_state")
        self.db.execute("INSERT INTO edges(project_id,run_id,src_ref,dst_ref,kind,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
                        (run["project_id"], run["run_id"], f"action:{d['action_id']}", f"decision:{d['decision_id']}", "decided",
                         dumps({"outcome": d["outcome"]}), utcnow()))
        if d["outcome"] == "DENY":
            progress.setdefault("denied", []).append({"tool_id": tool, "reason_code": d["reason_code"]})
        elif d["outcome"] == "ALLOW" and (state == "COMPLETED" or d.get("deduplicated")):
            res = d.get("result") or {}
            if tool == "read_source":
                refs = self.receive_observation(run, res["source_id"])
                observations.extend(r for r in refs if r not in observations)
                progress.setdefault("read", []).append(res["source_id"])
                progressed = True
            elif tool == "fetch_url":
                eff = self.db.one("SELECT external_ref FROM effects WHERE project_id=? AND operation_id=?", (run["project_id"], d["operation_id"]))
                if eff and eff["external_ref"] and eff["external_ref"].startswith("src_"):
                    refs = self.receive_observation(run, eff["external_ref"])
                    observations.extend(r for r in refs if r not in observations)
                progress.setdefault("fetched", []).append(args["url"])
                progressed = True
            elif tool == "write_report":
                rep = progress.setdefault("report", {})
                rep["written"] = True
                check = self.check_artifact(run, args["path"])
                rep["checked_passed"] = check["passed"]
                progressed = check["passed"]
                if not check["passed"]:
                    rep["failures"] = rep.get("failures", 0) + 1
            elif tool == "send_report":
                progress.setdefault("sent", []).append(args["recipient_id"])
                progressed = True
            else:
                src = self.core.sources.ingest_tool_result(run["project_id"], run["run_id"], tool, res)
                refs = self.receive_observation(run, src["source_id"])
                observations.extend(refs)
                progressed = True
            self.db.execute("INSERT INTO edges(project_id,run_id,src_ref,dst_ref,kind,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
                            (run["project_id"], run["run_id"], f"action:{d['action_id']}", f"effect:{d['operation_id']}", "result", "{}", utcnow()))
        elif d["outcome"] == "ALLOW" and state == "FAILED":
            err = (d.get("result") or {}).get("error") or {}
            if tool == "write_report":
                rep = progress.setdefault("report", {})
                rep["failures"] = rep.get("failures", 0) + 1
            src = self.core.sources.ingest_tool_result(run["project_id"], run["run_id"], tool,
                                                       {"status": "error", "exit_code": err.get("code"), "is_error": True,
                                                        "error_family": err.get("family"), "error_message": err.get("message", "")})
            refs = self.receive_observation(run, src["source_id"])
            observations.extend(refs)
        self.journal.append(run["project_id"], run["run_id"], "check.finished",
                            {"check": "step", "tool_id": tool, "outcome": d["outcome"], "effect_state": state, "progress": progressed},
                            actor_type="checker")
        return progressed

    def check_artifact(self, run: dict, path: str) -> dict:
        pid = run["project_id"]
        branch = self.core.versions.branch(run["branch_id"])
        task = self.core.projects.get_task(pid, run["task_id"], run["task_revision"])
        policy = self.core.projects.policy(pid)
        art = self.db.one("SELECT * FROM artifacts WHERE project_id=? AND branch_id=? AND path=?", (pid, branch["branch_id"], path))
        try:
            p = resolve_in_workspace(branch["workspace_dir"], path, task["body"]["file_roots"], must_exist=True)
            data = p.read_bytes()
        except Exception as e:
            data, err = None, str(e)
        from ..canonical import content_ref
        if data is None:
            result = {"passed": False, "checks": [{"check": "exists", "passed": False}]}
        else:
            result = check_report_bytes(data, task["body"]["resources"].get("required_phrases", []) if isinstance(task["body"]["resources"], dict) else [],
                                        policy.get("canary_markers", []))
            result["checks"].insert(0, {"check": "matches_recorded_effect", "passed": bool(art and art["content_hash"] == content_ref(data))})
            result["passed"] = all(c["passed"] for c in result["checks"])
        data_class = "approved_report" if result["passed"] else ("sensitive" if result.get("canary") else "unapproved")
        if art:
            self.db.execute("UPDATE artifacts SET check_status=?, data_class=?, check_detail_json=?, updated_at=? WHERE project_id=? AND branch_id=? AND path=?",
                            ("passed" if result["passed"] else "failed", data_class, dumps(result), utcnow(), pid, branch["branch_id"], path))
        self.journal.append(pid, run["run_id"], "check.finished",
                            {"check": "report_artifact", "path": path, "passed": result["passed"], "checks": result["checks"],
                             "data_class": data_class, "checker": "report_checker@1"}, actor_type="checker")
        return result

    # ================================================================ effect hooks
    def on_effect(self, run: dict, effect: dict, result: dict | None, error: dict | None) -> None:
        if effect["tool_id"] == "fetch_url" and effect["state"] == "COMPLETED" and result and result.get("body_bytes") is not None:
            src = self.core.sources.ingest_bytes(run["project_id"], "web", result["body_bytes"],
                                                 filename=None, content_type=result.get("content_type"), run_id=run["run_id"],
                                                 label=result.get("final_url"),
                                                 extra_meta={"url": result.get("url"), "final_url": result.get("final_url"),
                                                             "redirects": result.get("redirects"), "fetched_sha256": result.get("sha256")})
            self.db.execute("UPDATE effects SET external_ref=? WHERE project_id=? AND operation_id=?", (src["source_id"], run["project_id"], effect["operation_id"]))

    # ================================================================ repair & recovery
    def context_repair(self, run: dict, incident: dict, anchored: list[str]) -> None:
        """Quarantine the suspect spans and continue from a clean packet compiled from trusted
        task state and validated progress (not by appending a warning to the history)."""
        pid = run["project_id"]
        dec_id = next((r.split(":", 1)[1] for r in incident["evidence_refs"] if r.startswith("decision:")), None)
        values = []
        if dec_id:
            d = self.db.one("SELECT canonical_args_ref FROM decisions WHERE decision_id=?", (dec_id,))
            args = self.core.objects.get_json(pid, d["canonical_args_ref"], restricted=True)
            values = [str(v) for v in args.values() if isinstance(v, str) and len(v) >= 3]
        n = 0
        for ref in anchored:
            sid, _, idx = ref.partition(":chunk_")
            ch = self.db.one("SELECT text FROM chunks WHERE source_id=? AND chunk_index=?", (sid, int(idx or 0)))
            if not ch:
                continue
            spans = [[s, e] for s, e, sent in sentences(ch["text"])
                     if any(v.lower() in sent.lower() for v in values) or features(sent)["requested_deviations"]]
            if not spans:
                spans = [[0, len(ch["text"])]]
            self.db.execute("INSERT INTO dispositions(project_id,run_id,source_id,chunk_index,disposition,pattern_ref,reason,detail_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (pid, run["run_id"], sid, int(idx or 0), "quarantined", None, "context_repair after blocked action",
                             dumps({"spans": spans, "incident_id": incident["incident_id"], "stage": "after action"}), utcnow()))
            self.core.compiler.invalidate_for_source(pid, sid)
            self.journal.append(pid, run["run_id"], "source.quarantined",
                                {"chunk": ref, "spans": spans, "stage": "after blocked action", "incident_id": incident["incident_id"]},
                                actor_type="health_monitor", evidence_refs=[ref])
            n += 1
        h = self.core.health
        h.transition(run["run_id"], "Quarantined", "SUSPECT_CONTEXT", {"spans": n, "incident_id": incident["incident_id"]})
        h.transition(run["run_id"], "Recovering", "CLEAN_CONTEXT_PACKET",
                     {"method": "compile a clean packet from trusted task state and validated progress; quarantined spans excluded"})

    def request_recovery(self, run_id: str, incident: dict, candidate: dict | None) -> None:
        run = self.run(run_id)
        if run["purpose"] == "repair":
            # a repair branch never spawns further repairs: its budget is the bound
            self.core.health.transition(run_id, "Paused", "REPAIR_FAILED", {"incident_id": incident["incident_id"]})
            self.set_status(run_id, "paused", "REPAIR_FAILED")
            return
        self.core.incidents.update(incident["incident_id"], status="recovering" if candidate else "open")
        if not candidate:
            self.core.health.transition(run_id, "Paused", "REPEAT_FAILURE_LIMIT", {"incident_id": incident["incident_id"], "note": "no repair candidate"})
            self.set_status(run_id, "paused", "REPEAT_FAILURE_LIMIT")
            return
        self.set_status(run_id, "recovering", incident["incident_id"])

    def do_recovery(self, run: dict) -> dict | None:
        inc = self.core.incidents.get(run["status_reason"])
        h = self.core.health
        if not inc or inc.get("recovery_run_id") or not inc.get("candidate_id"):
            h.transition(run["run_id"], "Paused", "REPAIR_UNAVAILABLE", {"note": "at most one repair branch per incident"})
            self.set_status(run["run_id"], "paused", "REPAIR_UNAVAILABLE")
            return None
        cand = self.core.learning.candidate(inc["candidate_id"])
        h.transition(run["run_id"], "Recovering", "BOUNDED_REPAIR", {"candidate": f"{cand['candidate_id']}@{cand['revision']}"})
        parent_branch = self.core.versions.branch(run["branch_id"])
        rb = self.core.versions.create_branch(run["project_id"], run["agent_id"], f"repair-{inc['incident_id'][-8:]}",
                                              copy.deepcopy(parent_branch["state"]), parent_branch_id=parent_branch["branch_id"],
                                              base_version=parent_branch["head_version"], purpose="repair")
        src_root = Path(parent_branch["workspace_dir"])
        for p in src_root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                dst = Path(rb["workspace_dir"]) / p.relative_to(src_root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dst)
        self.journal.append(run["project_id"], run["run_id"], "recovery.started",
                            {"incident_id": inc["incident_id"], "repair_branch": rb["branch_id"], "candidate": f"{cand['candidate_id']}@{cand['revision']}",
                             "budget_steps": self.config.MAX_REPAIR_STEPS}, actor_type="coordinator")
        lesson = self.core.learning.as_test_lesson(cand)
        child = self.start_run(run["project_id"], run["agent_id"], run["task_id"], branch_id=rb["branch_id"],
                               context_budget_tokens=run["context_budget_tokens"], actor="health_monitor", purpose="repair",
                               parent_run_id=run["run_id"], extra={"lessons_under_test": [lesson], "candidate_under_test": lesson["ref"]},
                               limits={"max_tool_steps": self.config.MAX_REPAIR_STEPS}, wait=True)
        self.core.incidents.update(inc["incident_id"], recovery_run_id=child["run_id"])
        ok = child["status"] == "completed" and child["status_reason"] == "TASK_CHECK_PASSED"
        if ok:
            hyps = inc["hypotheses"] + [{"status": "intervention_supported", "text": "With the candidate applied on an isolated repair branch the same task passed its independent check; without it the operation failed repeatedly.",
                                         "evidence_refs": [f"run:{child['run_id']}", f"run:{run['run_id']}"]}]
            self.core.incidents.update(inc["incident_id"], status="recovered", hypotheses=hyps, attribution_level="intervention_supported")
            h.transition(run["run_id"], "Healthy", "INDEPENDENT_CHECKS_PASS", {"repair_run": child["run_id"]})
            self.set_status(run["run_id"], "completed", "RECOVERED_IN_BRANCH")
        else:
            h.transition(run["run_id"], "Paused", "REPAIR_FAILED", {"repair_run": child["run_id"], "child_status": child["status_reason"]})
            self.set_status(run["run_id"], "paused", "REPAIR_FAILED")
        self.journal.append(run["project_id"], run["run_id"], "recovery.finished",
                            {"incident_id": inc["incident_id"], "repair_run": child["run_id"], "repair_branch": rb["branch_id"], "passed": ok,
                             "note": "Recovery is not acceptance: the candidate still needs the fixed evaluation suite."}, actor_type="coordinator")
        return child

    # ================================================================ subagents
    def spawn_subagent(self, parent_run_id: str, task_ref: str, actor: str, handoff_refs: list[str] | None = None) -> dict:
        parent = self.run(parent_run_id)
        pb = self.core.versions.branch(parent["branch_id"])
        cb = self.core.versions.create_branch(parent["project_id"], parent["agent_id"], f"sub-{parent_run_id[-6:]}",
                                              copy.deepcopy(pb["state"]), parent_branch_id=pb["branch_id"], purpose="experiment")
        child = self.start_run(parent["project_id"], parent["agent_id"], task_ref, branch_id=cb["branch_id"], actor=actor,
                               purpose="subagent", parent_run_id=parent_run_id, wait=True,
                               extra={"handoff_refs": handoff_refs or []})
        child_obs = self.core.agent.get_state(child["thread_id"]).get("observations", [])
        msg = f"Subagent finished with status {child['status']} ({child['status_reason']})."
        src = self.core.sources.ingest_subagent_message(parent["project_id"], parent_run_id, child["run_id"], msg, child_obs[:20])
        self.journal.append(parent["project_id"], parent_run_id, "subagent.handoff",
                            {"child_run_id": child["run_id"], "source_id": src["source_id"], "origin": src["origin"]},
                            actor_type="coordinator", evidence_refs=child_obs[:20])
        return {"child": child, "handoff_source": src}

    # ================================================================ adapter state
    def export_adapter_state(self, branch_id: str) -> dict:
        b = self.core.versions.branch(branch_id)
        agent = self.agent(b["agent_id"])
        run = self.db.one("SELECT * FROM runs WHERE branch_id=? ORDER BY created_at DESC LIMIT 1", (branch_id,))
        if not run:
            cur = b["state"].get("restored_cursor") or {}
            return {"adapter": agent["adapter"], "run_id": None, "state": cur.get("state"), "operation_ids": cur.get("operation_ids", []), "messages": []}
        st = self.core.agent.get_state(run["thread_id"])
        st = {k: v for k, v in st.items() if k in ("observations", "progress", "pending", "action_id", "operation_id", "steps", "next_phase", "context_packet_id")}
        pkt = self.db.one("SELECT packet_ref FROM context_packets WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run["run_id"],))
        calls = self.db.all("SELECT call_id, output_json, packet_id FROM model_calls WHERE run_id=? ORDER BY created_at", (run["run_id"],))
        return {"adapter": agent["adapter"], "run_id": run["run_id"], "thread_id": run["thread_id"],
                "task_ref": f"{run['task_id']}@{run['task_revision']}", "state": st, "operation_ids": self._ops_for(run["run_id"]),
                "context_packet_ref": pkt["packet_ref"] if pkt else None,
                "messages": [{"call_id": c["call_id"], "packet_id": c["packet_id"], "output": loads(c["output_json"])} for c in calls]}

    def restore_adapter_state(self, branch: dict, cursor: dict, version_id: str) -> None:
        st = self.core.versions.branch(branch["branch_id"])["state"]
        st["restored_cursor"] = {"state": cursor.get("state"), "operation_ids": cursor.get("operation_ids", []),
                                 "run_id": cursor.get("run_id"), "version_id": version_id, "task_ref": cursor.get("task_ref")}
        self.core.versions.set_state(branch["branch_id"], st)

    # ================================================================ restart recovery
    def startup_recovery(self) -> dict:
        """After a service restart: in-flight effects become UNKNOWN (if the executor may have run)
        or return to RESERVED (if it never ran); active runs pause awaiting an explicit resume."""
        fixed = {"effects_unknown": 0, "effects_reserved": 0, "runs_paused": 0}
        for e in self.db.all("SELECT * FROM effects WHERE state='DISPATCHING'"):
            invoked = self.db.scalar("SELECT 1 FROM executor_invocations WHERE operation_id=? AND decision_id=?", (e["operation_id"], e["decision_id"]))
            new = "UNKNOWN" if invoked else "RESERVED"
            self.db.execute("UPDATE effects SET state=?, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE project_id=? AND operation_id=?",
                            (new, utcnow(), e["project_id"], e["operation_id"]))
            self.db.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                            (e["project_id"], e["operation_id"], new, dumps({"reason": "service restart"}), utcnow()))
            fixed["effects_unknown" if invoked else "effects_reserved"] += 1
        for r in self.db.all("SELECT run_id, status FROM runs WHERE status IN ('running','recovering')"):
            self.set_status(r["run_id"], "paused", "SERVICE_RESTART")
            fixed["runs_paused"] += 1
        self.db.execute("UPDATE branches SET barrier=0 WHERE barrier=1")
        return fixed


def _price(path: str, model_id: str) -> dict | None:
    import json
    from pathlib import Path
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cfg.get("models", {}).get(model_id)


def _canon(v):
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v
