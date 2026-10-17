"""Configuration contract (Technical Design: Configuration contract).

Every value can be overridden by an environment variable of the same name.
Defaults are engineering defaults, not research-derived constants; they are
recorded in each run's immutable start configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


@dataclass
class Config:
    DATABASE_PATH: str = str(ROOT / "var" / "antibody.sqlite")
    CHECKPOINT_PATH: str = str(ROOT / "var" / "checkpoints.sqlite")
    OBJECT_STORE_ROOT: str = str(ROOT / "var" / "objects")
    WORKSPACE_ROOT: str = str(ROOT / "var" / "workspaces")
    OUTBOX_ROOT: str = str(ROOT / "var" / "outbox")
    SECRETS_PATH: str = str(ROOT / "var" / "secrets.json")
    MODEL_PROVIDER: str = "deterministic"  # deterministic | anthropic | recorded
    MODEL_ID: str = "reference-planner-1"
    MODEL_CONTEXT_LIMIT: int = 32000
    AUDITOR_PROVIDER: str = "rules"  # rules | anthropic
    API_BIND_HOST: str = "127.0.0.1"
    # hosting: extra host names / browser origins allowed (comma separated in the environment)
    EXTRA_ALLOWED_HOSTS: list = field(default_factory=list)
    EXTRA_UI_ORIGINS: list = field(default_factory=list)
    # hosted accounts: GitHub sign-in (register an OAuth app; callback = PUBLIC_URL + /v1/auth/github/callback)
    PUBLIC_URL: str = ""
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    # Claude calls each project may make per day on the server's key (0 = no limit)
    CLAUDE_DAILY_CALLS_PER_PROJECT: int = 0
    API_PORT: int = 8765
    UI_ORIGIN: str = "http://127.0.0.1:5173"
    PROJECT_AUTH_MODE: str = "local"  # local (loopback bootstrap) | token
    MAX_SOURCE_BYTES: int = 512 * 1024
    MAX_RAW_BYTES: int = 2 * 1024 * 1024
    CHUNK_SIZE: int = 2000
    CHUNK_OVERLAP: int = 200
    CONTEXT_MARGIN: float = 0.10
    MAX_TOOL_STEPS: int = 30
    MAX_REPAIR_STEPS: int = 12
    MAX_RUN_COST: float = 1.00
    REPEAT_FAILURE_LIMIT: int = 2
    NO_PROGRESS_LIMIT: int = 3
    MAX_LESSONS_IN_CONTEXT: int = 3
    GUARD_TIMEOUT_SECONDS: float = 10.0
    EFFECT_LEASE_SECONDS: int = 30
    JOB_LEASE_SECONDS: int = 120
    AUTO_PROMOTE_CLASSES: list = field(default_factory=list)
    RAW_EVIDENCE_RETENTION_DAYS: int = 30
    OBJECT_GC_GRACE_SECONDS: int = 3600
    FETCH_MAX_REDIRECTS: int = 3
    FETCH_TIMEOUT_SECONDS: float = 10.0
    WORKER_THREADS: int = 2
    AUTO_RECOVERY: bool = True
    AUTO_CANDIDATES: bool = True
    PRICE_CONFIG_PATH: str = str(ROOT / "fixtures" / "price_config.json")

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        cfg = cls()
        data = os.environ.get("ANTIBODY_DATA_DIR")
        if data:  # hosting: keep all state on one mounted volume
            d = Path(data)
            for k, sub in (("DATABASE_PATH", "antibody.sqlite"), ("CHECKPOINT_PATH", "checkpoints.sqlite"), ("OBJECT_STORE_ROOT", "objects"),
                           ("WORKSPACE_ROOT", "workspaces"), ("OUTBOX_ROOT", "outbox"), ("SECRETS_PATH", "secrets.json")):
                setattr(cfg, k, str(d / sub))
        for k, v in asdict(cfg).items():
            setattr(cfg, k, _env(k, v))
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    @classmethod
    def for_root(cls, root: str | Path, **overrides) -> "Config":
        """Isolated configuration rooted in a directory (tests, evaluation sandboxes)."""
        root = Path(root)
        base = dict(
            DATABASE_PATH=str(root / "antibody.sqlite"),
            CHECKPOINT_PATH=str(root / "checkpoints.sqlite"),
            OBJECT_STORE_ROOT=str(root / "objects"),
            WORKSPACE_ROOT=str(root / "workspaces"),
            OUTBOX_ROOT=str(root / "outbox"),
            SECRETS_PATH=str(root / "secrets.json"),
        )
        base.update(overrides)
        return cls.from_env(**base)

    def public_view(self) -> dict:
        """Configuration safe to display (no paths to secrets)."""
        d = asdict(self)
        d.pop("SECRETS_PATH", None)
        return d
