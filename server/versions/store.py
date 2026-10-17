"""RECEIPT version store: manifests, atomic saves, isolated forks, restore
previews, structured comparison and garbage collection.

version_id = sha256 over the canonical manifest without the version_id field
(see canonical.py for the documented serializer). Labels and timestamps live
outside the hashed manifest.
"""
from __future__ import annotations

import copy
import difflib
import os
import platform
import shutil
import sys
import time
from pathlib import Path

from ..canonical import canonical_bytes, content_ref, dumps, hash_value, loads, new_id, utcnow
from ..projects import ConflictError, InvalidRequest, NotFound

MANIFEST_SCHEMA = 1
SUPPORTED_ADAPTERS = {"langgraph_reference", "mcp_proxy"}


def default_branch_state(agent: dict) -> dict:
    cfg = loads(agent["config_json"]) if isinstance(agent.get("config_json"), str) else agent.get("config", {})
    return {
        "schema_version": 1,
        "instructions": cfg.get("instructions", ""),
        "model": cfg.get("model", {"provider": "deterministic", "model_id": "reference-planner-1", "temperature": "0"}),
        "environment": dict({"export_adapter": "fixture_1", "adapter_version": "1"}, **cfg.get("environment", {})),
        "lesson_revisions": [],
        "disabled_lessons": [],
        "memory_facts": {},
        "context_refs": [],
    }


class VersionStore:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.objects = core.objects
        self.journal = core.journal
        self.config = core.config
        self.fail_head_cas = False  # crash-point test hook

    # ================================================================ branches
    def create_branch(self, project_id: str, agent_id: str, name: str, state: dict, parent_branch_id: str | None = None,
                      base_version: str | None = None, purpose: str = "work") -> dict:
        bid = new_id("branch")
        ws = Path(self.config.WORKSPACE_ROOT) / project_id / bid
        ws.mkdir(parents=True, exist_ok=False)
        if self.db.one("SELECT 1 FROM branches WHERE agent_id=? AND name=?", (agent_id, name)):
            name = f"{name}-{bid[-6:]}"
        self.db.execute("INSERT INTO branches(branch_id,project_id,agent_id,name,head_version,parent_branch_id,base_version,workspace_dir,thread_ns,state_json,barrier,status,purpose,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,'active',?,?)",
                        (bid, project_id, agent_id, name, base_version, parent_branch_id, base_version, str(ws), f"ns_{bid}",
                         dumps(state), purpose, utcnow()))
        return self.branch(bid)

    def branch(self, branch_id: str) -> dict:
        b = self.db.one("SELECT * FROM branches WHERE branch_id=?", (branch_id,))
        if not b:
            raise NotFound(f"branch {branch_id}")
        b["state"] = loads(b.pop("state_json"))
        return b

    def branches(self, project_id: str, agent_id: str | None = None) -> list[dict]:
        q = "SELECT branch_id FROM branches WHERE project_id=?" + (" AND agent_id=?" if agent_id else "") + " ORDER BY created_at"
        return [self.branch(r["branch_id"]) for r in self.db.all(q, (project_id, agent_id) if agent_id else (project_id,))]

    def set_state(self, branch_id: str, state: dict) -> None:
        self.db.execute("UPDATE branches SET state_json=? WHERE branch_id=?", (dumps(state), branch_id))

    def edit_memory(self, branch_id: str, key: str, value=None, delete: bool = False, note: str | None = None,
                    seeded: bool = False, origin: str = "USER_ASSIGNMENT", source_refs: list | None = None) -> dict:
        b = self.branch(branch_id)
        st = b["state"]
        if delete:
            st["memory_facts"].pop(key, None)
        else:
            st["memory_facts"][key] = {"value": value, "origin": origin, "note": note or "", "source_refs": source_refs or [],
                                       "seeded_test_condition": seeded}
        self.set_state(branch_id, st)
        return st

    # ================================================================ save
    def save(self, project_id: str, branch_id: str, expected_head: str | None, label: str | None, actor: str,
             barrier_deadline: float = 5.0, parents: list[str] | None = None) -> dict:
        b = self.branch(branch_id)
        if b["project_id"] != project_id:
            raise NotFound(f"branch {branch_id}")
        upd = self.db.execute("UPDATE branches SET barrier=1 WHERE branch_id=? AND barrier=0", (branch_id,))
        if upd.rowcount != 1:
            raise ConflictError("BARRIER_ACTIVE", "another save or restore is in progress on this branch")
        try:
            # 1 wait for in-flight work
            t0 = time.time()
            while self.db.scalar("SELECT COUNT(*) FROM effects WHERE branch_id=? AND state='DISPATCHING'", (branch_id,)) and time.time() - t0 < barrier_deadline:
                time.sleep(0.05)
            # 2 reconcile in-flight mutating actions
            for e in self.db.all("SELECT * FROM effects WHERE branch_id=? AND state IN ('UNKNOWN','DISPATCHING')", (branch_id,)):
                try:
                    self.core.effects.reconcile(project_id, e["operation_id"], actor="save_barrier")
                except Exception:
                    pass
            unresolved = self.db.scalar("SELECT COUNT(*) FROM effects WHERE branch_id=? AND state IN ('UNKNOWN','DISPATCHING')", (branch_id,))
            # 3 consistent adapter cursor + journal high-water mark
            b = self.branch(branch_id)
            if expected_head is not None and expected_head != (b["head_version"] or ""):
                raise ConflictError("STALE_HEAD", f"branch head is {b['head_version']}")
            cursor = self.core.coordinator.export_adapter_state(branch_id)
            hwm = self.db.scalar("SELECT COALESCE(MAX(seq),0) FROM effect_journal WHERE project_id=?", (project_id,))
            agent = self.db.one("SELECT * FROM agents WHERE agent_id=?", (b["agent_id"],))
            st = b["state"]
            tree = self.snapshot_tree(project_id, b["workspace_dir"])
            # 4 immutable objects (temp file, fsync, verify, rename)
            agent_cfg = {"agent_id": agent["agent_id"], "name": agent["name"], "adapter": agent["adapter"],
                         "instructions": st.get("instructions", ""), "model": st.get("model", {})}
            messages = cursor.pop("messages", [])
            last_run = cursor.get("run_id")
            run = self.db.one("SELECT * FROM runs WHERE run_id=?", (last_run,)) if last_run else None
            manifest = {
                "schema_version": MANIFEST_SCHEMA,
                "parent_ids": parents if parents is not None else ([b["head_version"]] if b["head_version"] else []),
                "agent_config_ref": self.objects.put_json(project_id, agent_cfg),
                "adapter_state_ref": self.objects.put_json(project_id, _canon(cursor)),
                "messages_ref": self.objects.put_json(project_id, _canon(messages)),
                "memory_ref": self.objects.put_json(project_id, _canon(st.get("memory_facts", {}))),
                "lesson_revisions": sorted(st.get("lesson_revisions", [])),
                "disabled_lessons": sorted(st.get("disabled_lessons", [])),
                "context_packet_ref": cursor.get("context_packet_ref") or "",
                "artifact_tree_ref": self.objects.put_json(project_id, tree),
                "environment_ref": self.objects.put_json(project_id, _canon(dict(st.get("environment", {}), os=platform.system(),
                                                                                 python=sys.version.split()[0]))),
                "task_revision_ref": f"{run['task_id']}@{run['task_revision']}" if run else "",
                "historical_policy_ref": run["policy_revision"] if run else self.core.projects.get_project(project_id)["policy_head"],
                "registry_revision": run["registry_revision"] if run else self.core.registry.head(project_id),
                "effect_cursor_ref": f"journal_cursor_{hwm}",
                "adapter": agent["adapter"],
                "requires_reconciliation": bool(unresolved),
            }
            version_id = hash_value(manifest)
            manifest_full = dict(manifest, version_id=version_id)
            manifest_ref = self.objects.put_json(project_id, manifest_full)
            # 5 short transaction: add version + CAS branch head
            with self.db.tx() as c:
                if not c.execute("SELECT 1 FROM versions WHERE version_id=?", (version_id,)).fetchone():
                    c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)",
                              (version_id, project_id, branch_id, manifest_ref, dumps(manifest["parent_ids"]), label,
                               1 if unresolved else 0, actor, utcnow()))
                if self.fail_head_cas:
                    raise RuntimeError("simulated failure in head transaction")
                cas = c.execute("UPDATE branches SET head_version=? WHERE branch_id=? AND COALESCE(head_version,'')=?",
                                (version_id, branch_id, b["head_version"] or ""))
                if cas.rowcount != 1:
                    raise ConflictError("STALE_HEAD", "branch head changed during save")
        finally:
            self.db.execute("UPDATE branches SET barrier=0 WHERE branch_id=?", (branch_id,))
        self.journal.append(project_id, f"branch:{branch_id}", "version.saved",
                            {"version_id": version_id, "branch_id": branch_id, "label": label,
                             "requires_reconciliation": bool(unresolved), "lesson_revisions": manifest["lesson_revisions"]},
                            actor_type="version_service")
        return self.version(version_id)

    def snapshot_tree(self, project_id: str, workspace_dir: str, store: bool = True) -> dict:
        """Tree of the workspace. With store=False only hashes are computed (read-only comparison)."""
        root = Path(workspace_dir)
        tree = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                if p.is_symlink():
                    continue
                data = p.read_bytes()
                ref = self.objects.put(project_id, data) if store else content_ref(data)
                tree[str(p.relative_to(root)).replace("\\", "/")] = {"blob": ref, "size": len(data)}
        return {"schema_version": 1, "files": tree}

    # ================================================================ read
    def version(self, version_id: str) -> dict:
        v = self.db.one("SELECT * FROM versions WHERE version_id=?", (version_id,))
        if not v:
            raise NotFound(f"version {version_id}")
        v["parents"] = loads(v.pop("parents_json"), [])
        v["manifest"] = self.objects.get_json(v["project_id"], v["manifest_ref"])
        return v

    def versions(self, project_id: str, agent_id: str | None = None) -> list[dict]:
        rows = self.db.all("SELECT v.version_id FROM versions v JOIN branches b ON b.branch_id=v.branch_id WHERE v.project_id=?" +
                           (" AND b.agent_id=?" if agent_id else "") + " ORDER BY v.created_at",
                           (project_id, agent_id) if agent_id else (project_id,))
        return [self.version(r["version_id"]) for r in rows]

    def manifest_state(self, project_id: str, manifest: dict) -> dict:
        """Load the supported state referenced by a manifest."""
        g = lambda ref: self.objects.get_json(project_id, ref)  # noqa: E731
        agent_cfg = g(manifest["agent_config_ref"])
        env = g(manifest["environment_ref"])
        return {
            "instructions": agent_cfg.get("instructions", ""),
            "model": agent_cfg.get("model", {}),
            "environment": {k: v for k, v in env.items() if k not in ("os", "python")},
            "environment_recorded": env,
            "lesson_revisions": list(manifest["lesson_revisions"]),
            "disabled_lessons": list(manifest["disabled_lessons"]),
            "memory_facts": g(manifest["memory_ref"]),
            "files": g(manifest["artifact_tree_ref"])["files"],
            "adapter_state": g(manifest["adapter_state_ref"]),
            "context_packet_ref": manifest.get("context_packet_ref"),
        }

    # ================================================================ fork / restore
    def preview_restore(self, project_id: str, version_id: str) -> dict:
        v = self.version(version_id)
        if v["project_id"] != project_id:
            raise NotFound(f"version {version_id}")
        m = v["manifest"]
        if m.get("schema_version") != MANIFEST_SCHEMA:
            raise InvalidRequest("INCOMPATIBLE_MANIFEST", f"manifest schema {m.get('schema_version')} unsupported", status=422)
        if m.get("adapter") not in SUPPORTED_ADAPTERS:
            raise InvalidRequest("INCOMPATIBLE_MANIFEST", f"adapter {m.get('adapter')} unsupported", status=422)
        caps = self.core.coordinator.capabilities(m.get("adapter"))
        cur_policy = self.core.projects.policy(project_id)
        hist = m.get("historical_policy_ref")
        diffs = []
        if hist and hist != cur_policy["revision"]:
            old = self.core.projects.policy(project_id, hist)
            for k in sorted(set(old) | set(cur_policy)):
                if k in ("revision", "body_hash"):
                    continue
                if old.get(k) != cur_policy.get(k):
                    diffs.append({"field": k, "historical": old.get(k), "current": cur_policy.get(k)})
        excluded = []
        for ref in m["lesson_revisions"]:
            if self.core.registry.ref_revoked(project_id, ref):
                excluded.append({"ref": ref, "reason": "globally revoked; will not be retrieved"})
        agent_id = self.db.scalar("SELECT agent_id FROM branches WHERE branch_id=?", (v["branch_id"],))
        pending = self.db.all("SELECT e.operation_id, e.tool_id, e.state, e.run_id FROM effects e JOIN runs r ON r.run_id=e.run_id "
                              "WHERE e.project_id=? AND r.agent_id=? AND e.state IN ('UNKNOWN','DISPATCHING')", (project_id, agent_id))
        task_note = None
        if m.get("task_revision_ref"):
            tid, rev = m["task_revision_ref"].split("@")
            cur = self.core.projects.current_task_revision(project_id, tid)
            if cur != int(rev):
                task_note = f"Task {tid} is now at revision {cur}; resume uses the current authorization."
        return {"version_id": version_id, "capabilities": caps.model_dump(),
                "unsupported": [k for k, v in caps.model_dump().items() if not v],
                "policy": {"historical": hist, "current": cur_policy["revision"], "differences": diffs,
                           "note": "Resume executes under the current policy floor; old grants are not copied."},
                "excluded_knowledge": excluded, "pending_effects": pending,
                "requires_reconciliation": bool(m.get("requires_reconciliation")) or bool(pending),
                "task": task_note,
                "message": "Restores supported agent state and files. Completed external actions remain recorded."}

    def fork(self, project_id: str, version_id: str, name: str | None, purpose: str, actor: str) -> dict:
        preview = self.preview_restore(project_id, version_id)
        v = self.version(version_id)
        m = v["manifest"]
        if purpose == "restore" and not preview["capabilities"]["restore_cursor"]:
            raise InvalidRequest("CAPABILITY_UNSUPPORTED", "this adapter cannot restore an agent cursor; only tool history and workspace versions are available", status=422)
        src_branch = self.branch(v["branch_id"])
        loaded = self.manifest_state(project_id, m)
        state = {"schema_version": 1, "instructions": loaded["instructions"], "model": copy.deepcopy(loaded["model"]),
                 "environment": copy.deepcopy(loaded["environment"]), "lesson_revisions": list(loaded["lesson_revisions"]),
                 "disabled_lessons": list(loaded["disabled_lessons"]), "memory_facts": copy.deepcopy(loaded["memory_facts"]),
                 "context_refs": []}
        nb = self.create_branch(project_id, src_branch["agent_id"], name or f"{purpose}-{version_id[7:15]}", state,
                                parent_branch_id=src_branch["branch_id"], base_version=version_id, purpose=purpose)
        self.materialize(project_id, loaded["files"], nb["workspace_dir"])
        self.carry_artifact_checks(project_id, nb["branch_id"], loaded["files"])
        cursor = loaded["adapter_state"]
        if preview["capabilities"]["restore_cursor"]:
            self.core.coordinator.restore_adapter_state(nb, cursor, version_id)
        self.journal.append(project_id, f"branch:{nb['branch_id']}", "run.restored",
                            {"version_id": version_id, "branch_id": nb["branch_id"], "purpose": purpose,
                             "excluded_knowledge": preview["excluded_knowledge"], "policy": preview["policy"]},
                            actor_type="version_service")
        return {"branch": self.branch(nb["branch_id"]), **preview}

    def carry_artifact_checks(self, project_id: str, branch_id: str, files: dict) -> int:
        """Verified-check records are bound to exact content; carry them to a branch holding identical bytes."""
        n = 0
        for rel, info in files.items():
            digest = info["blob"]
            row = self.db.one("SELECT * FROM artifacts WHERE project_id=? AND path=? AND content_hash=? ORDER BY updated_at DESC LIMIT 1",
                              (project_id, rel, digest))
            if row:
                self.db.execute("INSERT OR REPLACE INTO artifacts(project_id,branch_id,path,content_hash,run_id,operation_id,data_class,check_status,check_detail_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (project_id, branch_id, rel, digest, row["run_id"], row["operation_id"], row["data_class"], row["check_status"],
                                 row["check_detail_json"], utcnow()))
                n += 1
        return n

    def materialize(self, project_id: str, files: dict, workspace_dir: str) -> None:
        """Copy immutable blobs into a separate writable directory. Never hard-link."""
        root = Path(workspace_dir)
        for rel, info in files.items():
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = self.objects.get(project_id, info["blob"])
            with open(dst, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            st = os.stat(dst)
            if st.st_nlink != 1:
                raise RuntimeError("isolation check failed: materialized file has multiple links")

    def reset_workspace(self, project_id: str, workspace_dir: str, files: dict) -> None:
        root = Path(workspace_dir)
        for p in sorted(root.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
        self.materialize(project_id, files, workspace_dir)

    # ================================================================ compare
    def compare(self, project_id: str, a: str, b: str) -> dict:
        va, vb = self.version(a), self.version(b)
        sa, sb = self.manifest_state(project_id, va["manifest"]), self.manifest_state(project_id, vb["manifest"])
        return diff_states(self, project_id, sa, sb, va["manifest"], vb["manifest"])

    def working_state(self, branch_id: str, store: bool = True) -> dict:
        b = self.branch(branch_id)
        tree = self.snapshot_tree(b["project_id"], b["workspace_dir"], store=store)
        st = b["state"]
        return {"instructions": st.get("instructions", ""), "model": st.get("model", {}), "environment": st.get("environment", {}),
                "lesson_revisions": st.get("lesson_revisions", []), "disabled_lessons": st.get("disabled_lessons", []),
                "memory_facts": st.get("memory_facts", {}), "files": tree["files"]}

    def is_clean(self, branch_id: str) -> bool:
        b = self.branch(branch_id)
        if not b["head_version"]:
            return False
        head = self.manifest_state(b["project_id"], self.version(b["head_version"])["manifest"])
        ws = self.working_state(branch_id, store=False)
        keys = ("instructions", "lesson_revisions", "disabled_lessons", "memory_facts", "files")
        return all(_norm(head[k]) == _norm(ws[k]) for k in keys)

    # ================================================================ ancestry
    def ancestors(self, version_id: str) -> dict[str, int]:
        dist, frontier = {version_id: 0}, [version_id]
        while frontier:
            nxt = []
            for v in frontier:
                row = self.db.one("SELECT parents_json FROM versions WHERE version_id=?", (v,))
                for p in loads(row["parents_json"], []) if row else []:
                    if p not in dist:
                        dist[p] = dist[v] + 1
                        nxt.append(p)
            frontier = nxt
        return dist

    def common_ancestor(self, a: str, b: str) -> str | None:
        da, db_ = self.ancestors(a), self.ancestors(b)
        common = set(da) & set(db_)
        if not common:
            return None
        return min(common, key=lambda v: (da[v] + db_[v], v))

    # ================================================================ GC
    def gc(self, project_id: str, dry_run: bool = True) -> dict:
        roots = set()
        for b in self.db.all("SELECT head_version FROM branches WHERE project_id=? AND head_version IS NOT NULL", (project_id,)):
            roots |= set(self.ancestors(b["head_version"]))
        for v in self.db.all("SELECT version_id FROM versions WHERE project_id=? AND label IS NOT NULL", (project_id,)):
            roots |= set(self.ancestors(v["version_id"]))
        keep = set()
        for vid in roots:
            v = self.version(vid)
            keep.add(v["manifest_ref"])
            m = v["manifest"]
            for k, ref in m.items():
                if k.endswith("_ref") and isinstance(ref, str) and ref.startswith("sha256:"):
                    keep.add(ref)
            for info in self.objects.get_json(project_id, m["artifact_tree_ref"])["files"].values():
                keep.add(info["blob"])
        # audit retention roots: everything referenced from durable records
        for sql in ("SELECT payload_ref r FROM events WHERE project_id=?", "SELECT view_ref r FROM sources WHERE project_id=?",
                    "SELECT args_ref r FROM actions WHERE project_id=?", "SELECT result_ref r FROM effects WHERE project_id=?",
                    "SELECT packet_ref r FROM context_packets WHERE project_id=?"):
            keep |= {r["r"] for r in self.db.all(sql, (project_id,)) if r["r"]}
        for b in self.db.all("SELECT workspace_dir FROM branches WHERE project_id=?", (project_id,)):
            pass  # working copies are live files, not objects
        now = time.time()
        doomed = [(ref, p) for ref, p in self.objects.all_refs(project_id) if ref not in keep and now - p.stat().st_mtime > self.config.OBJECT_GC_GRACE_SECONDS]
        if not dry_run:
            for _, p in doomed:
                p.unlink(missing_ok=True)
        tmp = self.objects.cleanup_tmp(project_id, self.config.OBJECT_GC_GRACE_SECONDS) if not dry_run else 0
        return {"reachable_versions": len(roots), "kept_objects": len(keep), "deleted" if not dry_run else "would_delete": len(doomed), "tmp_cleaned": tmp}


def _canon(v):
    """Make adapter state canonical-safe (floats -> strings)."""
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    return str(v)


def _norm(v):
    return canonical_bytes(_canon(v))


def diff_states(store: VersionStore, project_id: str, a: dict, b: dict, ma: dict | None = None, mb: dict | None = None) -> dict:
    changes, summary = {}, []
    if a["instructions"] != b["instructions"]:
        changes["instructions"] = {"before": a["instructions"], "after": b["instructions"],
                                   "diff": list(difflib.unified_diff(a["instructions"].splitlines(), b["instructions"].splitlines(), lineterm=""))}
        summary.append({"text": "Changed agent instructions", "path": "instructions"})
    la = {r.split("@")[0]: r for r in a["lesson_revisions"]}
    lb = {r.split("@")[0]: r for r in b["lesson_revisions"]}
    lessons = []
    for lid in sorted(set(la) | set(lb)):
        if la.get(lid) == lb.get(lid):
            continue
        ref = lb.get(lid) or la.get(lid)
        lid_, rev = ref.split("@")
        rec = store.core.registry.lesson(project_id, lid_, int(rev))
        cand = None
        if rec is None:
            cand = store.db.one("SELECT body_json, kind FROM candidates WHERE lesson_id=? ORDER BY revision DESC LIMIT 1", (lid_,))
        body = rec["body"] if rec else (loads(cand["body_json"]) if cand else {})
        kind = rec["kind"] if rec else (cand["kind"] if cand else "unknown")
        entry = {"lesson_id": lid, "before": la.get(lid), "after": lb.get(lid), "kind": kind,
                 "conditions": body.get("conditions", {}), "procedure": body.get("procedure", {}),
                 "scope": body.get("scope", {}), "evaluation_id": rec["evaluation_id"] if rec else None,
                 "revoked": store.core.registry.ref_revoked(project_id, ref) if rec else False}
        lessons.append(entry)
        verb = "Added" if not la.get(lid) else "Removed" if not lb.get(lid) else "Revised"
        what = f"{kind} {lb.get(lid) or la.get(lid)}"
        if body.get("procedure"):
            what += f" ({', '.join(f'{k}={v}' for k, v in body['procedure'].items())} for {body.get('scope', {}).get('tool', 'scope')})"
        summary.append({"text": f"{verb} {what}", "path": f"lessons.{lid}"})
    if lessons:
        changes["lessons"] = lessons
    da, db_ = set(a["disabled_lessons"]), set(b["disabled_lessons"])
    if da != db_:
        changes["disabled_lessons"] = {"added": sorted(db_ - da), "removed": sorted(da - db_)}
        for r in sorted(db_ - da):
            summary.append({"text": f"Disabled lesson {r} on this branch", "path": "disabled_lessons"})
    mem = []
    for k in sorted(set(a["memory_facts"]) | set(b["memory_facts"])):
        x, y = a["memory_facts"].get(k), b["memory_facts"].get(k)
        if _norm(x) != _norm(y):
            mem.append({"key": k, "before": x, "after": y})
            verb = "Added" if x is None else "Removed" if y is None else "Changed"
            note = " (seeded test condition)" if (y or x or {}).get("seeded_test_condition") else ""
            summary.append({"text": f"{verb} memory fact '{k}'{note}", "path": f"memory.{k}"})
    if mem:
        changes["memory"] = mem
    if _norm(a.get("model")) != _norm(b.get("model")):
        changes["model"] = {"before": a.get("model"), "after": b.get("model")}
        summary.append({"text": "Changed model settings", "path": "model"})
    if _norm(a.get("environment")) != _norm(b.get("environment")):
        changes["environment"] = {"before": a.get("environment"), "after": b.get("environment")}
        summary.append({"text": "Changed environment profile", "path": "environment"})
    files = []
    for path in sorted(set(a["files"]) | set(b["files"])):
        x, y = a["files"].get(path), b["files"].get(path)
        if (x or {}).get("blob") == (y or {}).get("blob"):
            continue
        entry = {"path": path, "before": x, "after": y, "status": "added" if not x else "deleted" if not y else "modified"}
        if x and y and max(x["size"], y["size"]) < 64000:
            try:
                ta = store.objects.get(project_id, x["blob"]).decode("utf-8")
                tb = store.objects.get(project_id, y["blob"]).decode("utf-8")
                entry["text_diff"] = list(difflib.unified_diff(ta.splitlines(), tb.splitlines(), "before", "after", lineterm=""))[:400]
            except UnicodeDecodeError:
                entry["text_diff"] = None
        files.append(entry)
        summary.append({"text": f"{entry['status'].capitalize()} file {path}", "path": f"files.{path}"})
    if files:
        changes["files"] = files
    if ma and mb:
        meta = {}
        for k in ("task_revision_ref", "historical_policy_ref", "registry_revision", "context_packet_ref", "effect_cursor_ref"):
            if ma.get(k) != mb.get(k):
                meta[k] = {"before": ma.get(k), "after": mb.get(k)}
        if meta:
            changes["references"] = meta
            if "context_packet_ref" in meta:
                summary.append({"text": "Different context packet", "path": "references.context_packet_ref"})
        evals = []
        for entry in changes.get("lessons", []):
            if entry.get("evaluation_id"):
                ev = store.db.one("SELECT evaluation_id, status, summary_json FROM evaluations WHERE evaluation_id=?", (entry["evaluation_id"],))
                if ev:
                    evals.append({"evaluation_id": ev["evaluation_id"], "status": ev["status"], "summary": loads(ev["summary_json"], {})})
        if evals:
            changes["evaluations"] = evals
    return {"changes": changes, "summary": summary, "change_keys": change_keys(changes)}


def change_keys(changes: dict) -> list[str]:
    keys = []
    if "instructions" in changes:
        keys.append("instructions")
    for l in changes.get("lessons", []):
        keys.append(f"lesson:{l['lesson_id']}")
    for r in changes.get("disabled_lessons", {}).get("added", []) + changes.get("disabled_lessons", {}).get("removed", []):
        keys.append(f"disabled:{r}")
    for m in changes.get("memory", []):
        keys.append(f"memory:{m['key']}")
    for f in changes.get("files", []):
        keys.append(f"file:{f['path']}")
    if "model" in changes:
        keys.append("model")
    if "environment" in changes:
        keys.append("environment")
    return keys
