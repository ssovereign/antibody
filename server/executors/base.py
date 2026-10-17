"""Executor contracts. Executors own tool credentials and repeat resource
checks at use time; they accept only a bound decision from the gateway."""
from __future__ import annotations

from dataclasses import dataclass, field


class ToolError(Exception):
    """A known, structured failure. `code` and `family` are trusted wrapper metadata;
    `message` may contain untrusted text (e.g. stderr) and is treated as data."""

    def __init__(self, code: str, family: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code, self.family, self.message, self.detail = code, family, message, detail or {}


class ToolOutcomeUnknown(Exception):
    """The executor may have completed before a timeout or crash."""


class ResourceViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class ExecContext:
    project_id: str
    run_id: str
    branch_id: str
    workspace_dir: str
    environment: dict
    operation_id: str
    decision_id: str
    file_roots: list[str] = field(default_factory=lambda: ["workspace://branch/current"])
    task: dict = field(default_factory=dict)
