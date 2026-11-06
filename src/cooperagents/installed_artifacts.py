"""Host-installed source measurements. Never use a worker-supplied manifest as proof."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

STORE = Path("/opt/polar-repair-assets")
RECEIPT = ".measured.json"


@dataclass(frozen=True)
class InstalledArtifacts:
    root: Path
    files: dict
    binding: list

    def measured(self, path: Path) -> tuple[str, int]:
        if ".." in path.parts:
            raise ValueError("unsafe installed path")
        name = path.relative_to(self.root).as_posix()
        row = self.files[name]
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or row["bytes"] != info.st_size:
            raise ValueError("invalid installed file")
        return row["sha256"], info.st_size


def open_installed(kind: str, manifest_sha256: str, *, _binding: list | None = None) -> InstalledArtifacts:
    if kind not in {"pub", "prepared-sqfs"} or not re.fullmatch("[0-9a-f]{64}", manifest_sha256):
        raise ValueError("invalid installed identity")
    if _binding is None and Path("/proc/self/uid_map").read_text().split() != ["0", "0", "4294967295"]:
        raise ValueError("installed artifact admission requires original user namespace")
    root = STORE / kind / manifest_sha256
    for p in (*reversed(root.parents), root, root / RECEIPT):
        st = p.lstat()
        expected = stat.S_ISREG if p.name == RECEIPT else stat.S_ISDIR
        if not expected(st.st_mode) or st.st_mode & 0o022 or (_binding is None and st.st_uid != 0):
            raise ValueError("unsafe installed artifact ancestry")
    data = (root / RECEIPT).read_bytes()
    st = root.stat()
    binding = [st.st_dev, st.st_ino, hashlib.sha256(data).hexdigest()]
    if _binding is not None and binding != _binding:
        raise ValueError("installed artifact holder binding mismatch")
    doc = json.loads(data)
    if (doc["schema"], doc["kind"], doc["manifest_sha256"]) != (1, kind, manifest_sha256):
        raise ValueError("incorrect installed receipt")
    return InstalledArtifacts(root, doc["files"], binding)


def publication(path: Path, binding: list | None = None) -> InstalledArtifacts | None:
    if not path.is_relative_to(STORE):
        if binding is not None:
            raise ValueError("unexpected installed holder binding")
        return None
    kind, pin, *_ = path.relative_to(STORE).parts
    if kind != "pub" or ".." in path.parts:
        raise ValueError("invalid installed publication")
    return open_installed(kind, pin, _binding=binding)


_active: ContextVar[InstalledArtifacts | None] = ContextVar("installed_publication", default=None)


@contextmanager
def admission(path: Path, binding: list | None = None):
    current = _active.get()
    if current is not None and path.is_relative_to(current.root) and binding is None:
        yield current
        return
    installed = publication(path, binding)
    token = _active.set(installed)
    try:
        yield installed
    finally:
        _active.reset(token)


def measured(path: Path, fallback):
    if not path.is_relative_to(STORE) or path.name in {"publication.json", "manifest.json"}:
        return fallback(path)
    current = _active.get()
    return (current or publication(path)).measured(path)[0]
