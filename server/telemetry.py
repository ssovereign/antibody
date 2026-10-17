"""Structured logs and optional OpenTelemetry spans.

Logs carry references and redacted summaries only, never raw arguments or
source text. Spans use the OpenTelemetry API; without an SDK/exporter
configured they are no-ops, so tracing stays optional.
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from contextlib import contextmanager

try:  # optional dependency (installed transitively); no-op without an SDK
    from opentelemetry import trace as _otel
    _tracer = _otel.get_tracer("antibody")
except Exception:  # pragma: no cover
    _tracer = None

_logger = logging.getLogger("antibody")
if not _logger.handlers:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(h)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log_event(kind: str, **fields) -> None:
    safe = {k: v for k, v in fields.items() if k not in ("arguments", "content", "text", "raw", "body")}
    _logger.info(json.dumps({"event": kind, **safe}, default=str, ensure_ascii=False))


@contextmanager
def span(name: str, **attrs):
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if isinstance(v, (str, int, bool)):
                s.set_attribute(f"antibody.{k}", v)
        yield s


def disk_status(path: str, min_free_mb: int = 200) -> dict:
    try:
        u = shutil.disk_usage(path)
        free_mb = u.free // (1024 * 1024)
        return {"free_mb": free_mb, "ok": free_mb >= min_free_mb, "threshold_mb": min_free_mb}
    except OSError:
        return {"free_mb": None, "ok": False, "threshold_mb": min_free_mb}


def traced(name: str):
    """Decorator: wrap a function in an optional OpenTelemetry span."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with span(name):
                return fn(*a, **kw)
        return wrapper
    return deco
