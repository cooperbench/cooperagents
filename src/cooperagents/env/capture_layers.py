"""Select directly captured changes; publication requires a complete composition audit."""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath


def _name(name):
    path = PurePosixPath(name)
    if name in {"", ".", ".."} or str(path) != name or path.is_absolute() or any(p in (".", "..") for p in path.parts):
        raise ValueError(f"Unsafe path: {name!r}")
    if any(p.startswith(".wh.") for p in path.parts):
        raise ValueError(f"Reserved overlay filename: {name}")
    return path


def _index(index):
    entries = index["entries"]
    groups = {name: (name,) for name, row in entries.items() if row["type"] == "file"}
    for name, row in entries.items():
        path = _name(name)
        if row["type"] not in {"file", "dir", "symlink"}:
            raise ValueError(f"Unsupported object: {name}")
        for parent in path.parents:
            if str(parent) != "." and entries.get(str(parent), {}).get("type") != "dir":
                raise ValueError(f"Missing or non-directory ancestor: {name}")
    seen = set()
    for group in index["hardlink_groups"]:
        members = tuple(sorted(group))
        if len(members) < 2 or len(set(members)) != len(members):
            raise ValueError("Invalid hardlink group")
        for name in members:
            if name not in groups or name in seen or entries[name] != entries[members[0]]:
                raise ValueError(f"Inconsistent hardlink group: {members}")
            seen.add(name)
            groups[name] = members
    return entries, groups


def _comparable(row):
    return row


def plan(base_index, target_index):
    """Plan effective changes, target hardlink closure, ancestors and minimal whiteouts."""
    base, base_groups = _index(base_index)
    target, target_groups = _index(target_index)
    reasons = {}

    def select(name, reason):
        reasons.setdefault(name, set()).add(reason)

    surviving_files = set(target_groups)
    surviving_base_groups = {group: tuple(name for name in group if name in surviving_files) for group in set(base_groups.values())}
    for name, row in target.items():
        old = base.get(name)
        if old is None:
            select(name, "added")
        elif row["type"] != old["type"]:
            select(name, "type")
        elif _comparable(row) != _comparable(old):
            changed = {key for key in row.keys() | old.keys() if row.get(key) != old.get(key)}
            if changed & {"size", "sha256", "target"}:
                select(name, "content_or_target")
            if changed - {"size", "sha256", "target"}:
                select(name, "metadata")
        if row["type"] == "file" and name in base_groups and surviving_base_groups[base_groups[name]] != target_groups[name]:
            select(name, "hardlink_topology")
    for name in list(reasons):
        if name in target_groups:
            for member in target_groups[name]:
                if member not in reasons:
                    select(member, "hardlink_closure")
    removed = set(base) - set(target)
    whiteouts = []
    for name in sorted(removed):
        parents = [str(p) for p in _name(name).parents if str(p) != "."]
        if any(p in removed or (p in target and target[p]["type"] != "dir") for p in parents):
            continue
        whiteouts.append(name)
    for name in [*reasons, *whiteouts]:
        for ancestor in _name(name).parents:
            parent = str(ancestor)
            if parent != ".":
                select(parent, "ancestor_metadata")
    entries = {name: dict(target[name]) for name in sorted(reasons)}
    file_groups = sorted({target_groups[name] for name in entries if name in target_groups})
    return {
        "format": "direct-layer-plan-v1",
        "entries": entries,
        "file_groups": [list(group) for group in file_groups],
        "whiteouts": whiteouts,
        "reasons": {name: sorted(value) for name, value in sorted(reasons.items())},
        "summary": {
            "base_entries": len(base),
            "target_entries": len(target),
            "selected_entries": len(entries),
            "whiteout_count": len(whiteouts),
            "selected_regular_paths": sum(len(group) for group in file_groups),
            "selected_content_bytes": sum(target[group[0]]["size"] for group in file_groups),
            "selected_regular_inodes": len(file_groups),
            "reason_counts": dict(sorted(Counter(r for values in reasons.values() for r in values).items())),
        },
    }


def select_layer(base_index: dict, target_index: dict, repo: str) -> dict:
    """Repo deletion is covered by its opaque private copy; external deletion is rejected."""
    _name(repo)
    layer = plan(base_index, target_index)

    def inside(name):
        return name == repo or name.startswith(repo + "/")

    deleted = [name for name in layer["whiteouts"] if not inside(name)]
    if deleted:
        raise ValueError("unsupported outside-repo deletion: " + repr(deleted[:20]))
    layer["entries"] = {name: row for name, row in layer["entries"].items() if not inside(name)}
    layer["whiteouts"] = []
    return layer
