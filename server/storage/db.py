"""SQLite access layer with explicit migrations.

* WAL mode, foreign keys, bounded busy timeout, synchronous=FULL for the journal.
* BEGIN IMMEDIATE for short claims and compare-and-swap transitions.
* Never hold a transaction while waiting on a model or remote tool: callers
  keep `with db.tx():` blocks free of I/O other than SQLite and local fsyncs.

The repository boundary (this class plus the object store) is what a later
PostgreSQL/remote-object-store deployment would replace.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..canonical import utcnow

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class StorageUnavailable(RuntimeError):
    """Raised when durable storage cannot be used; mutating actions fail closed."""


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.fail_injection: str | None = None  # test hook: "unavailable"
        self.migrate()

    # -- connections -----------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        if self.fail_injection == "unavailable":
            raise StorageUnavailable("durable storage unavailable (injected)")
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA busy_timeout = 10000")
            c.execute("PRAGMA synchronous = FULL")
            self._local.conn = c
            self._local.depth = 0
            with self._lock:
                self._all.append(c)
        return c

    def close(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:
                    pass
            self._all.clear()
        self._local = threading.local()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self.conn()
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield c
            finally:
                self._local.depth -= 1
            return
        c.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            self._local.depth = 0

    # -- helpers ---------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.conn().execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, tuple(params)).fetchall()]

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.conn().execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    # -- migrations ------------------------------------------------------
    def migrate(self) -> list[int]:
        c = self.conn()
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        applied = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
        done = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(f.name.split("_", 1)[0])
            if version in applied:
                continue
            sql = f.read_text(encoding="utf-8")
            c.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(sql):
                    c.execute(stmt)
                c.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)", (version, f.name, utcnow()))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            done.append(version)
        return done

    def schema_version(self) -> int:
        return self.scalar("SELECT COALESCE(MAX(version),0) FROM schema_migrations") or 0

    def integrity_check(self) -> str:
        return self.scalar("PRAGMA integrity_check")


def _split_sql(sql: str) -> list[str]:
    """Split a migration file into statements, keeping trigger bodies intact."""
    out, buf, in_trigger = [], [], False
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") and not buf:
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper == "END;":
                out.append("\n".join(buf))
                buf, in_trigger = [], False
        elif stripped.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf))
    return [s for s in out if s.strip()]
