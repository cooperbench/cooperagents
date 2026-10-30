"""Full composition audit performed before a direct capture can be published."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

from cooperagents.env.published_audit import inventory
from cooperagents.published import PublicationError, VerifiedPublication


def compare(expected: dict, actual: dict) -> None:
    if expected["entries"].keys() != actual["entries"].keys() or expected["hardlink_groups"] != actual["hardlink_groups"]:
        raise PublicationError("capture composition paths/hardlinks mismatch")
    for name, row in [(".", expected["root_metadata"]), *expected["entries"].items()]:
        observed = actual["root_metadata"] if name == "." else actual["entries"][name]
        for key in row.keys() | observed.keys():
            a, b = row.get(key), observed.get(key)
            if key == "xattrs":
                b = {k: v for k, v in b.items() if not k.startswith("user.overlay.")}
            if a != b:
                raise PublicationError(f"capture composition mismatch: {name}: {key}")


def hold(config: dict) -> None:
    from cooperagents.env.published_holder import _base_mount, _mount_overlay
    from cooperagents.env.published_layers import prepare_layers

    root = Path(config["root"])
    bundle = Path(config["bundle"])
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise PublicationError("invalid private capture audit root")
    receipt = json.loads((bundle / "receipt.json").read_bytes())
    if config["base_proof"].get("runtime_qualification") != receipt["runtime_qualification"]:
        raise PublicationError("capture base runtime qualification mismatch")
    state = json.loads((bundle / "checkpoint/state.json").read_bytes())
    from cooperagents.env.published_capture import _reference

    publication = VerifiedPublication(
        _reference(
            bundle, receipt["manifest_sha256"], Path(state["runtime"]["image"]), state["runtime"]["image_sha256"], publication_hash="0" * 64
        ),
        bundle,
        bundle / "checkpoint",
        state,
        receipt,
        {},
    )
    prepare_layers(publication, root)
    helper = _base_mount(root, config["base_fd"], config["base_proof"], state["runtime"]["image_sha256"])
    try:
        _mount_overlay(root)
        expected = json.load(gzip.open(bundle / "target-index.json.gz"))
        compare(expected, inventory(root / "fs"))
        for i, row in enumerate(receipt["prepared_mounts"]):
            compare(json.load(gzip.open(bundle / row["tree"])), inventory(root / f"mount-{i}"))
        print(json.dumps(dict(status="ready", paths=len(expected["entries"]), mounts=len(receipt["prepared_mounts"]))), flush=True)
        sys.stdin.buffer.read(1)
    finally:
        helper.terminate()
        try:
            helper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait()
