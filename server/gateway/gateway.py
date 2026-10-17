"""Action gateway: ordered policy checks, bound decisions, durable effect
reservation and dispatch of exactly the checked arguments.

Sequence (Technical Design: Action execution sequence):
  1. Validate actor, run, branch, operation identity.
  2. Validate tool identity, schema and argument limits.
  3. Resolve resources with server-owned records.
  4. Enforce current task permissions.
  5. Enforce revocations and hard policy (grants).
  6. Enforce budgets and outstanding unknown effects.
  7. Collect action support (argument anchors in observed sources).
  8. Semantic audit for high-impact or untrusted-anchored actions.
  9. Persist the bound decision; for ALLOW reserve the effect (and consume a
     grant) in the same transaction, then dispatch the stored arguments.

Any exception while deciding a mutating action fails closed (PAUSE).
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from ..canonical import canonical_bytes, dumps, hash_value, loads, new_id, utcnow, utcnow_dt, parse_utc
from ..executors.base import ExecContext, ResourceViolation, ToolError, ToolOutcomeUnknown
from ..executors.fetch import check_url
from ..executors.files import resolve_in_workspace
from ..ingress.normalize import decode_b64_segments
from ..journal import redact
from ..projects import ConflictError
from ..schemas import REASON_CODES, ActionProposal, DecisionView
from ..storage.db import StorageUnavailable
from .auditor import AuditorFailure
from .jsonschema_lite import SchemaError, validate
from ..telemetry import log_event, traced

ANCHOR_ARGS = {"recipient_id", "artifact_path", "url", "payee_id", "path", "subject"}
SUSPICIOUS_QUERY = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


class Deny(Exception):
    def __init__(self, outcome: str, code: str, message: str | None = None, evidence: list | None = None, attribution: str = "observed"):
        super().__init__(code)
        self.outcome, self.code = outcome, code
        self.message = message or REASON_CODES.get(code, code)
        self.evidence = evidence or []
        self.attribution = attribution


def _exp(seconds: int) -> str:
    return (utcnow_dt() + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Gateway:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.journal = core.journal
        self.objects = core.objects
        self.projects = core.projects
        self.config = core.config
        self.auditor = core.auditor
        self.on_decision = []  # callbacks(run, decision_view)
        self.on_effect = []    # callbacks(run, effect_row, result)

    # ================================================================ submit
    @traced("gateway.submit")
    def submit(self, run_id: str, proposal: ActionProposal, worker: dict) -> DecisionView:
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if not run:
            raise ConflictError("RUN_NOT_FOUND", "run not found")
        if worker.get("role") not in ("worker", "admin"):
            raise ConflictError("WORKER_REQUIRED", "a trusted worker identity is required")
        if worker.get("role") == "worker" and worker.get("project_id") not in (None, run["project_id"]):
            raise ConflictError("WORKER_REQUIRED", "worker is not scoped to this project")
        pid = run["project_id"]
        canonical_args = proposal.arguments
        try:
            args_hash = hash_value(canonical_args)
        except Exception:
            args_hash = hash_value(json.loads(json.dumps(canonical_args, default=str)))
            canonical_args = json.loads(json.dumps(canonical_args, default=str))

        op = self.db.one("SELECT * FROM operations WHERE operation_id=? AND project_id=?", (proposal.operation_id, pid))
        owned = bool(op) and (op["run_id"] == run_id or self.db.one(
            "SELECT 1 FROM operation_bindings WHERE run_id=? AND operation_id=?", (run_id, proposal.operation_id)))
        if op and not owned:
            # Never reveal another run's effect through a guessed ID.
            raise ConflictError("OPERATION_CONFLICT", REASON_CODES["OPERATION_CONFLICT"])
        effect = self.db.one("SELECT * FROM effects WHERE project_id=? AND operation_id=?", (pid, proposal.operation_id))
        if effect:
            if effect["args_hash"] != args_hash:
                raise ConflictError("OPERATION_CONFLICT", REASON_CODES["OPERATION_CONFLICT"])
            if effect["state"] == "COMPLETED":
                return self._dedup_view(run, effect)
            if effect["state"] in ("UNKNOWN", "DISPATCHING") and effect["effect_class"] != "read":
                return self._record(run, proposal, canonical_args, args_hash,
                                    Deny("PAUSE", "OUTCOME_UNKNOWN", "The tool may have completed. Reconcile its status before retrying."),
                                    tool=self.projects.tool(pid, proposal.tool_id), existing_effect=True)
        try:
            tool = self.projects.tool(pid, proposal.tool_id)
            if not op:
                raise Deny("DENY", "OPERATION_NOT_ISSUED")
            if op["action_id"] != proposal.action_id and not effect:
                raise ConflictError("OPERATION_CONFLICT", "operation was issued for a different action")
            checks, evidence, audit, attribution, grant = self._decide(run, proposal, tool, canonical_args)
            view = self._record(run, proposal, canonical_args, args_hash, None, tool=tool, checks=checks,
                                evidence=evidence, audit=audit, attribution=attribution, grant=grant,
                                existing_effect=bool(effect))
        except Deny as d:
            view = self._record(run, proposal, canonical_args, args_hash, d, tool=self.projects.tool(pid, proposal.tool_id),
                                existing_effect=bool(effect))
        except (ConflictError,):
            raise
        except StorageUnavailable:
            raise
        except Exception as e:  # fail closed
            tb = traceback.format_exc(limit=3)
            view = self._record(run, proposal, canonical_args, args_hash,
                                Deny("PAUSE", "POLICY_ERROR", f"Policy evaluation failed ({type(e).__name__}); mutating actions fail closed."),
                                tool=None, existing_effect=bool(effect), audit={"error": tb[-800:]})
        if view.outcome == "ALLOW":
            view = self.dispatch(view.decision_id, worker.get("actor_id", "worker"))
        return view

    # ================================================================ decide
    def _decide(self, run: dict, p: ActionProposal, tool: dict | None, args: dict):
        pid = run["project_id"]
        checks: list[dict] = []
        ok = lambda name, detail="": checks.append({"check": name, "result": "pass", "detail": detail})  # noqa: E731

        # 1 actor / run / branch
        if run["status"] not in ("running",):
            raise Deny("PAUSE", "RUN_NOT_ACTIVE", f"Run status is {run['status']}; no new tools execute.")
        branch = self.db.one("SELECT * FROM branches WHERE branch_id=?", (run["branch_id"],))
        if branch["barrier"]:
            raise Deny("PAUSE", "BARRIER_ACTIVE")
        ok("run_active")

        # 2 tool identity and schema
        if tool is None:
            raise Deny("DENY", "TOOL_NOT_ALLOWED", "This tool is not in the operator tool registry.")
        if p.tool_schema_hash and p.tool_schema_hash != tool["schema_hash"]:
            raise Deny("DENY", "TOOL_SCHEMA_MISMATCH")
        policy = self.projects.policy(pid)
        if len(canonical_bytes(args)) > policy["max_argument_bytes"]:
            raise Deny("DENY", "SCHEMA_INVALID", "Arguments exceed the size limit.")
        try:
            validate(args, tool["schema"])
        except SchemaError as e:
            raise Deny("DENY", "SCHEMA_INVALID", f"Arguments do not match the tool schema: {e}")
        ok("schema", tool["schema_hash"])

        # 4 (first part) current task and policy revisions
        task = self.projects.get_task(pid, run["task_id"], run["task_revision"])
        if task["current_revision"] != run["task_revision"]:
            raise Deny("PAUSE", "STALE_REVISION", f"Task changed to revision {task['current_revision']}; resume under the new revision.")
        if policy["revision"] != run["policy_revision"]:
            raise Deny("PAUSE", "STALE_REVISION", f"Operator policy changed to {policy['revision']}.")
        body = task["body"]
        if tool["tool_id"] in policy.get("hard_denied_tools", []):
            raise Deny("DENY", "HARD_POLICY")
        if tool["tool_id"] not in body["tools"]:
            raise Deny("DENY", "TOOL_NOT_ALLOWED")
        ok("tool_in_task")

        # 3 + 4 resolve resources and enforce typed scope
        evidence_args: dict[str, str] = {}
        ec = tool["effect_class"]
        for key in ("path", "artifact_path"):
            if key in args:
                try:
                    resolve_in_workspace(branch["workspace_dir"], args[key], body["file_roots"])
                except ResourceViolation as e:
                    raise Deny("DENY", "PATH_OUTSIDE_WORKSPACE", e.message)
                evidence_args[key] = args[key]
        if any(k in args for k in ("path", "artifact_path")):
            ok("file_scope")
        if tool["tool_id"] == "read_source":
            src = self.db.one("SELECT project_id, run_id FROM sources WHERE source_id=?", (args["source_id"],))
            if not src or src["project_id"] != pid or (args["source_id"] not in body["resources"]["source_ids"] and src["run_id"] != run["run_id"]):
                raise Deny("DENY", "SCOPE_VIOLATION", "This source is not among the task's selected resources.")
            ok("source_scope", args["source_id"])
        dest_arg = tool.get("meta", {}).get("destination_arg") or ("recipient_id" if "recipient_id" in args else None)
        if ec == "external_send":
            if not dest_arg or dest_arg not in args:
                raise Deny("DENY", "DESTINATION_NOT_AUTHORIZED", "External sends need an operator-declared destination argument.")
            dest = str(args[dest_arg])
            rcpt = self.projects.recipient(pid, dest)
            if not rcpt:
                rcpt = self.db.one("SELECT * FROM recipients WHERE project_id=? AND lower(address)=lower(?)", (pid, dest))
            if not rcpt or rcpt["recipient_id"] not in body["recipient_ids"]:
                raise Deny("DENY", "DESTINATION_NOT_AUTHORIZED", "Blocked before execution. This recipient is outside the task authorization.",
                           evidence=self._anchors(run, {"recipient_id": dest}), attribution="hypothesized")
            evidence_args["recipient_id"] = rcpt["recipient_id"]
            ok("destination", rcpt["recipient_id"])
            if any(m in str(v) for v in args.values() for m in policy.get("canary_markers", [])):
                raise Deny("DENY", "DATA_CLASS_NOT_AUTHORIZED", "The outbound arguments contain sensitive canary content.")
            if "artifact_path" in args:
                self._check_outbound_artifact(run, branch, body, policy, args["artifact_path"])
                ok("data_class", "approved_report")
        if ec == "read_network":
            url = args.get("url", "")
            rn = body["read_network"]
            if not rn.get("public_https"):
                raise Deny("DENY", "NETWORK_NOT_AUTHORIZED")
            try:
                parts = urlsplit(url)
                if parts.scheme != "https":
                    raise ResourceViolation("NETWORK_NOT_AUTHORIZED", "only https URLs are permitted")
                if rn.get("allowed_hosts") and (parts.hostname or "").lower() not in [h.lower() for h in rn["allowed_hosts"]]:
                    raise ResourceViolation("NETWORK_NOT_AUTHORIZED", "host is not in the task's allowed hosts")
            except ResourceViolation as e:
                raise Deny("DENY", "NETWORK_NOT_AUTHORIZED", e.message)
            self._check_egress(run, url, policy)
            evidence_args["url"] = url
            ok("network_scope")
        if ec == "payment":
            payee = self.projects.recipient(pid, args["payee_id"])
            if not payee or payee["kind"] != "payee" or args["payee_id"] not in body["write_network"]["destination_ids"]:
                raise Deny("DENY", "DESTINATION_NOT_AUTHORIZED", "This payee is outside the task authorization.",
                           evidence=self._anchors(run, {"payee_id": args["payee_id"]}), attribution="hypothesized")
            try:
                amount = Decimal(args["amount"])
            except InvalidOperation:
                raise Deny("DENY", "SCHEMA_INVALID", "amount must be a decimal string")
            if amount <= 0 or args["currency"] != body["spending"]["currency"]:
                raise Deny("DENY", "SPENDING_LIMIT", "Amount or currency outside the authorized spending scope.")
            spent = self._spent(run)
            if spent + amount > Decimal(body["spending"]["limit"]):
                raise Deny("DENY", "SPENDING_LIMIT", f"Spending {spent + amount} would exceed the limit {body['spending']['limit']}.")
            evidence_args["payee_id"] = args["payee_id"]
            ok("spending", f"{spent + amount} <= {body['spending']['limit']}")

        # 5 revocations, hard policy, grants
        for ref in p.observation_refs:
            if ref.startswith("lesson:"):
                lid, rev = ref[len("lesson:"):].split("@")
                if self.core.registry.is_revoked(pid, lid, int(rev) if rev.isdigit() else None):
                    raise Deny("DENY", "LESSON_REVOKED", f"Lesson {lid}@{rev} was revoked; the action must be regenerated.")
        grant = None
        if ec in policy.get("grant_required_classes", []):
            grant = self._find_grant(pid, body, tool["tool_id"], args)
            if not grant:
                raise Deny("PAUSE", "GRANT_REQUIRED",
                           f"{tool['tool_id']} is high impact and needs a grant referenced by the task (missing scope: {ec}).")
            ok("grant", grant["grant_id"])

        # 6 budgets and outstanding unknown effects
        max_steps = min(body.get("max_tool_steps", self.config.MAX_TOOL_STEPS), loads(run["start_config_json"])["limits"]["max_tool_steps"])
        decided = self.db.scalar("SELECT COUNT(*) FROM decisions WHERE run_id=?", (run["run_id"],))
        if decided >= max_steps:
            raise Deny("PAUSE", "BUDGET_EXCEEDED", f"The run used its {max_steps} tool steps.")
        if ec in policy.get("mutating_classes", []):
            unk = self.db.scalar("SELECT COUNT(*) FROM effects WHERE run_id=? AND state='UNKNOWN'", (run["run_id"],))
            if unk:
                raise Deny("PAUSE", "OUTCOME_UNKNOWN", "A previous effect has an unknown outcome. Reconcile it before dependent actions.")
        ok("budget", f"{decided}/{max_steps}")

        # 7 support evidence
        anchors = self._anchors(run, evidence_args)
        evidence = [a["ref"] for a in anchors]
        attribution = "hypothesized" if any(a["origin"] == "EXTERNAL_DATA" for a in anchors) else "observed"

        # 8 semantic audit
        audit = None
        untrusted_anchor = any(a["origin"] in ("EXTERNAL_DATA", "AGENT_DERIVED") for a in anchors)
        needs_audit = policy["audit"]["enabled"] and (ec in policy["high_impact_classes"] or
                                                      (policy["audit"]["audit_untrusted_anchors"] and untrusted_anchor))
        if needs_audit:
            assignment = {"goal": body["goal"], "tools": body["tools"], "recipient_ids": body["recipient_ids"],
                          "file_roots": body["file_roots"]}
            action = {"tool_id": tool["tool_id"], "effect_class": ec, "arguments": redact(args)}
            try:
                out = self.core.auditor.run(assignment, action, anchors)
            except AuditorFailure as e:
                fallback = policy.get("read_only_safe_fallback") and ec in ("read", "read_network")
                if fallback:
                    audit = {"auditor": self.core.auditor.name, "error": str(e), "fallback": "read_only_safe_fallback"}
                    ok("audit", "auditor failed; explicit read-only safe fallback")
                    return checks, evidence, audit, attribution, grant
                raise Deny("PAUSE", "AUDIT_FAILED", f"{e}. Mutating actions fail closed.", evidence=evidence)
            audit = {"auditor": self.core.auditor.name, **out.model_dump()}
            if out.outcome == "uncertain":
                raise Deny("PAUSE", "AUDIT_UNCERTAIN", out.note or None, evidence=out.evidence_refs, attribution="hypothesized")
            if out.outcome == "deny":
                code = "KNOWN_PATTERN" if out.reason_code == "KNOWN_PATTERN" else "UNSUPPORTED_ACTION"
                raise Deny("DENY", code, out.note or None, evidence=out.evidence_refs, attribution="hypothesized")
            ok("audit", out.reason_code)
        return checks, evidence, audit, attribution, grant

    def _check_outbound_artifact(self, run, branch, body, policy, path: str) -> None:
        art = self.db.one("SELECT * FROM artifacts WHERE project_id=? AND branch_id=? AND path=?", (run["project_id"], branch["branch_id"], path))
        if not art or art["check_status"] != "passed":
            raise Deny("DENY", "DATA_CLASS_NOT_AUTHORIZED", "The artifact has not passed its verification checks, so it is not approved for sharing.")
        if art["data_class"] not in body["outbound_data_classes"]:
            raise Deny("DENY", "DATA_CLASS_NOT_AUTHORIZED", f"Data class {art['data_class']} is not authorized for outbound sharing.")
        try:
            p = resolve_in_workspace(branch["workspace_dir"], path, body["file_roots"], must_exist=True)
        except (ResourceViolation, ToolError) as e:
            raise Deny("DENY", "DATA_CLASS_NOT_AUTHORIZED", getattr(e, "message", str(e)))
        data = p.read_bytes()
        from ..canonical import content_ref
        if content_ref(data) != art["content_hash"]:
            raise Deny("DENY", "DATA_CLASS_NOT_AUTHORIZED", "The artifact changed after it was checked.")
        text = data.decode("utf-8", "ignore")
        if any(m in text for m in policy.get("canary_markers", [])):
            raise Deny("DENY", "DATA_CLASS_NOT_AUTHORIZED", "The artifact contains sensitive canary content.")

    def _check_egress(self, run, url: str, policy) -> None:
        parts = urlsplit(url)
        blob = (parts.query or "") + " " + (parts.path or "") + " " + (parts.fragment or "")
        markers = policy.get("canary_markers", [])
        decoded = " ".join(decode_b64_segments(blob.replace("-", "+").replace("_", "/")))
        if any(m in blob or m in decoded for m in markers):
            raise Deny("DENY", "SCOPE_VIOLATION", "The URL carries sensitive canary data (possible data egress).")
        if SUSPICIOUS_QUERY.search(parts.query or ""):
            raise Deny("PAUSE", "SCOPE_VIOLATION", "The URL query carries a long encoded value; possible data egress needs review.")

    def _spent(self, run) -> Decimal:
        total = Decimal("0")
        for e in self.db.all("SELECT decision_id FROM effects WHERE run_id=? AND effect_class='payment' AND state IN ('COMPLETED','DISPATCHING','UNKNOWN','RESERVED')", (run["run_id"],)):
            d = self.db.one("SELECT canonical_args_ref, project_id FROM decisions WHERE decision_id=?", (e["decision_id"],))
            a = self.objects.get_json(d["project_id"], d["canonical_args_ref"], restricted=True)
            total += Decimal(a["amount"])
        return total

    def _find_grant(self, pid: str, body: dict, tool_id: str, args: dict) -> dict | None:
        now = utcnow()
        for gid in body.get("grant_refs", []):
            g = self.db.one("SELECT * FROM grants WHERE grant_id=? AND project_id=? AND tool_id=?", (gid, pid, tool_id))
            if not g or g["remaining_uses"] <= 0 or g["expires_at"] <= now:
                continue
            if g["args_hash"] and g["args_hash"] == hash_value(args):
                return g
            if g["operation_class"]:
                scope = loads(g["resource_scope_json"], {})
                prefix = scope.get("path_prefix")
                if prefix and str(args.get("path", "")).startswith(prefix):
                    return g
                if not prefix and g["operation_class"] == tool_id:
                    return g
        return None

    def _anchors(self, run: dict, arg_values: dict) -> list[dict]:
        """Argument anchors: places where an argument value appears in content this run observed."""
        out = []
        values = {k: str(v) for k, v in arg_values.items() if isinstance(v, (str, int)) and len(str(v)) >= 3}
        if not values:
            return out
        if "recipient_id" in values:
            r = self.projects.recipient(run["project_id"], values["recipient_id"])
            if r:
                values["recipient_address"] = r["address"]
        refs = self.db.all("SELECT DISTINCT src_ref FROM edges WHERE run_id=? AND kind='observation'", (run["run_id"],))
        for row in refs:
            sid, _, cidx = row["src_ref"].partition(":chunk_")
            ch = self.db.one("SELECT c.text, c.flags_json, s.origin FROM chunks c JOIN sources s ON s.source_id=c.source_id WHERE c.source_id=? AND c.chunk_index=?", (sid, int(cidx or 0)))
            if not ch:
                continue
            lower = ch["text"].lower()
            for k, v in values.items():
                i = lower.find(v.lower())
                if i >= 0:
                    quarantined = flagged = False
                    for disp in self.db.all("SELECT disposition, detail_json FROM dispositions WHERE run_id=? AND source_id=? AND (chunk_index=? OR chunk_index IS NULL)",
                                            (run["run_id"], sid, int(cidx or 0))):
                        spans = loads(disp["detail_json"], {}).get("spans") or [[0, len(ch["text"])]]
                        if any(a <= i < b for a, b in spans):
                            quarantined |= disp["disposition"] == "quarantined"
                            flagged |= disp["disposition"] == "flagged"
                    flags = loads(ch["flags_json"], {})
                    in_quote = any(a <= i < b for a, b in flags.get("quotation_spans", []))
                    # minimal span: the sentence/line holding the anchor
                    ls = ch["text"].rfind("\n", 0, i) + 1
                    le = ch["text"].find("\n", i)
                    le = len(ch["text"]) if le < 0 else le
                    out.append({"ref": row["src_ref"], "arg": k, "origin": ch["origin"], "offset": i,
                                "window": ch["text"][max(ls, i - 220): min(le, i + len(v) + 160)],
                                "quarantined": quarantined, "flagged": flagged, "in_quotation": in_quote})
        return out

    # ================================================================ record
    def _record(self, run, p: ActionProposal, args, args_hash, deny: Deny | None, *, tool, checks=None, evidence=None,
                audit=None, attribution="observed", grant=None, existing_effect=False) -> DecisionView:
        pid = run["project_id"]
        policy_rev = run["policy_revision"]
        try:
            policy_rev = self.projects.get_project(pid)["policy_head"]
        except Exception:
            pass
        outcome = deny.outcome if deny else "ALLOW"
        code = deny.code if deny else "OK"
        message = deny.message if deny else REASON_CODES["OK"]
        evidence = (deny.evidence if deny else evidence) or []
        if deny and deny.evidence and isinstance(deny.evidence[0], dict):
            evidence = [a["ref"] for a in deny.evidence]
        attribution = deny.attribution if deny else attribution
        args_ref = self.objects.put_json(pid, args, restricted=True)
        decision_id = new_id("decision")
        now = utcnow()
        ttl = 120
        registry_rev = run["registry_revision"]
        with self.db.tx() as c:
            exists = c.execute("SELECT 1 FROM actions WHERE action_id=?", (p.action_id,)).fetchone()
            action_id = p.action_id if not exists else new_id("action")
            c.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (action_id, pid, run["run_id"], p.tool_id, (tool or {}).get("schema_hash", "unknown"), args_ref,
                       dumps(redact(args)), args_hash, p.operation_id, dumps(p.observation_refs), now))
            c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (decision_id, pid, action_id, run["run_id"], p.operation_id, p.tool_id, args_hash, args_ref, outcome, code, message,
                       policy_rev, run["task_revision"], registry_rev, _exp(ttl), dumps(evidence), dumps(checks or []),
                       dumps(audit) if audit else None, attribution, grant["grant_id"] if grant else None, now))
            ec = (tool or {}).get("effect_class", "read")
            if outcome == "ALLOW":
                if grant:
                    upd = c.execute("UPDATE grants SET remaining_uses=remaining_uses-1 WHERE grant_id=? AND remaining_uses>0", (grant["grant_id"],))
                    if upd.rowcount != 1:
                        raise ConflictError("GRANT_EXHAUSTED", "grant was consumed concurrently")
                jseq = c.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                                 (pid, p.operation_id, "RESERVED", dumps({"decision_id": decision_id}), now)).lastrowid
                if existing_effect:
                    c.execute("UPDATE effects SET decision_id=?, state='RESERVED', lease_owner=NULL, lease_expires=NULL, error_json=NULL, journal_seq=?, updated_at=? WHERE project_id=? AND operation_id=?",
                              (decision_id, jseq, now, pid, p.operation_id))
                else:
                    c.execute("INSERT INTO effects(project_id,operation_id,decision_id,run_id,branch_id,tool_id,effect_class,args_hash,state,journal_seq,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (pid, p.operation_id, decision_id, run["run_id"], run["branch_id"], p.tool_id, ec, args_hash, "RESERVED", jseq, now, now))
            elif outcome == "DENY" and not existing_effect and self.db.scalar("SELECT 1 FROM operations WHERE operation_id=?", (p.operation_id,)):
                jseq = c.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                                 (pid, p.operation_id, "DENIED", dumps({"decision_id": decision_id, "reason_code": code}), now)).lastrowid
                c.execute("INSERT INTO effects(project_id,operation_id,decision_id,run_id,branch_id,tool_id,effect_class,args_hash,state,journal_seq,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (pid, p.operation_id, decision_id, run["run_id"], run["branch_id"], p.tool_id, ec, args_hash, "DENIED", jseq, now, now))
            elif outcome == "DENY" and existing_effect:
                c.execute("UPDATE effects SET decision_id=?, state=CASE WHEN state IN ('FAILED','DENIED') THEN 'DENIED' ELSE state END, updated_at=? WHERE project_id=? AND operation_id=?",
                          (decision_id, now, pid, p.operation_id))
            for ref in evidence:
                c.execute("INSERT INTO edges(project_id,run_id,src_ref,dst_ref,kind,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
                          (pid, run["run_id"], ref, f"action:{action_id}", "argument_anchor", dumps({"decision_id": decision_id}), now))
        self.journal.append(pid, run["run_id"], "action.proposed",
                            {"action_id": action_id, "tool_id": p.tool_id, "arguments": redact(args), "operation_id": p.operation_id,
                             "observation_refs": p.observation_refs[:50]}, actor_type="agent",
                            policy_revision=policy_rev, registry_revision=registry_rev)
        view = DecisionView(decision_id=decision_id, action_id=action_id, operation_id=p.operation_id, outcome=outcome,
                            reason_code=code, message=message, arguments_hash=args_hash, policy_revision=policy_rev,
                            task_revision=run["task_revision"], expires_at=_exp(ttl), evidence_refs=evidence,
                            executor_invoked=False, attribution_status=attribution)
        self.journal.append(pid, run["run_id"], "action.decided",
                            {"decision_id": decision_id, "action_id": action_id, "tool_id": p.tool_id, "outcome": outcome,
                             "reason_code": code, "message": message, "checks": checks or [], "audit": audit,
                             "attribution_status": attribution, "executor_invoked": False},
                            actor_type="gateway", evidence_refs=evidence, policy_revision=policy_rev, registry_revision=registry_rev)
        log_event("action.decided", run_id=run["run_id"], decision_id=decision_id, tool_id=p.tool_id, outcome=outcome, reason_code=code)
        for cb in self.on_decision:
            cb(run, view)
        return view

    def _dedup_view(self, run, effect) -> DecisionView:
        d = self.db.one("SELECT * FROM decisions WHERE decision_id=?", (effect["decision_id"],))
        result = self.objects.get_json(run["project_id"], effect["result_ref"]) if effect["result_ref"] else None
        self.journal.append(run["project_id"], run["run_id"], "action.decided",
                            {"decision_id": d["decision_id"], "operation_id": effect["operation_id"], "outcome": "ALLOW",
                             "reason_code": "DEDUPLICATED", "message": REASON_CODES["DEDUPLICATED"], "executor_invoked": False,
                             "tool_id": effect["tool_id"]}, actor_type="gateway", policy_revision=d["policy_revision"])
        return DecisionView(decision_id=d["decision_id"], action_id=d["action_id"], operation_id=effect["operation_id"],
                            outcome="ALLOW", reason_code="DEDUPLICATED", message=REASON_CODES["DEDUPLICATED"],
                            arguments_hash=d["args_hash"], policy_revision=d["policy_revision"], task_revision=d["task_revision"],
                            expires_at=d["expires_at"], evidence_refs=loads(d["evidence_refs_json"], []), executor_invoked=False,
                            result=result, effect_state="COMPLETED", deduplicated=True)

    # ================================================================ dispatch
    @traced("gateway.dispatch")
    def dispatch(self, decision_id: str, worker_id: str) -> DecisionView:
        d = self.db.one("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        pid = d["project_id"]
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (d["run_id"],))
        now = utcnow()
        lease = (utcnow_dt() + timedelta(seconds=self.config.EFFECT_LEASE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        base_view = dict(decision_id=d["decision_id"], action_id=d["action_id"], operation_id=d["operation_id"],
                         arguments_hash=d["args_hash"], policy_revision=d["policy_revision"], task_revision=d["task_revision"],
                         expires_at=d["expires_at"], evidence_refs=loads(d["evidence_refs_json"], []),
                         attribution_status=d["attribution_status"])
        # atomic claim + recheck of current revisions
        with self.db.tx() as c:
            eff = c.execute("SELECT * FROM effects WHERE project_id=? AND operation_id=? AND decision_id=?", (pid, d["operation_id"], decision_id)).fetchone()
            if not eff or eff["state"] != "RESERVED":
                raise ConflictError("NOT_RESERVED", "decision has no reserved effect to dispatch")
            stale = None
            if d["expires_at"] <= now:
                stale = "decision expired"
            cur_task = c.execute("SELECT current_revision FROM task_heads WHERE project_id=? AND task_id=?", (pid, run["task_id"])).fetchone()[0]
            cur_pol = c.execute("SELECT policy_head FROM projects WHERE project_id=?", (pid,)).fetchone()[0]
            if cur_task != d["task_revision"] or cur_pol != d["policy_revision"]:
                stale = "task or policy changed after decision"
            if c.execute("SELECT barrier FROM branches WHERE branch_id=?", (run["branch_id"],)).fetchone()[0]:
                stale = "branch barrier active"
            if stale:
                c.execute("UPDATE effects SET state='FAILED', error_json=?, updated_at=? WHERE project_id=? AND operation_id=?",
                          (dumps({"code": "STALE_REVISION", "message": stale}), now, pid, d["operation_id"]))
                c.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                          (pid, d["operation_id"], "FAILED", dumps({"stale": stale}), now))
            else:
                claim = eff["claim_revision"] + 1
                c.execute("UPDATE effects SET state='DISPATCHING', lease_owner=?, lease_expires=?, claim_revision=?, attempts=attempts+1, updated_at=? WHERE project_id=? AND operation_id=? AND claim_revision=?",
                          (worker_id, lease, claim, now, pid, d["operation_id"], eff["claim_revision"]))
                c.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                          (pid, d["operation_id"], "DISPATCHING", dumps({"worker": worker_id}), now))
        if stale:
            return DecisionView(**base_view, outcome="PAUSE", reason_code="STALE_REVISION", message=f"Recheck failed: {stale}.",
                                executor_invoked=False, effect_state="FAILED")
        self.journal.append(pid, run["run_id"], "effect.started", {"operation_id": d["operation_id"], "decision_id": decision_id,
                                                                   "tool_id": d["tool_id"]}, actor_type="executor")
        # dispatch exactly the stored canonical arguments
        args = self.objects.get_json(pid, d["canonical_args_ref"], restricted=True)
        if hash_value(args) != d["args_hash"]:
            raise RuntimeError("stored arguments failed verification")
        self.db.execute("INSERT INTO executor_invocations(project_id,decision_id,operation_id,tool_id,args_hash,worker_id,invoked_at) VALUES (?,?,?,?,?,?,?)",
                        (pid, decision_id, d["operation_id"], d["tool_id"], d["args_hash"], worker_id, utcnow()))
        branch = self.db.one("SELECT * FROM branches WHERE branch_id=?", (run["branch_id"],))
        task = self.projects.get_task(pid, run["task_id"], run["task_revision"])
        ctx = ExecContext(project_id=pid, run_id=run["run_id"], branch_id=branch["branch_id"], workspace_dir=branch["workspace_dir"],
                          environment=loads(branch["state_json"])["environment"], operation_id=d["operation_id"],
                          decision_id=decision_id, file_roots=task["body"]["file_roots"], task=task["body"])
        state, result, error = "COMPLETED", None, None
        try:
            result = self.core.executors.run(d["tool_id"], args, ctx)
        except ToolError as e:
            state, error = "FAILED", {"code": e.code, "family": e.family, "message": e.message, "detail": e.detail}
        except ResourceViolation as e:
            state, error = "FAILED", {"code": e.code, "family": "ResourceViolation", "message": e.message}
        except ToolOutcomeUnknown as e:
            state, error = "UNKNOWN", {"code": "OUTCOME_UNKNOWN", "family": "Timeout", "message": str(e)}
        except Exception as e:
            tool = self.projects.tool(pid, d["tool_id"])
            if tool and tool["effect_class"] == "read":
                state, error = "FAILED", {"code": "E_EXECUTOR", "family": type(e).__name__, "message": str(e)[:300]}
            else:
                state, error = "UNKNOWN", {"code": "OUTCOME_UNKNOWN", "family": type(e).__name__, "message": str(e)[:300]}
        body_bytes = None
        if result and isinstance(result.get("body"), (bytes, bytearray)):
            body_bytes = result.pop("body")
        result_ref = self.objects.put_json(pid, result) if result is not None else None
        ext = (result or {}).get("outbox_entry") or (result or {}).get("ledger_entry") or (result or {}).get("sha256")
        done = utcnow()
        with self.db.tx() as c:
            upd = c.execute("UPDATE effects SET state=?, result_ref=?, external_ref=?, error_json=?, lease_owner=NULL, lease_expires=NULL, updated_at=? WHERE project_id=? AND operation_id=? AND state='DISPATCHING' AND lease_owner=?",
                            (state, result_ref, ext, dumps(error) if error else None, done, pid, d["operation_id"], worker_id))
            if upd.rowcount != 1:
                raise ConflictError("LEASE_LOST", "effect lease was lost before completion")
            c.execute("INSERT INTO effect_journal(project_id,operation_id,state,detail_json,created_at) VALUES (?,?,?,?,?)",
                      (pid, d["operation_id"], state, dumps({"error": error} if error else {"external_ref": ext}), done))
            if state == "COMPLETED" and d["tool_id"] == "write_report":
                c.execute("INSERT OR REPLACE INTO artifacts(project_id,branch_id,path,content_hash,run_id,operation_id,data_class,check_status,check_detail_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (pid, run["branch_id"], args["path"], result["sha256"], run["run_id"], d["operation_id"], "unapproved", "unchecked", "{}", done))
        kind = {"COMPLETED": "effect.completed", "FAILED": "effect.failed", "UNKNOWN": "effect.unknown"}[state]
        self.journal.append(pid, run["run_id"], kind, {"operation_id": d["operation_id"], "tool_id": d["tool_id"], "state": state,
                                                       "result": redact(result) if result else None, "error": error},
                            actor_type="executor", evidence_refs=[f"decision:{decision_id}"])
        log_event("effect." + state.lower(), run_id=run["run_id"], operation_id=d["operation_id"], tool_id=d["tool_id"], error_code=(error or {}).get("code"))
        eff_row = self.db.one("SELECT * FROM effects WHERE project_id=? AND operation_id=?", (pid, d["operation_id"]))
        if body_bytes is not None:
            result = dict(result, body_bytes=body_bytes)
        for cb in self.on_effect:
            cb(run, eff_row, result, error)
        out_result = {k: v for k, v in (result or {}).items() if k != "body_bytes"} if result else None
        return DecisionView(**base_view, outcome="ALLOW", reason_code="OK", message=REASON_CODES["OK"],
                            executor_invoked=True, effect_state=state, result=out_result if state == "COMPLETED" else {"error": error, **(out_result or {})})
