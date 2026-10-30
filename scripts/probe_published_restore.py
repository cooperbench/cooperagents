"""Model-free runtime qualification of the public repair restore entrypoints.

The catalog is authenticated input, never code. Run on the recorded compute node.
The first command timing includes publication validation, namespace creation,
private layers, Git checks, and Apptainer execution. Base admission is separate.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path

from cooperagents.env.image_base import ImageBase
from cooperagents.published import load_publications
from cooperagents.published_restore import restore_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result: dict = dict(state="running", reads=[], bases={}, training_eligible=False, repair_validated=False)
    bases = {}
    try:
        rows = load_publications(args.catalog, expected_sha256=args.sha256)
        for row in rows:
            if row.image_sha256 not in bases:
                base = ImageBase.prepare(Path(row.image), row.image_sha256, args.scratch)
                bases[row.image_sha256] = base
                result["bases"][row.image_sha256] = base.proof
            base = bases[row.image_sha256]
            handoff = base.handoff(args.scratch / "handoff")
            with ImageBase.from_environment(row.image_sha256, handoff.environment) as borrowed:
                before = time.monotonic()
                env = restore_environment(row, Path(row.publication.path) / "checkpoint", scratch=args.scratch, image_base=borrowed)
                root = env.root
                try:
                    command = env.execute(
                        'python3 -c "import os,sys; print(os.getcwd()); print(sys.executable)" && git diff --check', timeout=60
                    )
                    if command.exit_code != 0 and "conflict marker" not in command.stdout:
                        raise AssertionError(command)
                    # Different arms must never see a former rollout's private writes.
                    marker = env.execute("test ! -e /tmp/ca-public-restore-private && echo PRIVATE", timeout=30)
                    assert marker.exit_code == 0 and "PRIVATE" in marker.stdout
                    assert env.execute("touch /tmp/ca-public-restore-private", timeout=30).exit_code == 0
                    result["reads"].append(
                        dict(
                            id=row.id,
                            version=row.version,
                            seconds=time.monotonic() - before,
                            metrics=env.restore_metrics,
                            command=dict(code=command.exit_code, output=command.stdout),
                        )
                    )
                finally:
                    env.cleanup()
                assert not root.exists(), root
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        result["state"] = "complete"
        return 0
    except BaseException as exc:
        result.update(state="failed", error=repr(exc), traceback=traceback.format_exc())
        return 1
    finally:
        for base in bases.values():
            base.close()
        result["max_rss_kib"] = __import__("resource").getrusage(0).ru_maxrss
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
