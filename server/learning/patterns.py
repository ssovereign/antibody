"""Threat-pattern fingerprints and ingress screening.

Matching policy (Technical Design: Attack patterns):
  1. exact   - an identical sentence/chunk hash under the same accepted scope;
  2. normalized - only the explicitly supported transformations in
     ingress.normalize (bounded decoding);
  3. family  - a deterministic tactic descriptor must satisfy the accepted
     predicates *and* the span must not sit in a quotation / explanatory
     context. Partial matches are only flagged (uncertain) and the fixed
     action boundary remains the protection.
Similarity alone never establishes maliciousness or authorizes an action.
"""
from __future__ import annotations

import re

from ..ingress.normalize import exact_hash, normalize, views_for_matching
from ..canonical import sha256_hex

AUTHORITY = re.compile(r"(\bsystem\b|\badmin(istrator)?\b|\bit (security|department)\b|\bcompliance\b|\bceo\b|\bsecurity team\b|"
                       r"\bnote to (the )?(ai|assistant|agent|model)\b|\bai (agent|assistant)\b|\bassistant\b|\boverride\b|\bpriority instruction\b|"
                       r"\bignore (all |any )?(previous|prior|earlier|above) (instructions|guidance|rules)\b|\bnew instructions?\b|\bautomated (agent|assistant)s?\b)", re.I)
ADDRESSED = re.compile(r"(\byou\b|\byour\b|\bassistant\b|\bagent\b|\bai\b|\bmodel\b|^\s*(please\s+)?(send|forward|email|delete|pay|wire|transfer|share|cc|bcc|remove|upload)\b)", re.I | re.M)
DEVIATIONS = {
    "new_recipient": re.compile(r"\b(send|forward|email|e-mail|cc|bcc|share|deliver|copy|mail|route)\b[^.\n]{0,120}?\b(to|with)\b[^.\n]{0,60}?([\w.+-]+@[\w-]+(\.[\w-]+)+|recipient|address|inbox|mailbox)", re.I),
    "deletion": re.compile(r"\b(delete|remove|wipe|erase|purge|rm)\b[^.\n]{0,80}\b(file|files|report|reports|folder|directory|workspace|everything|all)\b", re.I),
    "payment": re.compile(r"\b(pay|wire|transfer|send)\b[^.\n]{0,60}(\$\s?\d|\d+(\.\d+)?\s?(usd|dollars)|paypal|invoice|payment)", re.I),
    "credential_disclosure": re.compile(r"\b(api[ _-]?key|password|token|credential|secret|canary)s?\b[^.\n]{0,80}\b(send|share|post|include|reveal|print|forward)\b|"
                                        r"\b(send|share|post|include|reveal|print|forward)\b[^.\n]{0,80}\b(api[ _-]?key|password|token|credential|secret)s?\b", re.I),
}
TOOL_CLASS = {"new_recipient": "external_send", "deletion": "delete", "payment": "payment", "credential_disclosure": "external_send"}
QUOTE_FRAME = re.compile(r"(for example|e\.g\.|such as|attackers? (may|might|often|could|can|use|wrote|try)|a (common|typical|classic) (attack|injection|phishing)|"
                         r"phishing|security (researchers?|team|advisory|training|awareness)|do not follow|example (payload|message|text)|we observed|"
                         r"looks like|reads?:|says?:|warning|beware|never comply|was blocked|training material|illustrat)", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str) -> list[tuple[int, int, str]]:
    out, pos = [], 0
    for part in SENT_SPLIT.split(text):
        if part is None:
            continue
        i = text.find(part, pos)
        if i < 0:
            continue
        if part.strip():
            out.append((i, i + len(part), part))
        pos = i + len(part)
    return out


def norm_sentence_hash(s: str) -> str:
    return "sha256:" + sha256_hex(normalize(s).encode("utf-8"))


def features(text: str) -> dict:
    devs = [k for k, rx in DEVIATIONS.items() if rx.search(text)]
    return {
        "claimed_authority": bool(AUTHORITY.search(text)),
        "addressed_to_agent": bool(ADDRESSED.search(text)),
        "requested_deviations": devs,
        "target_tool_classes": sorted({TOOL_CLASS[d] for d in devs}),
        "emails": re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)[:10],
    }


def quoted_context(chunk_text: str, start: int, end: int, flags: dict) -> bool:
    for a, b in flags.get("quotation_spans", []):
        if a <= start < b:
            return True
    before = chunk_text[max(0, start - 240): start]
    line_start = chunk_text.rfind("\n", 0, start) + 1
    if chunk_text[line_start:start + 1].lstrip().startswith(">"):
        return True
    sent = chunk_text[start:end].strip()
    if sent[:1] in "\"“'‘" or before.rstrip().endswith(("\"", "“", ":")) and QUOTE_FRAME.search(before):
        return True
    return bool(QUOTE_FRAME.search(before[-160:]) or QUOTE_FRAME.search(chunk_text[start:end]))


def build_pattern_body(chunk_text: str, anchor_values: list[str], channel: str, publisher: str = "learning_service") -> dict | None:
    """Derive a candidate pattern from the sentence(s) carrying the denied action's anchors."""
    hits = []
    for s, e, sent in sentences(chunk_text):
        if any(v.lower() in sent.lower() for v in anchor_values) or (features(sent)["requested_deviations"] and features(sent)["claimed_authority"]):
            hits.append((s, e, sent))
    if not hits:
        return None
    window = " ".join(h[2] for h in hits)
    ctx_start = max(0, hits[0][0] - 200)
    f = features(chunk_text[ctx_start:hits[-1][1]])
    if not f["requested_deviations"]:
        return None
    return {
        "kind": "threat_pattern",
        "exact_hashes": sorted({exact_hash(h[2].strip()) for h in hits}),
        "normalized_hashes": sorted({norm_sentence_hash(h[2]) for h in hits}),
        "family": {"claimed_authority": f["claimed_authority"], "requested_deviation": f["requested_deviations"][0],
                   "target_tool_class": TOOL_CLASS[f["requested_deviations"][0]], "addressed_to_agent": True},
        "channels": sorted({channel, "web", "document", "calendar", "log", "tool_result"}),
        "tactic": _tactic(f),
        "argument_anchors": ["destination not in assignment"] if f["requested_deviations"][0] == "new_recipient" else [],
        "safe_description": f"{_tactic(f)} ({len(window)} chars; raw text kept in restricted evidence)",
        "publisher": publisher,
        "transformations": ["exact", "nfkc", "casefold", "zero_width", "html_entities", "whitespace", "leet", "base64_bounded"],
    }


def _tactic(f: dict) -> str:
    dev = f["requested_deviations"][0] if f["requested_deviations"] else "unknown"
    who = "claimed authority" if f["claimed_authority"] else "embedded request"
    return {"new_recipient": f"{who} asking the agent to send data to a new recipient",
            "deletion": f"{who} asking the agent to delete files",
            "payment": f"{who} asking the agent to make a payment",
            "credential_disclosure": f"{who} asking the agent to disclose credentials"}.get(dev, who)


def match_chunk(chunk: dict, patterns: list[dict], channel: str) -> list[dict]:
    """Return per-sentence match decisions for one chunk against accepted patterns."""
    text = chunk["text"]
    flags = chunk.get("flags", {})
    results = []
    for s, e, sent in sentences(text):
        ex = exact_hash(sent.strip())
        nviews = {"sha256:" + sha256_hex(v.encode("utf-8")) for v in views_for_matching(sent)}
        f = None
        for p in patterns:
            body = p["body"]
            if channel not in body.get("channels", []) and body.get("channels"):
                continue
            internal = [d.lower() for d in body.get("exceptions", {}).get("internal_domains", [])]
            if internal:
                emails = re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", sent)
                if emails and all(any(e.lower() == d or e.lower().endswith("." + d) for d in internal) for e in emails):
                    continue
            level = None
            if ex in body.get("exact_hashes", []):
                level = "exact"
            elif nviews & set(body.get("normalized_hashes", [])):
                level = "normalized"
            else:
                f = f or features(text[max(0, s - 200): e])
                fam = body.get("family", {})
                dev_ok = fam.get("requested_deviation") in f["requested_deviations"]
                auth_ok = (not fam.get("claimed_authority")) or f["claimed_authority"]
                addr_ok = (not fam.get("addressed_to_agent")) or f["addressed_to_agent"]
                sent_f = features(sent)
                if dev_ok and fam.get("requested_deviation") in sent_f["requested_deviations"]:
                    level = "family" if (auth_ok and addr_ok) else "family_partial"
            if not level:
                continue
            quoted = quoted_context(text, s, e, flags)
            if level == "family_partial" or quoted:
                disposition = "flagged"
                reason = "quoted or explanatory context" if quoted else "family predicates not fully satisfied"
            else:
                disposition = "quarantined"
                reason = f"{level} match"
            results.append({"start": s, "end": e, "pattern_ref": p["ref"], "level": level, "disposition": disposition,
                            "reason": reason, "quoted": quoted})
            break
    return results
