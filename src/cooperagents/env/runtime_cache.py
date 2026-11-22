"""Optional per-(repo, task) uv artifacts, separate from qualified image bytes.

Builders run outside episodes. Consumers reuse a sealed SQFS descriptor for the
life of an environment pool; neither the artifact path nor a writable shared
cache is exposed to an agent. The caller pins the receipt digest at admission.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from cooperagents.checkpoint import remove_tree, sha256
from cooperagents.env.image_base import _copy_descriptor

LOG = logging.getLogger(__name__)
TARGET = "/home/agent/.cache/uv"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RuntimeCacheIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    repo: str = Field(min_length=1)
    task_id: int
    image_sha256: Digest
    image_manifest_sha256: Digest
    pyproject_sha256: Digest
    uv_lock_sha256: Digest

    @classmethod
    def from_files(
        cls, *, repo: str, task_id: int, image_sha256: str, image_manifest: Path, pyproject: Path, uv_lock: Path
    ) -> RuntimeCacheIdentity:
        return cls(repo=repo, task_id=task_id, image_sha256=image_sha256, image_manifest_sha256=sha256(image_manifest),
                   pyproject_sha256=sha256(pyproject), uv_lock_sha256=sha256(uv_lock))

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CacheReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    identity: RuntimeCacheIdentity
    sqfs_sha256: Digest
    created_at: str
    size_bytes: int = Field(gt=0)
    warmup_seconds: float = Field(ge=0, allow_inf_nan=False)


class RuntimeCacheArtifact(BaseModel):
    """Trusted admission: retain this locator/digest, never discover it from an episode."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    directory: Path
    identity: RuntimeCacheIdentity
    receipt_sha256: Digest

    @classmethod
    def build(
        cls, store: Path, identity: RuntimeCacheIdentity, warm: Callable[[Path], None]
    ) -> RuntimeCacheArtifact:
        """Serialize builders per key and atomically publish once.

        warm(destination) must populate ONLY a uv cache directory using the
        exact image/manifest and dependency files in identity. It must finish
        and release writers before returning. No venv enters the artifact.
        """
        store.mkdir(parents=True, exist_ok=True)
        target = store / identity.key
        with (store / (identity.key + ".lock")).open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if target.exists():
                return cls._existing(target, identity)
            work = Path(tempfile.mkdtemp(prefix=".building-", dir=store))
            try:
                cache = work / "cache"
                cache.mkdir()
                started = time.monotonic()
                warm(cache)
                elapsed = time.monotonic() - started
                # uv uses relative links from wheel indexes into archive-v0.
                # Preserve those, but never capture a link to a venv/host path.
                for path in cache.rglob("*"):
                    if path.is_symlink():
                        if Path(os.readlink(path)).is_absolute() or not path.resolve().is_relative_to(cache):
                            raise ValueError("runtime cache link escapes its cache directory")
                    elif not (path.is_file() or path.is_dir()):
                        raise ValueError("runtime cache contains a special file")
                publish = work / "publish"
                publish.mkdir()
                packed = publish / "uv.sqfs"
                subprocess.run(
                    ["mksquashfs", str(cache), str(packed), "-noappend", "-processors", "2", "-quiet",
                     "-comp", "zstd", "-mem", "256M"], capture_output=True, check=True, timeout=1800,
                )
                receipt = CacheReceipt(identity=identity, sqfs_sha256=sha256(packed),
                                       created_at=datetime.now(UTC).isoformat(), size_bytes=packed.stat().st_size,
                                       warmup_seconds=elapsed)
                (publish / "receipt.json").write_text(receipt.model_dump_json(indent=2) + "\n")
                for path in publish.iterdir():
                    path.chmod(0o444)
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
                # All payload bytes are complete and read-only before rename.
                # macOS requires write permission on the directory being moved.
                os.rename(publish, target)
                target.chmod(0o555)
                with _directory_fd(store) as descriptor:
                    os.fsync(descriptor)
                return cls._existing(target, identity)
            finally:
                remove_tree(work)

    @classmethod
    def _existing(cls, directory: Path, identity: RuntimeCacheIdentity) -> RuntimeCacheArtifact:
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o222:
            raise ValueError("invalid cache artifact directory")
        receipt_file, packed = directory / "receipt.json", directory / "uv.sqfs"
        if any(p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o222 for p in (receipt_file, packed)):
            raise ValueError("cache artifact is missing or writable")
        receipt = CacheReceipt.model_validate_json(receipt_file.read_bytes())
        if receipt.identity != identity or receipt.size_bytes != packed.stat().st_size or receipt.sqfs_sha256 != sha256(packed):
            raise ValueError("existing cache artifact identity mismatch")
        return cls(directory=directory.resolve(), identity=identity, receipt_sha256=sha256(receipt_file))

    def open(self) -> RuntimeCache:
        """Authenticate once per environment pool; failures remain valid cold runs."""
        descriptor = -1
        try:
            artifact = self._existing(self.directory, self.identity)
            if artifact.receipt_sha256 != self.receipt_sha256:
                raise ValueError("cache receipt digest mismatch")
            receipt_bytes = (self.directory / "receipt.json").read_bytes()
            if hashlib.sha256(receipt_bytes).hexdigest() != self.receipt_sha256:
                raise ValueError("cache receipt changed during admission")
            receipt = CacheReceipt.model_validate_json(receipt_bytes)
            create = getattr(os, "memfd_create", None)
            if create is None:
                raise ValueError("uv runtime cache requires Linux sealed memfd support")
            descriptor = create("cooperagents-uv-cache", getattr(os, "MFD_ALLOW_SEALING", 0))
            _copy_descriptor(self.directory / "uv.sqfs", descriptor)
            fcntl.fcntl(descriptor, _constant("F_ADD_SEALS"), _seals())
            if sha256(Path(f"/proc/self/fd/{descriptor}")) != receipt.sqfs_sha256:
                raise ValueError("sealed cache copy digest mismatch")
            info = os.fstat(descriptor)
            proof = dict(receipt=receipt.model_dump(), fd_identity=[info.st_dev, info.st_ino, info.st_size])
            return RuntimeCache(descriptor, proof, identity=self.identity)
        except (OSError, ValueError, AttributeError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            reason = f"{type(exc).__name__}: {exc}"
            LOG.warning("uv runtime cache fallback: %s", reason)
            return RuntimeCache(-1, dict(status="fallback", reason=reason), identity=self.identity)


# Use a context manager for directory fsync without leaking a descriptor.
@contextmanager
def _directory_fd(path: Path) -> Iterator[int]:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _constant(name: str) -> int:
    # Linux-only fcntl symbols are absent from macOS Python/type stubs.
    return getattr(fcntl, name)


def _seals() -> int:
    return _constant("F_SEAL_SEAL") | _constant("F_SEAL_WRITE") | _constant("F_SEAL_GROW") | _constant("F_SEAL_SHRINK")


class RuntimeCache:
    """Borrowed by episodes, closed by its pool owner (like ImageBase)."""

    def __init__(self, descriptor: int, proof: dict, *, identity: RuntimeCacheIdentity | None = None) -> None:
        self.descriptor, self.proof = descriptor, proof
        self.identity = identity

    def transport(self) -> dict:
        try:
            if self.descriptor < 0:
                raise ValueError(self.proof.get("reason", "cache closed or unavailable"))
            info = os.fstat(self.descriptor)
            if self.proof["fd_identity"] != [info.st_dev, info.st_ino, info.st_size]:
                raise ValueError("cache descriptor identity mismatch")
            if fcntl.fcntl(self.descriptor, _constant("F_GET_SEALS")) & _seals() != _seals():
                raise ValueError("cache descriptor is writable")
            return dict(descriptor=self.descriptor, proof=self.proof)
        except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            LOG.warning("uv runtime cache fallback: %s", reason)
            return dict(descriptor=-1, proof=dict(reason=reason))

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> RuntimeCache:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def validate_transport(config: dict, *, repo: str, task_id: int, image: str, filesystem: Path, repo_path: str) -> CacheReceipt:
    proof, descriptor = config["proof"], config["descriptor"]
    if descriptor < 0:
        raise ValueError(proof.get("reason", "cache unavailable"))
    receipt = CacheReceipt.model_validate(proof["receipt"])
    identity = receipt.identity
    if (identity.repo, identity.task_id, identity.image_sha256) != (repo, task_id, image):
        raise ValueError("runtime cache repo/task/image mismatch")
    for name, expected in [("pyproject.toml", identity.pyproject_sha256), ("uv.lock", identity.uv_lock_sha256)]:
        path = filesystem / repo_path.lstrip("/") / name
        if path.is_symlink() or not path.resolve().is_relative_to(filesystem) or sha256(path) != expected:
            raise ValueError("runtime cache dependency mismatch: " + name)
    info = os.fstat(descriptor)
    if proof["fd_identity"] != [info.st_dev, info.st_ino, info.st_size] or info.st_size != receipt.size_bytes:
        raise ValueError("runtime cache descriptor identity mismatch")
    if fcntl.fcntl(descriptor, _constant("F_GET_SEALS")) & _seals() != _seals():
        raise ValueError("runtime cache descriptor is writable")
    return receipt
