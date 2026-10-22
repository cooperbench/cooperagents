"""Filesystem deltas promoted from the verified 44-checkpoint offline converter.

Store the complete target tar inventory and only changed file bytes. Unchanged
base files are checked during restore; absent target entries represent deletions.
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import tarfile
import tempfile
import threading
from pathlib import Path, PurePosixPath

from cooperagents.checkpoint import remove_tree, sha256


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {name}")
    return str(path)


def cached_base(root: Path, identity: str, build) -> Path:
    """Expand an immutable image once per cache, publishing only complete builds."""
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Invalid base content identity")
    destination = root / identity
    with (root / f"{identity}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not destination.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".base-", dir=root))
            try:
                build(temporary / "fs")
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    remove_tree(temporary)
        if destination.is_symlink() or (destination / "fs").is_symlink() or not (destination / "fs").is_dir():
            raise ValueError("Invalid expanded base cache")
    return destination / "fs"


def pack_tree(source: Path, base: Path, destination: Path) -> dict:
    """Stream tar metadata/content into the existing packer; never save a full tar."""
    read_fd, write_fd = os.pipe()
    errors = []

    def produce():
        try:
            with os.fdopen(write_fd, "wb") as stream, tarfile.open(fileobj=stream, mode="w|", dereference=False) as archive:
                archive.add(source, arcname=".")
        except BaseException as error:
            errors.append(error)

    producer = threading.Thread(target=produce)
    producer.start()
    try:
        with os.fdopen(read_fd, "rb") as stream:
            result = pack_stream(stream, base, destination)
    finally:
        producer.join()
    if errors:
        raise errors[0]
    return result


def base_file(base, name):
    path = base / safe_name(name)
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Base path escapes root: {name}")
    return path


def metadata(member):
    keys = ("name", "mode", "uid", "gid", "size", "mtime", "linkname", "uname", "gname", "devmajor", "devminor", "pax_headers")
    return {**{key: getattr(member, key) for key in keys}, "type": member.type.decode("ascii")}


def tarinfo(record):
    info = tarfile.TarInfo(record["name"])
    for key, value in record.items():
        setattr(info, key, value.encode("ascii") if key == "type" else value)
    return info


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.hash.update(data)
        return data


class HashWriter:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def write(self, data):
        self.hash.update(data)
        return self.stream.write(data)


class NullWriter:
    def write(self, data):
        return len(data)


def pack_stream(raw, base, destination, scratch=None, *, canonicalize=False):
    """Save all tar metadata, but only compress bytes that differ from the base."""
    base, destination = map(Path, (base, destination))
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    changed = 0
    with tarfile.open(destination / "payload.tar.gz", "w:gz", compresslevel=1) as payload:
        original = HashReader(raw)
        with tarfile.open(fileobj=original, mode="r|") as archive:
            for member in archive:
                safe_name(member.name)
                entry = {"tar": metadata(member), "payload": False}
                if member.isreg():
                    try:
                        path = base_file(base, member.name)
                    except ValueError:
                        # A replaced absolute base symlink must not read host files.
                        path = None
                    candidate = path is not None and path.is_file() and not path.is_symlink()
                    same = candidate and path.stat().st_size == member.size
                    with contextlib.ExitStack() as stack:
                        old = stack.enter_context(path.open("rb")) if same else None
                        content = archive.extractfile(member)
                        digest = hashlib.sha256()
                        offset = 0
                        delta = None
                        while data := content.read(1024 * 1024):
                            digest.update(data)
                            equal = same and old.read(len(data)) == data
                            if not equal and delta is None:
                                delta = stack.enter_context(tempfile.TemporaryFile(dir=scratch))
                                if offset:
                                    old.seek(0)
                                    remaining = offset
                                    while remaining:
                                        prefix = old.read(min(remaining, 1024 * 1024))
                                        if not prefix:
                                            raise ValueError("Truncated base file")
                                        delta.write(prefix)
                                        remaining -= len(prefix)
                                same = False
                            if delta is not None:
                                delta.write(data)
                            offset += len(data)
                        if offset != member.size:
                            raise ValueError("Truncated checkpoint file")
                        # Empty newly added files also require a payload entry.
                        if not same and delta is None:
                            delta = stack.enter_context(tempfile.TemporaryFile(dir=scratch))
                        if delta is not None:
                            delta.seek(0)
                            payload.addfile(member, delta)
                            entry["payload"] = True
                            changed += 1
                        entry["sha256"] = digest.hexdigest()
                records.append(entry)
        while original.read(1024 * 1024):
            pass
    manifest = {
        "format": "checkpoint-tar-delta-v1",
        "entries": records,
        "target_uncompressed_tar_sha256": original.hash.hexdigest(),
        "payload_sha256": sha256(destination / "payload.tar.gz"),
        "changed_regular_files": changed,
    }
    with gzip.open(destination / "filesystem.json.gz", "wt", compresslevel=1) as f:
        json.dump(manifest, f, separators=(",", ":"), ensure_ascii=False)
    if canonicalize:
        # Docker's Go tar writer uses different headers/end padding than Python.
        # Compute the reconstructible PAX identity before publishing the checkpoint.
        result = _restore(destination, base, destination / "unused", verify_only=True, verify_tar=False)
        manifest["source_uncompressed_tar_sha256"] = manifest["target_uncompressed_tar_sha256"]
        manifest["target_uncompressed_tar_sha256"] = result["uncompressed_tar_sha256"]
        with gzip.open(destination / "filesystem.json.gz", "wt", compresslevel=1) as f:
            json.dump(manifest, f, separators=(",", ":"), ensure_ascii=False)
    return manifest


def restore(delta, base, output, verify_only=False):
    """Reconstruct a full rootfs tar; absent target entries implement deletions."""
    return _restore(delta, base, output, verify_only=verify_only)


def _restore(delta, base, output, *, verify_only, verify_tar=True):
    delta, base, output = map(Path, (delta, base, output))
    if not verify_only and output.exists():
        raise FileExistsError(output)
    with gzip.open(delta / "filesystem.json.gz", "rt") as f:
        manifest = json.load(f)
    if manifest["format"] != "checkpoint-tar-delta-v1":
        raise ValueError("Unknown delta format")
    if sha256(delta / "payload.tar.gz") != manifest["payload_sha256"]:
        raise ValueError("Delta payload checksum mismatch")
    temporary = output.with_name(output.name + ".partial")
    if not verify_only and temporary.exists():
        raise FileExistsError(temporary)
    try:
        sink = contextlib.nullcontext(NullWriter()) if verify_only else gzip.open(temporary, "xb", compresslevel=1)
        with sink as raw, tarfile.open(delta / "payload.tar.gz", "r|gz") as payload:
            writer = HashWriter(raw)
            with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for record in manifest["entries"]:
                    member = tarinfo(record["tar"])
                    safe_name(member.name)
                    with contextlib.ExitStack() as stack:
                        if member.isreg():
                            if record["payload"]:
                                saved = payload.next()
                                if saved is None or metadata(saved) != metadata(member):
                                    raise ValueError("Delta entry mismatch")
                                file = payload.extractfile(saved)
                            else:
                                path = base_file(base, member.name)
                                if path.is_symlink() or not path.is_file() or path.stat().st_size != member.size:
                                    raise ValueError(f"Missing base file: {member.name}")
                                file = stack.enter_context(path.open("rb"))
                            reader = HashReader(file)
                            archive.addfile(member, reader)
                            if reader.hash.hexdigest() != record["sha256"]:
                                raise ValueError(f"Restored file checksum mismatch: {member.name}")
                        else:
                            archive.addfile(member)
                if payload.next() is not None:
                    raise ValueError("Unexpected extra delta entry")
            if verify_tar and writer.hash.hexdigest() != manifest["target_uncompressed_tar_sha256"]:
                raise ValueError("Restored tar does not exactly match original uncompressed tar")
        if not verify_only:
            os.replace(temporary, output)
    except BaseException:
        if not verify_only:
            temporary.unlink(missing_ok=True)
        raise
    return {
        "verified": True,
        "entries": len(manifest["entries"]),
        "uncompressed_tar_sha256": writer.hash.hexdigest(),
        "archive_sha256": None if verify_only else sha256(output),
        "verify_only": verify_only,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore",))
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new gzip tar path on scratch")
    args = parser.parse_args()
    print(json.dumps(restore(args.delta, args.base, args.output)))
