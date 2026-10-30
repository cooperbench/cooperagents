"""Strict research tree audit; caller must quiesce writers for a consistent snapshot."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def _raise_walk_error(error):
    raise error


def _xattrs(path):
    if not hasattr(os, "listxattr") or not hasattr(os, "getxattr"):
        raise RuntimeError("Strict tree audit requires real xattr APIs")
    return {name: os.getxattr(path, name, follow_symlinks=False).hex() for name in sorted(os.listxattr(path, follow_symlinks=False))}


def _metadata(path, info):
    return {
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mtime_ns": info.st_mtime_ns,
        "xattrs": _xattrs(path),
    }


def inventory(root):
    root = Path(root)
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("Audit root must be a real directory, not a symlink")
    root_metadata = {**_metadata(root, root_info), "type": "dir"}
    entries, groups, content = {}, {}, {}
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        for name in sorted([*dirs, *files]):
            path = Path(parent) / name
            info = path.lstat()
            relative = str(path.relative_to(root))
            row = _metadata(path, info)
            if stat.S_ISREG(info.st_mode):
                key = (info.st_dev, info.st_ino)
                if key not in content:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_dev, opened.st_ino, opened.st_size) != (*key, info.st_size):
                            raise RuntimeError(f"File changed while opening: {relative}")
                        content[key] = hashlib.file_digest(stream, "sha256").hexdigest()
                groups.setdefault(key, []).append(relative)
                row.update(type="file", size=info.st_size, sha256=content[key])
            elif stat.S_ISLNK(info.st_mode):
                row.update(type="symlink", target=os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                row["type"] = "dir"
            else:
                raise ValueError(f"Unsupported filesystem object: {relative}")
            entries[relative] = row
    return {
        "entries": entries,
        "hardlink_groups": sorted(sorted(group) for group in groups.values() if len(group) > 1),
        "regular_inodes": len(content),
        "root_metadata": root_metadata,
    }
