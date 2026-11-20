"""Explicit deployment gate; normal repair/eval cache failures still run cold.

Run under the actual service UID, before enabling the capability in a campaign.
A successful exit certifies real FUSE/overlay/private-write behavior on THIS host
and identity. It is not a throughput qualification or a training smoke.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import uuid
from pathlib import Path

from cooperagents.checkpoint import complete_output
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.image_base import ImageBase
from cooperagents.env.runtime_cache import TARGET, RuntimeCacheArtifact


class PreflightFailure(ValueError):
    def __init__(self, record: dict) -> None:
        self.record = record
        super().__init__("cache preflight requires a hit: " + record["reason"])


def preflight(artifact: RuntimeCacheArtifact, image: Path, scratch: Path, *, expected_uid: int) -> dict:
    if os.geteuid() != expected_uid:
        raise ValueError("preflight must run under the actual campaign service UID")
    identity = artifact.identity
    marker = ".cooperagents-preflight-" + uuid.uuid4().hex
    records = []
    with artifact.open() as cache:
        config = cache.transport()
        if config["descriptor"] < 0:
            raise ValueError("cache admission failed: " + config["proof"]["reason"])
        with ImageBase.prepare(image, identity.image_sha256, scratch) as base:
            for _ in range(2):
                env = ApptainerEnv.from_image_base(
                    image, identity.image_sha256, scratch=scratch, base=base, runtime_cache=cache,
                    cache_task=(identity.repo, identity.task_id),
                )
                try:
                    record = env.runtime_cache_receipt
                    if record["status"] != "hit":
                        raise PreflightFailure(record)
                    checks = record["mount_checks"]
                    if not all(checks.get(name) is True for name in ["helper_exists", "squashfuse_mount", "overlay_mount"]):
                        raise ValueError("cache preflight lacks verified helper/FUSE/overlay checks")
                    probe = shlex.quote(TARGET + "/" + marker)
                    complete_output(env.execute(
                        f'test "$UV_CACHE_DIR" = {shlex.quote(TARGET)} && '
                        f'test "$(uv cache dir)" = {shlex.quote(TARGET)} && '
                        f"test ! -e {probe} && printf private > {probe}"
                    ), "Cache preflight container bind, uv environment and private write")
                    if (env.root / "runtime-cache/upper" / marker).read_text() != "private":
                        raise ValueError("preflight write did not reach this sandbox's private upper")
                    records.append(record)
                finally:
                    env.cleanup()
                if env.root.exists():
                    raise ValueError("cache preflight leaked the sandbox upper/work")
    return dict(status="hit", host=socket.gethostname(), uid=os.geteuid(), gid=os.getegid(),
                key=identity.key, sandboxes=records, private_write_isolated=True, cleanup_verified=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True, help="trusted pinned RuntimeCacheArtifact JSON locator")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--expected-uid", type=int, required=True, help="actual campaign service UID, never a privileged substitute")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        artifact = RuntimeCacheArtifact.model_validate_json(args.artifact.read_bytes())
        result = preflight(artifact, args.image, args.scratch, expected_uid=args.expected_uid)
    except Exception as exc:
        result = dict(status="failed", uid=os.geteuid(), gid=os.getegid(), error_type=type(exc).__name__, reason=str(exc))
        if isinstance(exc, PreflightFailure):
            result["cache"] = exc.record
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return 0 if result["status"] == "hit" else 1


if __name__ == "__main__":
    raise SystemExit(main())
