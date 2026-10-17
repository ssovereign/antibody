"""Canonical JSON and hashing.

Documented serializer (Technical Design: Hash semantics):
  * UTF-8 encoding, keys sorted lexicographically by code point,
  * no insignificant whitespace (separators "," and ":"),
  * non-ASCII characters emitted literally (ensure_ascii=False),
  * floats are rejected so identity never depends on float formatting;
    money and ratios are carried as decimal strings,
  * list order is significant (parent order matters for merges).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any


class CanonicalError(ValueError):
    pass


def _check(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        raise CanonicalError(f"float not allowed in canonical content at {path}")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError(f"non-string key at {path}")
            _check(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalError(f"unsupported type {type(value).__name__} at {path}")


def canonical_bytes(value: Any) -> bytes:
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_ref(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


def hash_value(value: Any) -> str:
    return content_ref(canonical_bytes(value))


def dumps(value: Any) -> str:
    """Non-canonical JSON for storage columns (floats allowed)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
