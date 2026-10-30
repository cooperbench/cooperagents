"""Checked, direct tar emission; no reconstructed full-root archive."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import tarfile
from pathlib import Path

from cooperagents.env.published_audit import inventory
from cooperagents.trajectory import _json_default


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=_json_default) + "\n")


def write_index(path: Path, value: dict) -> None:
    with gzip.open(path, "wt") as stream:
        json.dump(value, stream, sort_keys=True)


def signatures(root: Path) -> dict:
    """Detect concurrent edits independently of content hashing (atime is excluded)."""
    result = {}
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        result[str(path.relative_to(root))] = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return result


def check_xattrs(index: dict) -> None:
    for name, row in [(".", index["root_metadata"]), *index["entries"].items()]:
        for key in row["xattrs"]:
            if not key.startswith("user.") or key.startswith("user.overlay."):
                raise ValueError(f"unsupported captured xattr: {name}: {key}")


def archive(source: Path, destination: Path, index: dict, names: set[str] | None = None) -> None:
    """Serialize indexed bytes and inode topology, and verify each file while reading."""
    selected = set(index["entries"]) if names is None else names
    rows = {".": index["root_metadata"], **{n: index["entries"][n] for n in sorted(selected)}}
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT, dereference=False) as stream:
        for name, row in rows.items():
            path = source / name
            member = stream.gettarinfo(str(path), arcname=name)
            member.pax_headers.update(
                {"SCHILY.xattr." + k: bytes.fromhex(v).decode("utf-8", "surrogateescape") for k, v in row["xattrs"].items()}
            )
            if member.isfile():
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as data:
                    if not stat.S_ISREG(os.fstat(fd).st_mode) or hashlib.file_digest(data, "sha256").hexdigest() != row["sha256"]:
                        raise ValueError("capture source content changed: " + name)
                    data.seek(0)
                    stream.addfile(member, data)
            else:
                stream.addfile(member)


def prepared_mounts(mounts: dict[str, Path], bundle: Path) -> tuple[list, list]:
    from cooperagents.checkpoint import sha256

    (bundle / "mounts").mkdir()
    records, runtime = [], []
    for i, (target, source) in enumerate(mounts.items()):
        if target not in {"/cbshared", "/coordination"}:
            raise ValueError("unsupported checkpoint mount: " + target)
        tree = inventory(source)
        check_xattrs(tree)
        tar = f"mounts/mount-{i}.tar"
        idx = f"mounts/mount-{i}.tree.json.gz"
        archive(source, bundle / tar, tree)
        write_index(bundle / idx, tree)
        records.append(
            dict(
                target=target,
                readonly=target == "/coordination",
                archive=tar,
                archive_sha256=sha256(bundle / tar),
                archive_bytes=(bundle / tar).stat().st_size,
                tree=idx,
                tree_sha256=sha256(bundle / idx),
            )
        )
        runtime.append(dict(target=target, readonly=target == "/coordination", base="empty"))
    return records, runtime
