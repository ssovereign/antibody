"""Contained file executor.

Paths are resolved against the branch workspace and authorized file roots at
use time. Traversal, absolute paths, drive letters, alternate data streams,
symlinks and Windows reparse points (junctions) are rejected; containment is
checked on the fully resolved path, not by string prefix.
"""
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path, PurePosixPath

from .base import ExecContext, ResourceViolation, ToolError

ENV_EXPORT_FIXTURES = {
    "fixture_1": {"default_encoding": "cp1252", "description": "declared legacy-encoding export fixture"},
    "fixture_2": {"default_encoding": "utf-8", "description": "UTF-8 default export fixture"},
}


def _is_link_or_reparse(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def root_subdirs(file_roots: list[str]) -> list[str]:
    out = []
    for r in file_roots:
        rest = r[len("workspace://branch/current"):].strip("/")
        out.append(rest)
    return out


def resolve_in_workspace(workspace_dir: str, rel: str, file_roots: list[str], must_exist: bool = False) -> Path:
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise ResourceViolation("PATH_OUTSIDE_WORKSPACE", "empty or invalid path")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":") or norm.startswith("//"):
        raise ResourceViolation("PATH_OUTSIDE_WORKSPACE", "absolute paths are not permitted")
    parts = PurePosixPath(norm).parts
    if any(p in ("..",) for p in parts) or any(":" in p for p in parts):
        raise ResourceViolation("PATH_OUTSIDE_WORKSPACE", "traversal or alternate streams are not permitted")
    root = Path(workspace_dir).resolve()
    allowed = [root / s if s else root for s in root_subdirs(file_roots)]
    target = root.joinpath(*parts)
    # walk each existing component and reject links / reparse points
    cur = root
    for p in parts:
        cur = cur / p
        if _is_link_or_reparse(cur):
            raise ResourceViolation("PATH_OUTSIDE_WORKSPACE", "symbolic links and reparse points are not permitted")
    resolved = target.resolve()
    if not any(_within(resolved, a.resolve()) for a in allowed):
        raise ResourceViolation("PATH_OUTSIDE_WORKSPACE", "path resolves outside the authorized file roots")
    if must_exist and not resolved.exists():
        raise ToolError("E_NOT_FOUND", "FileNotFoundError", f"{rel} does not exist")
    return resolved


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class FileExecutor:
    def list_files(self, ctx: ExecContext, args: dict) -> dict:
        base = resolve_in_workspace(ctx.workspace_dir, args.get("path") or ".", ctx.file_roots) if args.get("path") else Path(ctx.workspace_dir).resolve()
        root = Path(ctx.workspace_dir).resolve()
        files = []
        if base.exists():
            for p in sorted(base.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    files.append(str(p.relative_to(root)).replace("\\", "/"))
        return {"status": "ok", "files": files[:500]}

    def read_file(self, ctx: ExecContext, args: dict) -> dict:
        p = resolve_in_workspace(ctx.workspace_dir, args["path"], ctx.file_roots, must_exist=True)
        data = p.read_bytes()[:200_000]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError("E_DECODE", "UnicodeDecodeError", f"{args['path']} is not valid UTF-8")
        return {"status": "ok", "path": args["path"], "text": text}

    def delete_file(self, ctx: ExecContext, args: dict) -> dict:
        p = resolve_in_workspace(ctx.workspace_dir, args["path"], ctx.file_roots, must_exist=True)
        if p.is_dir():
            raise ToolError("E_IS_DIR", "IsADirectoryError", "directories cannot be deleted with this tool")
        p.unlink()
        return {"status": "ok", "path": args["path"], "deleted": True}

    def write_report(self, ctx: ExecContext, args: dict) -> dict:
        """Export tool with a declared environment fixture.

        fixture_1 declares a legacy cp1252 default encoding; writing text that
        cp1252 cannot represent raises a real UnicodeEncodeError. This is an
        encoding fixture, not a claim about any particular operating system.
        """
        adapter = ctx.environment.get("export_adapter", "fixture_1")
        fx = ENV_EXPORT_FIXTURES.get(adapter)
        if fx is None:
            raise ToolError("E_ADAPTER", "AdapterError", f"unknown export adapter {adapter}")
        encoding = args.get("encoding") or fx["default_encoding"]
        p = resolve_in_workspace(ctx.workspace_dir, args["path"], ctx.file_roots)
        try:
            data = args["content"].encode(encoding)
        except UnicodeEncodeError as e:
            raise ToolError("E_ENCODE", "UnicodeEncodeError",
                            f"'{encoding}' codec can't encode character at position {e.start}",
                            {"encoding": encoding, "adapter": adapter, "position": e.start})
        if encoding == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            data = b"\xff\xfe" + data
        atomic_write(p, data)
        from ..canonical import content_ref
        return {"status": "ok", "path": args["path"], "bytes_written": len(data), "sha256": content_ref(data),
                "encoding": encoding, "adapter": adapter}
