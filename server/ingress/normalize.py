"""Bounded text normalization used for chunk fingerprints and pattern matching.

Only explicitly supported transformations are applied (Technical Design:
Matching policy, rule 2): Unicode NFKC, case folding, zero-width removal,
HTML entity decoding, whitespace collapsing, a small homoglyph/leet map and
bounded base64 segment decoding.
"""
from __future__ import annotations

import base64
import binascii
import html
import re
import unicodedata

from ..canonical import sha256_hex

ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
B64_SEGMENT = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}")
MAX_B64_SEGMENTS = 4
MAX_B64_LEN = 4096
SUPPORTED_TRANSFORMS = ["nfkc", "casefold", "zero_width", "html_entities", "whitespace", "leet", "base64_bounded"]


def normalize(text: str, leet: bool = False) -> str:
    t = html.unescape(text)
    t = unicodedata.normalize("NFKC", t).translate(ZERO_WIDTH).casefold()
    if leet:
        t = t.translate(LEET)
    t = re.sub(r"[^\w@.\-:/ ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def decode_b64_segments(text: str) -> list[str]:
    out = []
    for m in list(B64_SEGMENT.finditer(text))[:MAX_B64_SEGMENTS]:
        seg = m.group(0)
        if len(seg) > MAX_B64_LEN:
            continue
        try:
            raw = base64.b64decode(seg + "=" * (-len(seg) % 4), validate=True)
            dec = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if sum(ch.isprintable() for ch in dec) / max(1, len(dec)) > 0.9:
            out.append(dec)
    return out


def norm_hash(text: str) -> str:
    return "sha256:" + sha256_hex(normalize(text).encode("utf-8"))


def exact_hash(text: str) -> str:
    return "sha256:" + sha256_hex(text.encode("utf-8"))


def views_for_matching(text: str) -> list[str]:
    """All bounded normalized views of a chunk that pattern matching may inspect."""
    views = [normalize(text), normalize(text, leet=True)]
    for dec in decode_b64_segments(text):
        views.append(normalize(dec))
    seen, out = set(), []
    for v in views:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
