"""Filesystem checkpoints at worker delivery and repair boundaries."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import shlex
import shutil
import tarfile
from copy import copy
from datetime import UTC, datetime
from pathlib import Path

from cooperagents.env.base import Environment, ExecResult
from cooperagents.patching import strip_for_submission, strip_test_sections
from cooperagents.trajectory import _json_default


def archive_tree(source: Path, destination: Path) -> None:
    with tarfile.open(destination, "w:gz", compresslevel=1, dereference=False) as archive:
        archive.add(source, arcname=".")


def remove_tree(root: Path) -> None:
    """Remove owned read-only rootfs directories without following symlinks."""
    import os

    root.chmod(root.stat().st_mode | 0o700)
    for directory, children, _ in os.walk(root, followlinks=False):
        for name in children:
            child = Path(directory) / name
            if not child.is_symlink():
                child.chmod(child.stat().st_mode | 0o700)
    shutil.rmtree(root)


def save_tree_delta(source: Path, base: Path, destination: Path, name: str) -> dict:
    from cooperagents.checkpoint_delta import pack_tree

    pack_tree(source, base, destination / name)
    return dict(format="checkpoint-tar-delta-v1", archive=f"{name}/filesystem.json.gz", payload=f"{name}/payload.tar.gz")


def restore_tree(checkpoint: Path, record: dict, base: Path, destination: Path) -> None:
    """Read old full archives, or verify a delta against its immutable base."""
    import tempfile

    from cooperagents.checkpoint_delta import restore

    if record.get("format") == "checkpoint-tar-delta-v1":
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".restore-") as temporary:
            archive = Path(temporary) / "rootfs.tar.gz"
            restore((checkpoint / record["archive"]).parent, base, archive)
            extract_tree(archive, destination)
    elif "format" not in record:
        extract_tree(checkpoint / record["archive"], destination)
    else:
        raise ValueError("Unsupported checkpoint filesystem format")


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def complete_output(result: ExecResult, operation: str, *, allow_failure: bool = False) -> str:
    """Control operations must not accept failed or incomplete command output."""
    if (result.exit_code and not allow_failure) or any(line.startswith("[output truncated after ") for line in result.stdout.splitlines()):
        raise RuntimeError(f"{operation} failed or returned truncated output (exit {result.exit_code})")
    return result.stdout


def extract_tree(archive_path: Path, destination: Path) -> None:
    """Restore rootfs links without permitting archive writes through those links."""
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        links = {Path(m.name) for m in members if m.issym() or m.islnk()}
        names = set()
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or name in names:
                raise ValueError("Unsafe or duplicate archive member")
            names.add(name)
            if any(parent in links for parent in name.parents):
                raise ValueError("Archive member writes through a link")
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise ValueError("Unsupported archive member")
            if member.islnk():
                target = Path(member.linkname)
                if target.is_absolute() or ".." in target.parts or target in links:
                    raise ValueError("Unsafe archive hardlink")
        # Absolute system symlinks are valid rootfs state. Install them last,
        # after all file writes, so they cannot redirect extraction to the host.
        cleaned = []
        for member in sorted(members, key=lambda m: (m.issym(), m.islnk())):
            clean = copy(member)
            clean.uid = clean.gid = None
            clean.uname = clean.gname = None
            cleaned.append(clean)
        # extractall defers directory chmod until children have been written.
        # Real rootfs trees contain read-only directories, unlike a task repo.
        archive.extractall(destination, members=cleaned, filter="fully_trusted")


def preview_patch(env: Environment) -> str:
    """Collect a binary-capable patch without changing the real Git index/stashes."""
    base = shlex.quote(getattr(env, "_base_commit", "") or "HEAD")
    result = env.execute(
        "index=$(mktemp) || exit; "
        "original=$(git rev-parse --git-path index) && "
        '{ if [ -f "$original" ]; then cp "$original" "$index"; else rm -f "$index"; fi; } && '
        'GIT_INDEX_FILE="$index" git add -A && '
        f'GIT_INDEX_FILE="$index" git diff --cached --binary {base}; '
        'rc=$?; rm -f "$index" "$index.lock"; exit "$rc"',
        timeout=180,
    )
    if result.exit_code or any(line.startswith("[output truncated after ") for line in result.stdout.splitlines()):
        raise RuntimeError(f"Cannot record complete checkpoint patch (exit {result.exit_code})")
    return result.stdout


def save_checkpoint(env, destination: Path, *, metadata: dict, collect_patch=None, trace=None, repair_input=None) -> str:
    """Write manifest last; a failed or partial capture never appears complete.

    Worker captures precede collect_patch, which may stage files or pop stashes.
    Repair captures use a temporary Git index to leave the handoff unchanged.
    """
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    run_config = destination.parent / "run.json"
    if run_config.exists():
        (destination / "run.json").write_bytes(run_config.read_bytes())
    if repair_input is not None:
        (destination / "repair-input.json").write_text(repair_input.model_dump_json(indent=2))
    started = datetime.now(UTC).isoformat()
    if trace:
        trace("checkpoint_start", path=str(destination), boundary=metadata["boundary"])
    git = {}
    for name, command in {
        "head": "git rev-parse HEAD",
        "status": "git status --porcelain=v1 --untracked-files=all",
        "stashes": "git stash list",
    }.items():
        result = env.execute(f"GIT_OPTIONAL_LOCKS=0 {command}")
        complete_output(result, f"Checkpoint Git {name}")
        git[name] = dataclasses.asdict(result)
    runtime = env.checkpoint(destination)
    raw, source = collect_patch() if collect_patch else (preview_patch(env), "working_tree")
    if any(line.startswith("[output truncated after ") for line in raw.splitlines()):
        raise RuntimeError("Collected worker patch was truncated")
    patch = strip_test_sections(raw)
    (destination / "raw.patch").write_text(raw)
    (destination / "integration.patch").write_text(patch)
    (destination / "submission.patch").write_text(strip_for_submission(patch))
    state = dict(
        version=2,
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
        repo_path=env.repo_path,
        base_commit=getattr(env, "_base_commit", ""),
        runtime=runtime,
        git=git,
        patch_source=source,
        metadata=metadata,
        process_state_saved=False,
    )
    (destination / "state.json").write_text(json.dumps(state, default=_json_default, ensure_ascii=False, indent=2))
    files = {str(p.relative_to(destination)): dict(bytes=p.stat().st_size, sha256=sha256(p)) for p in destination.rglob("*") if p.is_file()}
    (destination / "manifest.json").write_text(json.dumps(dict(version=2, files=files), indent=2))
    if trace:
        trace("checkpoint_end", path=str(destination), boundary=metadata["boundary"], files=files, patch_source=source)
    return patch


def verify_checkpoint(path: Path) -> dict:
    manifest = json.loads((path / "manifest.json").read_text())
    if (
        manifest["version"] not in {1, 2}
        or not {"state.json", "raw.patch", "integration.patch", "submission.patch"} <= manifest["files"].keys()
    ):
        raise ValueError("Incomplete or unsupported checkpoint manifest")
    for name, record in manifest["files"].items():
        if not name or Path(name).is_absolute() or ".." in Path(name).parts or name in {".", ".."}:
            raise ValueError("Invalid checkpoint filename")
        file = path / name
        if file.is_symlink() or not file.resolve().is_relative_to(path.resolve()):
            raise ValueError("Checkpoint file escapes snapshot")
        if file.stat().st_size != record["bytes"] or sha256(file) != record["sha256"]:
            raise ValueError(f"Checkpoint checksum mismatch: {name}")
    state = json.loads((path / "state.json").read_text())
    records = [state["runtime"], *state["runtime"].get("mounts", [])]
    archives = [record[key] for record in records for key in ("archive", "payload") if key in record]
    if state["version"] != manifest["version"] or not set(archives) <= manifest["files"].keys():
        raise ValueError("Missing checkpoint filesystem archive")
    if state["version"] == 2 and any(
        record.get("format") != "checkpoint-tar-delta-v1"
        or "payload" not in record
        or Path(record["archive"]).name != "filesystem.json.gz"
        or record["payload"] != str(Path(record["archive"]).with_name("payload.tar.gz"))
        for record in records
    ):
        raise ValueError("New checkpoints require filesystem deltas")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="checkpoint directory, or pair's checkpoints directory")
    args = parser.parse_args()
    paths = [args.path] if (args.path / "state.json").exists() else sorted(p for p in args.path.iterdir() if p.is_dir())
    if not paths:
        raise ValueError("No checkpoints found")
    for path in paths:
        state = verify_checkpoint(path)
        print(json.dumps(dict(path=str(path), boundary=state["metadata"]["boundary"], runtime=state["runtime"]["backend"])))


if __name__ == "__main__":
    main()
