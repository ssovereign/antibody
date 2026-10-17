"""Learning service: incident -> candidate -> fixed evaluation -> promotion,
revision and revocation.

Authority split: the learner proposes candidate records and requests tests;
it cannot certify its own result, edit the evaluator, or grant permissions.
Promotion is a short transaction with compare-and-swap on the registry head
and the target branch head; lesson.promoted is announced only after commit.
"""
from __future__ import annotations

import copy

from ..canonical import dumps, hash_value, loads, new_id, utcnow
from ..executors.files import ENV_EXPORT_FIXTURES
from ..projects import AuthError, ConflictError, InvalidRequest, NotFound
from .patterns import build_pattern_body

PERMISSION_FIELDS = {"tools", "recipient_ids", "file_roots", "spending", "grant_refs", "outbound_data_classes", "read_network",
                     "write_network", "policy", "checker", "suite", "hard_denied_tools"}


class LearningService:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.journal = core.journal

    # ------------------------------------------------------------ candidates
    def candidate(self, candidate_id: str, revision: int | None = None) -> dict:
        if revision is None:
            r = self.db.one("SELECT * FROM candidates WHERE candidate_id=? ORDER BY revision DESC LIMIT 1", (candidate_id,))
        else:
            r = self.db.one("SELECT * FROM candidates WHERE candidate_id=? AND revision=?", (candidate_id, revision))
        if not r:
            raise NotFound(f"candidate {candidate_id}")
        r["body"] = loads(r.pop("body_json"))
        r["evaluations"] = self.db.all("SELECT evaluation_id, candidate_revision, status, created_at FROM evaluations WHERE candidate_id=? ORDER BY created_at DESC", (candidate_id,))
        r["revisions"] = [x["revision"] for x in self.db.all("SELECT revision FROM candidates WHERE candidate_id=? ORDER BY revision", (candidate_id,))]
        return r

    def candidates(self, project_id: str, status: str | None = None) -> list[dict]:
        rows = self.db.all("SELECT candidate_id, MAX(revision) rev FROM candidates WHERE project_id=? GROUP BY candidate_id ORDER BY MAX(created_at) DESC", (project_id,))
        out = [self.candidate(r["candidate_id"], r["rev"]) for r in rows]
        return [c for c in out if not status or c["status"] == status]

    def create_candidate(self, project_id: str, kind: str, lesson_id: str, body: dict, created_by: str,
                         incident_id: str | None = None, parent_revision: int | None = None) -> dict:
        bad = PERMISSION_FIELDS & set(body.get("procedure", {}) or {})
        if bad or PERMISSION_FIELDS & set(body):
            raise InvalidRequest("AUTHORITY_FIELD", f"candidates cannot carry permission fields: {sorted(bad or (PERMISSION_FIELDS & set(body)))}")
        bh = hash_value(_canon(body))
        rejected = self.db.one("SELECT candidate_id, revision FROM candidates WHERE project_id=? AND body_hash=? AND status='Rejected'", (project_id, bh))
        if rejected:
            raise ConflictError("PREVIOUSLY_REJECTED", f"an identical candidate was already rejected ({rejected['candidate_id']}@{rejected['revision']})")
        cid = new_id("cand")
        now = utcnow()
        self.db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, 1, project_id, kind, "Draft", incident_id, lesson_id, parent_revision, dumps(body), bh, created_by, now, now))
        c = self.candidate(cid)
        run_id = self.db.scalar("SELECT run_id FROM incidents WHERE incident_id=?", (incident_id,)) if incident_id else None
        self.journal.append(project_id, run_id or f"learning:{project_id}", "candidate.created",
                            {"candidate_id": cid, "kind": kind, "lesson_id": lesson_id, "incident_id": incident_id,
                             "diagnosis": body.get("diagnosis"), "applicability": body.get("conditions") or body.get("family")},
                            actor_type="learner", evidence_refs=[f"incident:{incident_id}"] if incident_id else [])
        return c

    def revise_candidate(self, candidate_id: str, changes: dict, actor: str) -> dict:
        cur = self.candidate(candidate_id)
        body = copy.deepcopy(cur["body"])
        for k in ("conditions", "procedure", "negative_cases", "family", "channels", "exceptions"):
            if changes.get(k) is not None:
                body[k] = changes[k]
        if changes.get("note"):
            body.setdefault("revision_notes", []).append(changes["note"])
        if PERMISSION_FIELDS & set(body.get("procedure", {}) or {}):
            raise InvalidRequest("AUTHORITY_FIELD", "candidates cannot carry permission fields")
        rev = cur["revision"] + 1
        now = utcnow()
        self.db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (candidate_id, rev, cur["project_id"], cur["kind"], "Draft", cur["incident_id"], cur["lesson_id"],
                         cur["parent_revision"], dumps(body), hash_value(_canon(body)), actor, now, now))
        return self.candidate(candidate_id, rev)

    def as_test_lesson(self, cand: dict) -> dict:
        b = cand["body"]
        return {"ref": f"{cand['lesson_id']}@cand{cand['revision']}", "lesson_id": cand["lesson_id"], "revision": cand["revision"],
                "kind": cand["kind"], "body": b, "under_test": True, "evaluation_id": None}

    # ------------------------------------------------------------ incident -> candidate
    def on_incident(self, inc: dict) -> dict | None:
        if not self.core.config.AUTO_CANDIDATES:
            return None
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (inc["run_id"],))
        try:
            if inc["category"] == "execution_failure":
                cand = self._procedure_candidate(run, inc)
            elif inc["category"] == "blocked_action":
                cand = self._pattern_candidate(run, inc)
            else:
                cand = None
        except ConflictError:
            cand = None
        if cand:
            hyps = inc["hypotheses"] + [{"status": "hypothesized", "text": cand["body"].get("diagnosis", ""), "candidate_id": cand["candidate_id"]}]
            self.core.incidents.update(inc["incident_id"], candidate_id=cand["candidate_id"], hypotheses=hyps)
        return cand

    def _procedure_candidate(self, run: dict, inc: dict) -> dict | None:
        obs = inc["observed"][0]
        if obs.get("error_family") != "UnicodeEncodeError" or obs.get("tool_id") != "write_report":
            return None
        branch = self.core.versions.branch(run["branch_id"])
        env = branch["state"]["environment"]
        adapter = env.get("export_adapter", "fixture_1")
        default = ENV_EXPORT_FIXTURES.get(adapter, {}).get("default_encoding")
        lesson_id = f"lesson_export_encoding_{adapter}"
        existing = self.core.registry.active(run["project_id"])
        parent = next((l for l in existing if l["lesson_id"] == lesson_id), None)
        body = {
            "kind": "procedure", "family": "encoding",
            "scope": {"tool": "write_report", "project": run["project_id"]},
            "conditions": {"adapter": adapter, "default_encoding": default, "text_contains_nonencodable_characters": True},
            "procedure": {"encoding": "utf-8"},
            "diagnosis": f"The {adapter} export tool defaults to {default}, which cannot represent some report characters; setting UTF-8 explicitly is expected to fix the write.",
            "summary": f"Use UTF-8 for write_report on {adapter} when the text is not representable in {default}",
            "expected_improvement": "preserve Unicode report content",
            "negative_cases": ["ASCII-only reports keep the default", "other export adapters are unaffected"],
            "requested_evaluation": {"suite": "suite_core@1", "family": "encoding"},
            "source_incident_id": inc["incident_id"],
            "environment_signature": {"adapter": adapter, "adapter_version": env.get("adapter_version", "1")},
            "expires_on": ["adapter_version_change"],
            "evidence_refs": inc["evidence_refs"],
        }
        open_c = self.db.one("SELECT candidate_id FROM candidates WHERE project_id=? AND lesson_id=? AND status IN ('Draft','Testing') ORDER BY created_at DESC LIMIT 1",
                             (run["project_id"], lesson_id))
        if open_c:
            return self.candidate(open_c["candidate_id"])
        return self.create_candidate(run["project_id"], "procedure", lesson_id, body, "learner", inc["incident_id"],
                                     parent_revision=parent["revision"] if parent else None)

    def _pattern_candidate(self, run: dict, inc: dict) -> dict | None:
        anchors = [r for r in inc["evidence_refs"] if ":chunk_" in r]
        if not anchors:
            return None
        dec_id = next((r.split(":", 1)[1] for r in inc["evidence_refs"] if r.startswith("decision:")), None)
        d = self.db.one("SELECT canonical_args_ref FROM decisions WHERE decision_id=?", (dec_id,))
        args = self.core.objects.get_json(run["project_id"], d["canonical_args_ref"], restricted=True) if d else {}
        values = [str(v) for v in args.values() if isinstance(v, str) and len(v) >= 3]
        sid, _, idx = anchors[0].partition(":chunk_")
        ch = self.db.one("SELECT c.text, s.channel FROM chunks c JOIN sources s ON s.source_id=c.source_id WHERE c.source_id=? AND c.chunk_index=?", (sid, int(idx or 0)))
        if not ch:
            return None
        body = build_pattern_body(ch["text"], values, ch["channel"])
        if not body:
            return None
        body.update({"family_name": "injection", "family": dict(body["family"]), "source_incident_id": inc["incident_id"],
                     "evidence_refs": anchors, "diagnosis": f"A blocked {body['family']['target_tool_class']} action carried arguments found in external content: {body['tactic']}.",
                     "summary": body["safe_description"], "requested_evaluation": {"suite": "suite_core@1", "family": "injection"}})
        body["family_suite"] = "injection"
        lesson_id = f"pattern_{body['family']['requested_deviation']}"
        open_c = self.db.one("SELECT candidate_id FROM candidates WHERE project_id=? AND lesson_id=? AND status IN ('Draft','Testing') ORDER BY created_at DESC LIMIT 1",
                             (run["project_id"], lesson_id))
        if open_c:
            return self.candidate(open_c["candidate_id"])
        parent = next((l for l in self.core.registry.active(run["project_id"]) if l["lesson_id"] == lesson_id), None)
        return self.create_candidate(run["project_id"], "threat_pattern", lesson_id, _pattern_family_fix(body), "learner", inc["incident_id"],
                                     parent_revision=parent["revision"] if parent else None)

    # ------------------------------------------------------------ evaluation
    def evaluate(self, project_id: str, candidate_id: str, suite_ref: str | None, actor: str) -> dict:
        cand = self.candidate(candidate_id)
        if cand["project_id"] != project_id:
            raise NotFound(f"candidate {candidate_id}")
        if cand["status"] not in ("Draft", "Testing", "Rejected"):
            raise ConflictError("NOT_EVALUABLE", f"candidate is {cand['status']}")
        if cand["status"] == "Rejected":
            raise ConflictError("REJECTED", "revise the candidate (new revision) before re-evaluating")
        self._set_status(cand, "Testing")
        host = cand["body"].get("host") == "claude_code"
        ev = (self.core.cc_learning.evaluate(project_id, cand, actor) if host
              else self.core.evaluator.evaluate_candidate(project_id, cand, suite_ref, actor))
        if ev["status"] != "passed":
            self._set_status(cand, "Rejected")
        else:
            settings = self.core.projects.get_project(project_id)["settings"]
            cls = f"{cand['kind']}:{cand['body'].get('scope', {}).get('tool', '*')}"
            if (host and settings.get("claude_code_auto_accept", True)) or (settings.get("auto_promote_enabled") and (
                    cls in settings.get("auto_promote_classes", []) or cls in self.core.config.AUTO_PROMOTE_CLASSES)):
                try:
                    self.promote(project_id, candidate_id, None, None, None, "automatic", "auto_promotion")
                except ConflictError:
                    pass
        return ev

    def _set_status(self, cand: dict, status: str) -> None:
        self.db.execute("UPDATE candidates SET status=?, updated_at=? WHERE candidate_id=? AND revision=?",
                        (status, utcnow(), cand["candidate_id"], cand["revision"]))

    # ------------------------------------------------------------ promotion
    def promote(self, project_id: str, candidate_id: str, expected_registry: int | None, target_branch: str | None,
                expected_head: str | None, mode: str, actor: str) -> dict:
        cand = self.candidate(candidate_id)
        if cand["project_id"] != project_id:
            raise NotFound(f"candidate {candidate_id}")
        if cand["status"] != "Testing":
            raise ConflictError("NOT_PROMOTABLE", f"candidate is {cand['status']}; it needs a passing evaluation of its current revision")
        ev = self.db.one("SELECT * FROM evaluations WHERE candidate_id=? AND candidate_revision=? ORDER BY created_at DESC LIMIT 1",
                         (candidate_id, cand["revision"]))
        if not ev or ev["status"] != "passed":
            raise ConflictError("EVALUATION_REQUIRED", "no passing evaluation for this candidate revision")
        suite = self.core.evaluator.suite(f"{ev['suite_id']}@{ev['suite_revision']}")
        if ev["checker_hash"] != suite["checker_hash"]:
            raise ConflictError("CHECKER_CHANGED", "evaluation used a different checker")
        self._policy_compatible(project_id, cand)
        if self.core.registry.is_revoked(project_id, cand["lesson_id"]):
            raise ConflictError("REVOKED_LESSON", "this lesson id is globally revoked")
        agent_branch = target_branch
        if agent_branch is None:
            run_id = self.db.scalar("SELECT run_id FROM incidents WHERE incident_id=?", (cand["incident_id"],)) if cand["incident_id"] else None
            if run_id:
                agent_id = self.db.scalar("SELECT agent_id FROM runs WHERE run_id=?", (run_id,))
                agent_branch = self.db.scalar("SELECT accepted_branch_id FROM agents WHERE agent_id=?", (agent_id,))
        now = utcnow()
        with self.db.tx() as c:
            cur_reg = c.execute("SELECT registry_head FROM projects WHERE project_id=?", (project_id,)).fetchone()[0]
            exp = expected_registry if expected_registry is not None else cur_reg
            parent_rev = cand["parent_revision"]
            latest = c.execute("SELECT MAX(revision) FROM lessons WHERE project_id=? AND lesson_id=?", (project_id, cand["lesson_id"])).fetchone()[0]
            if latest is not None and parent_rev != latest:
                raise ConflictError("STALE_PARENT", f"lesson head is revision {latest}; reevaluate against the combined state")
            rev = (latest or 0) + 1
            changes = [{"op": "accept", "lesson_id": cand["lesson_id"], "revision": rev, "kind": cand["kind"]}]
            if latest:
                changes.append({"op": "supersede", "lesson_id": cand["lesson_id"], "revision": latest})
            reg_rev = self.core.registry.publish(c, project_id, exp, changes, f"publication_service:{actor}")
            body = dict(cand["body"], lesson_id=cand["lesson_id"], revision=rev, evaluation_id=ev["evaluation_id"],
                        parent_revision=latest, candidate_id=candidate_id)
            c.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (project_id, cand["lesson_id"], rev, cand["kind"], "accepted", cand["body"].get("scope", {}).get("tool"),
                       dumps(body), hash_value(_canon(body)), candidate_id, cand["revision"], ev["evaluation_id"], mode, reg_rev, actor, now))
            if latest:
                c.execute("UPDATE lessons SET status='superseded' WHERE project_id=? AND lesson_id=? AND revision=?", (project_id, cand["lesson_id"], latest))
                c.execute("UPDATE candidates SET status='Superseded' WHERE lesson_id=? AND status='Accepted'", (cand["lesson_id"],))
            c.execute("UPDATE candidates SET status='Accepted', updated_at=? WHERE candidate_id=? AND revision=?", (now, candidate_id, cand["revision"]))
            if cand["incident_id"]:
                c.execute("UPDATE incidents SET status='resolved' WHERE incident_id=? AND status IN ('open','recovered','recovering')", (cand["incident_id"],))
        ref = f"{cand['lesson_id']}@{rev}"
        version = None
        if agent_branch:
            b = self.core.versions.branch(agent_branch)
            st = b["state"]
            st["lesson_revisions"] = sorted([r for r in st["lesson_revisions"] if r.split("@")[0] != cand["lesson_id"]] + [ref])
            self.core.versions.set_state(agent_branch, st)
            version = self.core.versions.save(project_id, agent_branch, expected_head if expected_head is not None else b["head_version"] or "",
                                              f"accept {ref}", actor)
        self.journal.append(project_id, f"learning:{project_id}", "lesson.promoted",
                            {"lesson_ref": ref, "candidate_id": candidate_id, "evaluation_id": ev["evaluation_id"], "registry_revision": reg_rev,
                             "acceptance_mode": mode, "label": "assisted validation" if mode == "assisted" else "automatic acceptance",
                             "version_id": version["version_id"] if version else None},
                            actor_type="publication_service", registry_revision=reg_rev)
        return {"lesson": self.core.registry.lesson(project_id, cand["lesson_id"], rev), "registry_revision": reg_rev,
                "version": version, "acceptance_mode": mode}

    def _policy_compatible(self, project_id: str, cand: dict) -> None:
        b = cand["body"]
        if cand["kind"] == "procedure":
            tool = self.core.projects.tool(project_id, b.get("scope", {}).get("tool", ""))
            if not tool:
                raise ConflictError("POLICY_INCOMPATIBLE", "procedure scope names an unknown tool")
            for k, v in (b.get("procedure") or {}).items():
                if k not in tool["learnable_params"] or v not in tool["learnable_params"][k]:
                    raise ConflictError("POLICY_INCOMPATIBLE", f"{k}={v} is not an operator-declared learnable parameter")
        if PERMISSION_FIELDS & set(b.get("procedure", {}) or {}):
            raise ConflictError("POLICY_INCOMPATIBLE", "lessons cannot change permissions")

    # ------------------------------------------------------------ revocation
    def revoke(self, project_id: str, lesson_id: str, revision: int | None, reason: str, actor: str) -> dict:
        rows = self.db.all("SELECT revision FROM lessons WHERE project_id=? AND lesson_id=?", (project_id, lesson_id))
        if not rows:
            raise NotFound(f"lesson {lesson_id}")
        with self.db.tx() as c:
            cur = c.execute("SELECT registry_head FROM projects WHERE project_id=?", (project_id,)).fetchone()[0]
            reg = self.core.registry.publish(c, project_id, cur, [{"op": "revoke", "lesson_id": lesson_id, "revision": revision, "reason": reason}],
                                             f"publication_service:{actor}")
            c.execute("INSERT INTO revocations(project_id,lesson_id,revision,reason,actor,registry_revision,created_at) VALUES (?,?,?,?,?,?,?)",
                      (project_id, lesson_id, revision, reason, actor, reg, utcnow()))
            if revision is None:
                c.execute("UPDATE lessons SET status='revoked' WHERE project_id=? AND lesson_id=?", (project_id, lesson_id))
            else:
                c.execute("UPDATE lessons SET status='revoked' WHERE project_id=? AND lesson_id=? AND revision=?", (project_id, lesson_id, revision))
            c.execute("UPDATE candidates SET status='Revoked' WHERE lesson_id=? AND status='Accepted'", (lesson_id,))
        affected = self.db.all("SELECT DISTINCT run_id FROM lesson_uses WHERE project_id=? AND lesson_ref LIKE ?", (project_id, f"{lesson_id}@%"))
        self.journal.append(project_id, f"learning:{project_id}", "lesson.revoked",
                            {"lesson_id": lesson_id, "revision": revision, "reason": reason, "registry_revision": reg,
                             "runs_that_used_it": [a["run_id"] for a in affected]}, actor_type="publication_service", registry_revision=reg)
        return {"lesson_id": lesson_id, "revision": revision, "registry_revision": reg, "runs_that_used_it": [a["run_id"] for a in affected]}

    def disable_on_branch(self, branch_id: str, ref: str, disabled: bool = True) -> dict:
        """Branch-local removal: a new manifest on save; not a global revocation."""
        b = self.core.versions.branch(branch_id)
        st = b["state"]
        s = set(st.get("disabled_lessons", []))
        (s.add(ref) if disabled else s.discard(ref))
        st["disabled_lessons"] = sorted(s)
        if disabled:
            st["lesson_revisions"] = [r for r in st["lesson_revisions"] if r != ref]
        self.core.versions.set_state(branch_id, st)
        return st


def _pattern_family_fix(body: dict) -> dict:
    """Keep the suite family name separate from the tactic descriptor."""
    b = dict(body)
    b["family"] = dict(body["family"])
    b["suite_family"] = "injection"
    return b


def _canon(v):
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v
