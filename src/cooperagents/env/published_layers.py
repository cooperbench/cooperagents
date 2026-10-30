"""Materialize authenticated layers into one rollout's private directories."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath

from cooperagents.checkpoint import extract_tree, restore_tree, sha256
from cooperagents.published import V3, PublicationError, VerifiedPublication


def relative_name(value: str) -> Path:
    name = PurePosixPath(value)
    if name.is_absolute() or not name.parts or ".." in name.parts or str(name) != value or value == ".":
        raise PublicationError("unsafe private layer path")
    return Path(value)


def metadata(path: Path, row: dict) -> None:
    info = path.lstat()
    if (info.st_uid, info.st_gid) != (row["uid"], row["gid"]):
        os.chown(path, row["uid"], row["gid"], follow_symlinks=False)
    if row["type"] != "symlink":
        path.chmod(row["mode"])
    elif row["mode"] != stat.S_IMODE(info.st_mode):
        raise PublicationError("unsupported symlink mode")
    for name, value in row.get("xattrs", {}).items():
        os.setxattr(path, name, bytes.fromhex(value), follow_symlinks=False)
    os.utime(path, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False)


def private_archive(source: Path, destination: Path, expected: str) -> Path:
    if source.is_symlink() or not source.is_file():
        raise PublicationError("layer must be a regular file")
    shutil.copyfile(source, destination)
    if sha256(destination) != expected:
        raise PublicationError("private layer copy identity mismatch")
    return destination


def extract_layer(archive: Path, destination: Path) -> None:
    # Reuse upstream path/link/special-file validation before any layer is mounted.
    extract_tree(archive, destination)
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            path = destination / member.name
            for key, value in member.pax_headers.items():
                if key.startswith("SCHILY.xattr."):
                    os.setxattr(path, key.removeprefix("SCHILY.xattr."), value.encode("utf-8", "surrogateescape"), follow_symlinks=False)
            if member.issym():
                os.utime(path, ns=(int(float(member.mtime) * 1_000_000_000),) * 2, follow_symlinks=False)


def private_links(upper: Path, groups: list, repo: str) -> None:
    seen = set()
    for group in groups:
        names = [relative_name(name) for name in group]
        if len(names) < 2 or len(set(names)) != len(names) or seen.intersection(names):
            raise PublicationError("invalid private hardlink group")
        seen.update(names)
        inside = [name for name in names if name.is_relative_to(repo)]
        outside = [name for name in names if name not in inside]
        if not inside or not outside:
            raise PublicationError("private group must cross repo boundary")
        source = upper / inside[0]
        if source.is_symlink() or not source.is_file() or any(not os.path.samefile(source, upper / name) for name in inside):
            raise PublicationError("private repo hardlink topology mismatch")
        for name in outside:
            _alias(source, upper, name)


def _alias(source: Path, upper: Path, name: Path) -> None:
    for parent in reversed(name.parents):
        path = upper / parent
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise PublicationError("private alias crosses non-directory")
        path.mkdir(exist_ok=True)
    target = upper / name
    if target.exists() or target.is_symlink():
        raise PublicationError("private alias unexpectedly exists")
    os.link(source, target)


def prepare_layers(publication: VerifiedPublication, root: Path) -> None:
    receipt, bundle = publication.receipt, publication.root
    repo = relative_name(receipt["repo"])
    if str(repo) != publication.state["repo_path"].lstrip("/"):
        raise PublicationError("private repo differs from checkpoint")
    (root / "upper").mkdir()
    delta = private_archive(bundle / "delta.tar", root / "delta.tar", receipt["delta_sha256"])
    repo_tar = private_archive(bundle / "repo.tar", root / "repo.tar", receipt["repo_sha256"])
    extract_layer(delta, root / "lower")
    private = root / "upper" / repo
    private.parent.mkdir(parents=True, exist_ok=True)
    extract_layer(repo_tar, private)
    os.setxattr(private, "user.overlay.opaque", b"y")
    _parents(root / "upper", receipt["repo_parent_metadata"])
    private_links(root / "upper", receipt.get("private_link_groups", []), str(repo))
    _parents(root / "upper", receipt.get("private_parent_metadata", {}))
    with gzip.open(bundle / "target-index.json.gz") as stream:
        target_root = json.load(stream)["root_metadata"]
    metadata(root / "upper", target_root)
    prepare_mounts(publication, root)


def _parents(upper: Path, rows: dict) -> None:
    for name, row in rows.items():
        path = upper / relative_name(name)
        if path.is_symlink() or not path.is_dir():
            raise PublicationError("private parent metadata target missing/non-directory")
        metadata(path, row)


def prepare_mounts(publication: VerifiedPublication, root: Path) -> None:
    mounts = publication.state["runtime"]["mounts"]
    _mount_targets(mounts)
    empty = root / "empty"
    empty.mkdir()
    if publication.reference.format != V3:
        for i, record in enumerate(mounts):
            restore_tree(publication.checkpoint, record, empty, root / f"mount-{i}")
        return
    records = publication.receipt["prepared_mounts"]
    if len(records) != len(mounts):
        raise PublicationError("prepared mount count mismatch")
    for i, (record, mount) in enumerate(zip(records, mounts, strict=True)):
        _prepared_mount(publication.root, root, i, record, mount)


def _mount_targets(mounts: list) -> None:
    targets = [m["target"] for m in mounts]
    if len(set(targets)) != len(targets) or any(t not in {"/cbshared", "/coordination"} for t in targets):
        raise PublicationError("unsupported/duplicate mount target")
    if any(
        m.get("base") != "empty" or type(m["readonly"]) is not bool or m["readonly"] != (m["target"] == "/coordination") for m in mounts
    ):
        raise PublicationError("unsupported mount base/access")


def _prepared_mount(bundle: Path, root: Path, i: int, row: dict, mount: dict) -> None:
    keys = {"target", "readonly", "archive", "archive_sha256", "archive_bytes", "tree", "tree_sha256"}
    if set(row) != keys or row["target"] != mount["target"] or type(row["readonly"]) is not bool or row["readonly"] != mount["readonly"]:
        raise PublicationError("prepared mount identity mismatch")
    if row["archive"] != f"mounts/mount-{i}.tar" or row["tree"] != f"mounts/mount-{i}.tree.json.gz":
        raise PublicationError("unsafe prepared mount names")
    source = bundle / row["archive"]
    if type(row["archive_bytes"]) is not int or source.stat().st_size != row["archive_bytes"]:
        raise PublicationError("prepared mount size mismatch")
    archive = private_archive(source, root / f"prepared-mount-{i}.tar", row["archive_sha256"])
    if sha256(bundle / row["tree"]) != row["tree_sha256"]:
        raise PublicationError("prepared mount tree identity mismatch")
    extract_layer(archive, root / f"mount-{i}")
