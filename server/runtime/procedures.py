"""Bounded procedure module.

Accepted `procedure` lessons may set only the operator-declared learnable
parameters of a tool (e.g. write_report.encoding). Action-time conditions are
evaluated by code here, never trusted because a model asserted them. The
module cannot touch permissions, destinations, roots or the checker.
"""
from __future__ import annotations

from ..executors.files import ENV_EXPORT_FIXTURES


def eval_action_condition(name: str, expected, tool_id: str, args: dict, environment: dict) -> bool | None:
    """Return True/False for known conditions, None for unknown (fail safe: not applied)."""
    if name == "text_contains_nonencodable_characters":
        enc = ENV_EXPORT_FIXTURES.get(environment.get("export_adapter"), {}).get("default_encoding", "utf-8")
        try:
            str(args.get("content", "")).encode(enc)
            actual = False
        except UnicodeEncodeError:
            actual = True
        return actual == bool(expected)
    if name in ("adapter", "default_encoding", "adapter_version", "encoding"):
        return True  # static conditions were already checked by the context compiler
    return None


def apply_procedures(tool: dict, args: dict, lessons: list[dict], environment: dict) -> tuple[dict, list[dict]]:
    learnable = tool.get("learnable_params", {})
    new_args = dict(args)
    applied = []
    for l in lessons:
        if l.get("kind") != "procedure" or l.get("scope", {}).get("tool") != tool["tool_id"]:
            continue
        ok = True
        for k, v in (l.get("conditions") or {}).items():
            r = eval_action_condition(k, v, tool["tool_id"], args, environment)
            if r is not True:
                ok = False
                break
        if not ok:
            applied.append({"ref": l["ref"], "applied": False, "reason": "action-time condition not met"})
            continue
        changes = {}
        for param, value in (l.get("procedure") or {}).items():
            if param in args and args[param] not in (None, ""):
                # the model chose this parameter explicitly; the tool default is not in play
                applied.append({"ref": l["ref"], "applied": False, "reason": f"{param} set explicitly by the model"})
                continue
            if param in learnable and value in learnable[param]:
                if new_args.get(param) != value:
                    changes[param] = value
                    new_args[param] = value
        applied.append({"ref": l["ref"], "applied": bool(changes), "changes": changes, "under_test": l.get("under_test", False)})
    return new_args, applied
