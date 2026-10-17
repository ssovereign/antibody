"""Project-scoped content-addressed object store.

Objects are written to a temporary file, flushed and fsynced, hash-verified,
then atomically renamed into place (Technical Design: Save at a stable boundary).
Restricted objects (raw hostile evidence) live in a separate namespace so a
version can reference evidence without exposing its bytes to every viewer.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from ..canonical import canonical_bytes, content_ref, sha256_hex


class ObjectIntegrityError(RuntimeError):
    pass


class ObjectStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.crash_after_tmp: bool = False  # test hook for crash-point recovery

    def _dir(self, project_id: str, restricted: bool) -> Path:
        d = self.root / project_id / ("restricted" if restricted else "objects")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _tmpdir(self, project_id: str) -> Path:
        d = self.root / project_id / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, project_id: str, ref: str, restricted: bool = False) -> Path:
        digest = ref.split(":", 1)[1]
        return self._dir(project_id, restricted) / digest[:2] / digest

    def put(self, project_id: str, data: bytes, restricted: bool = False) -> str:
        ref = content_ref(data)
        final = self.path_for(project_id, ref, restricted)
        if final.exists():
            return ref
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmpdir(project_id) / f"{uuid.uuid4().hex}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if self.crash_after_tmp:
            raise RuntimeError("simulated crash before object rename")
        with open(tmp, "rb") as fh:
            if content_ref(fh.read()) != ref:
                tmp.unlink(missing_ok=True)
                raise ObjectIntegrityError("hash mismatch after write")
        os.replace(tmp, final)
        return ref

    def put_json(self, project_id: str, value, restricted: bool = False) -> str:
        return self.put(project_id, canonical_bytes(value), restricted)

    def get(self, project_id: str, ref: str, restricted: bool = False) -> bytes:
        p = self.path_for(project_id, ref, restricted)
        if not p.exists():
            raise FileNotFoundError(f"object {ref} unavailable")
        data = p.read_bytes()
        if "sha256:" + sha256_hex(data) != ref:
            raise ObjectIntegrityError(f"object {ref} failed verification")
        return data

    def get_json(self, project_id: str, ref: str, restricted: bool = False):
        import json
        return json.loads(self.get(project_id, ref, restricted).decode("utf-8"))

    def exists(self, project_id: str, ref: str, restricted: bool = False) -> bool:
        return self.path_for(project_id, ref, restricted).exists()

    def purge(self, project_id: str, ref: str, restricted: bool = True) -> bool:
        p = self.path_for(project_id, ref, restricted)
        if p.exists():
            p.unlink()
            return True
        return False

    def all_refs(self, project_id: str, restricted: bool = False) -> list[tuple[str, Path]]:
        d = self._dir(project_id, restricted)
        return [("sha256:" + p.name, p) for p in d.glob("*/*") if p.is_file()]

    def cleanup_tmp(self, project_id: str, older_than_seconds: int) -> int:
        n = 0
        now = time.time()
        for p in self._tmpdir(project_id).glob("*.tmp"):
            if now - p.stat().st_mtime >= older_than_seconds:
                p.unlink(missing_ok=True)
                n += 1
        return n
