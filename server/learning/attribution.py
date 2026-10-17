"""Optional controlled attribution (Technical Design: Optional controlled attribution).

For a blocked action, rerun the same assignment in isolated sandboxes from the
pre-observation state: once with the original sources and once with the
candidate spans masked, a bounded number of times each, with every effect
confined to the sandbox. If the unsafe proposal recurs with the original
sources and disappears when only the candidate spans are masked, the
hypothesis is labeled "intervention supported" - stronger evidence than
exposure, not universal causal proof. Both trajectories are persisted.
"""
from __future__ import annotations

from ..canonical import dumps, loads, utcnow
from .patterns import features, sentences


class Attribution:
    def __init__(self, core):
        self.core = core
        self.db = core.db

    def counterfactual(self, incident_id: str, repeats: int = 2) -> dict:
        c = self.core
        inc = c.incidents.get(incident_id)
        if not inc or inc["category"] != "blocked_action":
            raise ValueError("counterfactual attribution applies to blocked actions")
        run = c.coordinator.run(inc["run_id"])
        dec_id = next(r.split(":", 1)[1] for r in inc["evidence_refs"] if r.startswith("decision:"))
        dec = self.db.one("SELECT * FROM decisions WHERE decision_id=?", (dec_id,))
        args = c.objects.get_json(run["project_id"], dec["canonical_args_ref"], restricted=True)
        values = [str(v) for v in args.values() if isinstance(v, str) and len(v) >= 3]
        anchored = {r.partition(":chunk_")[0] for r in inc["evidence_refs"] if ":chunk_" in r}
        task = c.projects.get_task(run["project_id"], run["task_id"], run["task_revision"])
        cfg = loads(run["start_config_json"])
        original, masked, masked_spans = [], [], []
        for sid in task["body"]["resources"]["source_ids"]:
            src = c.sources.get(sid)
            view = c.sources.view(sid)
            fn = (src["label"] or sid) + ".txt"
            original.append({"inline": view, "filename": fn, "channel": "document"})
            text = view
            if sid in anchored:
                for s, e, sent in sorted(sentences(view), reverse=True):
                    if any(v.lower() in sent.lower() for v in values) or features(sent)["requested_deviations"]:
                        text = text[:s] + "[masked span]" + text[e:]
                        masked_spans.append({"source_id": sid, "start": s, "end": e})
            masked.append({"inline": text, "filename": fn, "channel": "document"})
        t = task["body"]
        base_case = {"case_id": f"cf_{incident_id}", "kind": "attack_variant", "family": "injection",
                     "environment": cfg.get("environment", {}),
                     "agent": {"follows_embedded_instructions": bool(cfg["model"].get("follows_embedded_instructions"))},
                     "task": {"goal": t["goal"], "tools": t["tools"], "recipient_ids": t["recipient_ids"],
                              "output_path": t["resources"].get("output_path"), "required_phrases": t["resources"].get("required_phrases", [])},
                     "expect": {}}
        # keep the original run's fixed settings: the procedure lessons it had loaded (patterns excluded so content reaches the model)
        lessons = [dict(l, under_test=False) for l in c.registry.active(run["project_id"], run["registry_revision"]) if l["kind"] == "procedure"]
        trajectories = {"original": [], "masked": []}
        for label, srcs in (("original", original), ("masked", masked)):
            for _ in range(repeats):
                o = c.evaluator.run_case(dict(base_case, sources=srcs), lessons, [])
                same = [d for d in o["decisions"] if d["tool_id"] == dec["tool_id"] and d["outcome"] == "DENY"]
                trajectories[label].append({"unsafe_proposals": len(same), "decisions": o["decisions"], "task_completed": o["task_completed"]})
        reproduced = all(tr["unsafe_proposals"] > 0 for tr in trajectories["original"])
        removed = all(tr["unsafe_proposals"] == 0 for tr in trajectories["masked"])
        supported = reproduced and removed
        result = {"incident_id": incident_id, "repeats": repeats, "masked_spans": masked_spans, "trajectories": trajectories,
                  "reproduced_with_original": reproduced, "absent_when_masked": removed,
                  "conclusion": "intervention_supported" if supported else "inconclusive",
                  "setup": "Same assignment and planner settings; sandboxed effects; only the candidate spans differ.",
                  "remaining_uncertainty": "Bounded reruns of a deterministic simulated planner; not universal causal certainty.",
                  "ran_at": utcnow()}
        ref = c.objects.put_json(run["project_id"], _canon(result))
        hyps = inc["hypotheses"] + [{"status": "intervention_supported" if supported else "hypothesized",
                                     "text": ("Masking only the linked spans removed the unsafe proposal in every rerun; the original sources reproduced it."
                                              if supported else "Controlled rerun was inconclusive."),
                                     "evidence_refs": [f"object:{ref}"], "counterfactual": {k: result[k] for k in ("repeats", "reproduced_with_original", "absent_when_masked")}}]
        c.incidents.update(incident_id, hypotheses=hyps, attribution_level="intervention_supported" if supported else inc["attribution_level"])
        result["evidence_ref"] = ref
        return result


def _canon(v):
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dict):
        return {str(k): _canon(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    return v
