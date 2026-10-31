"""One authenticated immutable image capability, reusable across repair rollouts."""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from cooperagents.checkpoint import remove_tree, sha256


class ImageBase:
    """Own a sealed descriptor, not a cached writable checkpoint or container."""

    def __init__(self, descriptor: int, proof: dict[str, object]) -> None:
        self.descriptor, self.proof = descriptor, proof

    @classmethod
    def prepare(cls, image: Path, expected_sha256: str, scratch: Path) -> ImageBase:
        if not hasattr(os, "memfd_create"):
            raise RuntimeError("Published repair requires Linux sealed memfd support")
        started = time.monotonic()
        if image.is_symlink() or not image.is_file() or sha256(image) != expected_sha256:
            raise ValueError("Base SIF identity mismatch")
        scratch.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="ca-image-", dir=scratch))
        os.chown(work, -1, os.getgid())
        work.chmod(0o700)
        descriptor = -1
        try:
            packed = _pack_image(image, work)
            from cooperagents.env.published_audit import inventory

            native_inventory = work / "native-index.json.gz"
            with gzip.open(native_inventory, "wt") as stream:
                json.dump(inventory(work / "fs"), stream)
            descriptor = os.memfd_create("cooperagents-verified-base", os.MFD_ALLOW_SEALING)
            _copy_descriptor(packed, descriptor)
            seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
            checksum = sha256(packed)
            if sha256(Path(f"/proc/self/fd/{descriptor}")) != checksum:
                raise ValueError("Immutable image base copy mismatch")
            stat = os.fstat(descriptor)
            proof = dict(
                image_sha256=expected_sha256,
                base_sha256=checksum,
                seals=seals,
                fd_identity=[stat.st_dev, stat.st_ino, stat.st_size],
                preparation_seconds=time.monotonic() - started,
            )
            from cooperagents.published_profiles import qualification_identity

            proof["runtime_qualification"] = _runtime()
            proof["reader_qualification_sha256"] = qualification_identity()
            config = work / "base-audit.json"
            config.write_text(
                json.dumps(
                    dict(
                        operation="audit-base",
                        root=str(work),
                        base_fd=descriptor,
                        base_proof=proof,
                        native_inventory=str(native_inventory),
                        native_inventory_sha256=sha256(native_inventory),
                    )
                )
            )
            proof["audit"] = _audit_base(config, descriptor)
            proof["preparation_seconds"] = time.monotonic() - started
            return cls(descriptor, proof)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        finally:
            remove_tree(work)

    def validate(self, expected_image: str) -> None:
        """Validate a live borrowed capability before a fresh image mount."""
        from cooperagents.published_profiles import validate_reader

        validate_reader()
        _verify_handoff(self.descriptor, self.proof, expected_image)

    def handoff(self, directory: Path) -> ImageHandoff:
        if self.descriptor < 0:
            raise ValueError("image base already closed")
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o700)
            os.chown(directory, -1, os.getgid())
            directory.chmod(0o700)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise ValueError("image handoff requires a private owned directory")
        data = json.dumps(self.proof, sort_keys=True).encode()
        target = directory / f"base-{self.proof['image_sha256']}-{cast(list[int], self.proof['fd_identity'])[1]}.json"
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
            partial = Path(stream.name)
            stream.write(data)
        try:
            partial.chmod(0o400)
            try:
                os.link(partial, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != data or target.stat().st_mode & 0o222:
                    raise ValueError("existing image handoff metadata is corrupt") from None
        finally:
            partial.unlink()
        return ImageHandoff(self.descriptor, target, sha256(target))

    @classmethod
    def from_environment(cls, expected_image: str, environment: Mapping[str, str] | None = None) -> ImageBase | None:
        settings = os.environ if environment is None else environment
        channel = settings.get("COOPER_REPAIR_BASE_CHANNEL")
        if channel is not None:
            if any(settings.get(key) is not None for key in _HANDOFF_KEYS):
                raise ValueError("ambiguous image capability transport")
            from cooperagents.env.image_channel import receive_image

            descriptor, proof = receive_image(channel)
            try:
                _verify_handoff(descriptor, proof, expected_image)
                return cls(descriptor, proof)
            except BaseException:
                os.close(descriptor)
                raise
        return cls._from_inherited(expected_image, settings)

    @classmethod
    def _from_inherited(cls, expected_image: str, settings: Mapping[str, str]) -> ImageBase | None:
        values = [settings.get(k) for k in _HANDOFF_KEYS]
        if all(v is None for v in values):
            return None
        descriptor_text, filename, checksum = values
        if descriptor_text is None or filename is None or checksum is None:
            raise ValueError("incomplete shared image capability; no fallback")
        proof_file = Path(filename)
        if proof_file.is_symlink() or proof_file.stat().st_mode & 0o222 or sha256(proof_file) != checksum:
            raise ValueError("shared image proof identity mismatch")
        proof = json.loads(proof_file.read_bytes())
        descriptor = int(descriptor_text)
        _verify_handoff(descriptor, proof, expected_image)
        return cls(os.dup(descriptor), proof)

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> ImageBase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _pack_image(image: Path, work: Path) -> Path:
    fs, packed = work / "fs", work / "base.sqfs"
    subprocess.run(["apptainer", "build", "--fix-perms", "--sandbox", str(fs), str(image)], capture_output=True, check=True, timeout=1800)
    subprocess.run(
        ["mksquashfs", str(fs), str(packed), "-noappend", "-processors", "2", "-quiet", "-comp", "zstd", "-mem", "256M"],
        capture_output=True,
        check=True,
        timeout=1800,
    )
    return packed


def _copy_descriptor(source: Path, descriptor: int) -> None:
    with source.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            view = memoryview(block)
            while view:
                view = view[os.write(descriptor, view) :]


def _runtime() -> dict:
    from cooperagents.published import runtime_qualification

    return runtime_qualification()


def _audit_base(config: Path, descriptor: int) -> dict:
    command = [sys.executable, "-m", "cooperagents.env.published_holder", "--launch", str(os.getpid()), str(config)]
    result = subprocess.run(
        command, input="", capture_output=True, text=True, check=True, pass_fds=(descriptor,), start_new_session=True, timeout=1800
    )
    record = json.loads(result.stdout)
    if record.get("status") != "ready" or record.get("operation") != "audit-base":
        raise ValueError("Image base filesystem qualification failed")
    return record["audit"]


class ImageHandoff:
    """Immutable descriptor + authenticated private metadata for an episode worker."""

    def __init__(self, descriptor: int, proof_file: Path, checksum: str) -> None:
        self.descriptor, self.proof_file, self.checksum = descriptor, proof_file, checksum

    @property
    def environment(self) -> dict[str, str]:
        return dict(zip(_HANDOFF_KEYS, [str(self.descriptor), str(self.proof_file), self.checksum], strict=True))

    @property
    def pass_fds(self) -> tuple[int]:
        return (self.descriptor,)


_HANDOFF_KEYS = ("COOPER_REPAIR_BASE_FD", "COOPER_REPAIR_BASE_PROOF", "COOPER_REPAIR_BASE_PROOF_SHA256")


def _verify_handoff(descriptor: int, proof: dict[str, object], expected_image: str) -> None:
    from cooperagents.published_profiles import qualification_identity

    st = os.fstat(descriptor)
    seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
    if proof["image_sha256"] != expected_image or proof["fd_identity"] != [st.st_dev, st.st_ino, st.st_size]:
        raise ValueError("shared image capability identity mismatch")
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & seals != seals:
        raise ValueError("shared image capability is writable")
    if proof.get("reader_qualification_sha256") != qualification_identity() or proof.get("runtime_qualification") != _runtime():
        raise ValueError("shared image capability uses an unqualified reader/runtime")
    audit = proof.get("audit")
    if not isinstance(audit, dict) or not audit.get("paths"):
        raise ValueError("shared image capability lacks native filesystem qualification")
