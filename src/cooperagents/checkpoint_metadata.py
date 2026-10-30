"""Authenticated admission metadata; this never qualifies filesystem payloads."""

from __future__ import annotations

import json
from pathlib import Path

from cooperagents.checkpoint import sha256


def checkpoint_metadata(path: Path, *, expected_manifest_sha256: str) -> dict:
    if sha256(path / "manifest.json") != expected_manifest_sha256:
        raise ValueError("Checkpoint metadata manifest identity mismatch")
    manifest = json.loads((path / "manifest.json").read_bytes())
    if manifest.get("version") not in {1, 2}:
        raise ValueError("Unsupported checkpoint metadata schema")
    for name in ["state.json", "repair-input.json"]:
        member = path / name
        record = manifest["files"].get(name)
        if record is None or member.is_symlink() or not member.resolve().is_relative_to(path.resolve()):
            raise ValueError("Admission requires embedded authenticated repair metadata")
        if member.stat().st_size != record["bytes"] or sha256(member) != record["sha256"]:
            raise ValueError("Checkpoint metadata checksum mismatch: " + name)
    state = json.loads((path / "state.json").read_bytes())
    if state["version"] != manifest["version"]:
        raise ValueError("Checkpoint metadata version mismatch")
    return state
