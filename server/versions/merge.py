"""Three-way selected merge with combined-state validation.

For each selected record: current == ancestor -> take candidate; candidate ==
ancestor -> keep current; equal -> keep; otherwise conflict (unless the user
supplied an explicit resolution). Credentials, effect history, grants and
policy are never merged as agent content. The combined state is checked
against protected regressions before the head compare-and-swap.
"""
from __future__ import annotations

import copy

from ..canonical import loads
from ..projects import ConflictError, InvalidRequest
from .store import _norm

FORBIDDEN_PREFIXES = ("credential", "secret", "effect", "grant", "policy", "permission", "task")


def _get(state: dict, key: str):
    kind, _, name = key.partition(":")
    if key == "instructions":
        return state["instructions"]
    if key == "model":
        return state.get("model")
    if key == "environment":
        return state.get("environment")
    if kind == "lesson":
        return next((r for r in state["lesson_revisions"] if r.split("@")[0] == name), None)
    if kind == "disabled":
        return name if name in state["disabled_lessons"] else None
    if kind == "memory":
        return state["memory_facts"].get(name)
    if kind == "file":
        f = state["files"].get(name)
        return f["blob"] if f else None
    raise InvalidRequest("UNKNOWN_CHANGE_KEY", f"unknown change key {key}")


def _set(state: dict, key: str, value, source_state: dict) -> None:
    kind, _, name = key.partition(":")
    if key in ("instructions", "model", "environment"):
        state[key] = copy.deepcopy(value)
    elif kind == "lesson":
        state["lesson_revisions"] = [r for r in state["lesson_revisions"] if r.split("@")[0] != name] + ([value] if value else [])
        state["lesson_revisions"].sort()
    elif kind == "disabled":
        s = set(state["disabled_lessons"])
        (s.add(name) if value else s.discard(name))
        state["disabled_lessons"] = sorted(s)
    elif kind == "memory":
        if value is None:
            state["memory_facts"].pop(name, None)
        else:
            state["memory_facts"][name] = copy.deepcopy(value)
    elif kind == "file":
        if value is None:
            state["files"].pop(name, None)
        else:
            src = source_state["files"].get(name)
            state["files"][name] = copy.deepcopy(src) if src and src["blob"] == value else {"blob": value, "size": 0}


def three_way(ancestor: dict, current: dict, candidate: dict, selected: list[str], resolutions: dict) -> tuple[dict, list[dict], list[dict]]:
    merged = copy.deepcopy(current)
    conflicts, applied = [], []
    for key in selected:
        if key.split(":")[0].lower().startswith(FORBIDDEN_PREFIXES) or key.lower().startswith(FORBIDDEN_PREFIXES):
            raise InvalidRequest("NOT_MERGEABLE", f"{key} is not ordinary agent content and cannot be merged")
        a, c, s = _get(ancestor, key), _get(current, key), _get(candidate, key)
        if _norm(c) == _norm(a):
            _set(merged, key, s, candidate)
            applied.append({"key": key, "result": "candidate", "value": s})
        elif _norm(s) == _norm(a):
            applied.append({"key": key, "result": "kept_current", "value": c})
        elif _norm(c) == _norm(s):
            applied.append({"key": key, "result": "identical", "value": c})
        else:
            res = resolutions.get(key)
            if res == "candidate":
                _set(merged, key, s, candidate)
                applied.append({"key": key, "result": "resolved_candidate", "value": s})
            elif res == "current":
                applied.append({"key": key, "result": "resolved_current", "value": c})
            else:
                kind = "delete_vs_modify" if (c is None) != (s is None) else "both_modified"
                conflicts.append({"key": key, "kind": kind, "ancestor": a, "current": c, "candidate": s})
    return merged, applied, conflicts


class MergeService:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.store = core.versions

    def merge(self, project_id: str, target_branch: str, expected_head: str | None, source_version: str,
              selected: list[str], resolutions: dict, suite_revision: str | None, actor: str) -> dict:
        tb = self.store.branch(target_branch)
        if tb["project_id"] != project_id:
            raise InvalidRequest("NOT_FOUND", "branch not in project", status=404)
        head = tb["head_version"]
        if not head:
            raise ConflictError("NO_HEAD", "target branch has no saved version")
        if expected_head is not None and expected_head != head:
            raise ConflictError("STALE_HEAD", f"target head is {head}")
        if not self.store.is_clean(target_branch):
            raise ConflictError("UNSAVED_CHANGES", "save the target branch before merging")
        anc = self.store.common_ancestor(head, source_version)
        cur_state = self.store.manifest_state(project_id, self.store.version(head)["manifest"])
        cand_state = self.store.manifest_state(project_id, self.store.version(source_version)["manifest"])
        if anc:
            anc_state = self.store.manifest_state(project_id, self.store.version(anc)["manifest"])
        else:
            anc_state = {"instructions": "", "model": None, "environment": None, "lesson_revisions": [], "disabled_lessons": [],
                         "memory_facts": {}, "files": {}}
        merged, applied, conflicts = three_way(anc_state, cur_state, cand_state, selected, resolutions)
        if conflicts:
            raise ConflictError("MERGE_CONFLICT", f"{len(conflicts)} selected change(s) conflict", detail={"conflicts": conflicts, "ancestor": anc})
        # revoked lessons cannot be merged back in
        for r in merged["lesson_revisions"]:
            if self.core.registry.ref_revoked(project_id, r) and r not in cur_state["lesson_revisions"]:
                raise ConflictError("REVOKED_LESSON", f"{r} is revoked and cannot be merged")
        # validate the combined state against protected regressions
        evaluation = self.core.evaluator.check_combined(project_id, merged, suite_revision, actor=actor)
        if evaluation["status"] != "passed":
            raise ConflictError("MERGE_CHECKS_FAILED", "combined state failed protected checks; head unchanged",
                                detail={"evaluation_id": evaluation["evaluation_id"], "summary": evaluation["summary"]})
        # apply to the target working copy, save with both parents, CAS the head
        st = tb["state"]
        for k in ("instructions", "model", "environment", "lesson_revisions", "disabled_lessons", "memory_facts"):
            if merged.get(k) is not None:
                st[k] = merged[k]
        self.store.set_state(target_branch, st)
        self.store.reset_workspace(project_id, tb["workspace_dir"], merged["files"])
        v = self.store.save(project_id, target_branch, head, f"merge {source_version[7:15]}", actor, parents=[head, source_version])
        return {"version": v, "applied": applied, "ancestor": anc, "evaluation_id": evaluation["evaluation_id"]}
