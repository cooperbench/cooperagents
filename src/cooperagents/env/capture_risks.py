"""Bounded dependency evidence, never a claim that a filesystem snapshot is live-equivalent."""

from pathlib import Path


def resolve_guest(path: str, entries: dict) -> str | None:
    pending = path.split("/")
    parts: list[str] = []
    hops = 0
    while pending:
        item = pending.pop(0)
        if item in ("", "."):
            continue
        if item == "..":
            if parts:
                parts.pop()
            continue
        parts.append(item)
        row = entries.get("/" + "/".join(parts))
        if row and row["type"] == "symlink":
            hops += 1
            if hops > 40:
                return None
            target = row["target"]
            parts.pop()
            if target.startswith("/"):
                parts = []
            pending = target.split("/") + pending
    result = "/" + "/".join(parts)
    return result if result in entries else None


def assess(roots: dict[str, Path], indexes: dict[str, dict]) -> dict:
    entries = {}
    for target, index in indexes.items():
        prefix = "" if target == "rootfs" else target
        entries[prefix or "/"] = index["root_metadata"]
        entries.update({prefix + "/" + name: row for name, row in index["entries"].items()})
    findings = [
        dict(
            category="unknown",
            resource="processes/open files/sockets/external resources",
            detail="Filesystem capture cannot establish whether these are required. The caller must quiesce writers.",
        ),
        dict(
            category="contract_limit",
            resource="lower-only directories and hardlinks",
            detail="Lower directory rename can return EXDEV; copy-up can split lower hardlinks. Repo is a complete private copy.",
        ),
    ]
    for path, row in sorted(entries.items()):
        if row["type"] == "symlink" and resolve_guest(path, entries) is None:
            findings.append(
                dict(
                    category="suspected_dependency",
                    resource=path,
                    detail="Symlink target is absent from captured trees; may be runtime-provided or intentionally dangling.",
                )
            )
    for target, root in roots.items():
        for name, row in indexes[target]["entries"].items():
            if row["type"] != "file" or not row["mode"] & 0o111:
                continue
            with (root / name).open("rb") as stream:
                header = stream.read(512)
            if not header.startswith(b"#!"):
                continue
            parts = header.splitlines()[0][2:].split()
            if not parts:
                continue
            interpreter = parts[0].decode("utf-8", errors="replace")
            if interpreter.startswith("/") and resolve_guest(interpreter, entries) is None:
                findings.append(
                    dict(
                        category="suspected_dependency",
                        resource=("/" if target == "rootfs" else target + "/") + name,
                        detail="Executable interpreter is absent from captured trees: " + interpreter,
                    )
                )
    return dict(
        schema=1,
        review_status="needs_review",
        training_eligible=False,
        scope="static symlink and executable interpreter checks; not complete dynamic dependency discovery",
        findings=findings,
    )
