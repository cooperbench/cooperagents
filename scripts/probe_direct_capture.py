"""No-model direct capture regression on a real historical repair environment.

The live source is reconstructed with the legacy native reader. This checks new
capture/restore behavior, not equivalence to the unavailable historical process.
"""

import argparse
import json
import time
from pathlib import Path

from probe_official_repair import COMMAND, execute

import cooperagents
from cooperagents.checkpoint import save_checkpoint, sha256
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.image_base import ImageBase
from cooperagents.env.image_channel import ImageChannel
from cooperagents.published import captured_publication
from cooperagents.repair import load_repair_input


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    env = ApptainerEnv.from_checkpoint(args.checkpoint, scratch=args.scratch)
    try:
        inputs = load_repair_input(args.checkpoint)
        package = Path(cooperagents.__file__).parent
        inputs.source_hashes = {name: sha256(package / name) for name in inputs.source_hashes}
        # A new capture under the reviewed current runtime, with the historical task inputs.
        start = time.monotonic()
        destination = args.output / "before-integrator1"
        save_checkpoint(
            env, destination, metadata={"boundary": "repair_agent_start", "qualification": "native-reconstructed-live"}, repair_input=inputs
        )
        elapsed = time.monotonic() - start
        reference = captured_publication(destination)
        (args.output / "capture.json").write_text(json.dumps({"capture_seconds": elapsed, "publication": reference.model_dump()}, indent=2))
        with ImageBase.prepare(env.image, reference.image_sha256, args.scratch) as base:
            with ImageChannel(base.descriptor, base.proof, Path(__import__("tempfile").mkdtemp(prefix="v3-channel-"))) as channel:
                import os

                os.environ["COOPER_REPAIR_BASE_CHANNEL"] = str(channel.path)
                execute(reference, args.scratch, args.output / "official.json")
        result = env.execute(COMMAND, timeout=180)
        (args.output / "live-command.json").write_text(json.dumps({"stdout": result.stdout, "exit_code": result.exit_code}, indent=2))
        if result.exit_code:
            raise RuntimeError("live repair failed")
        from cooperagents.checkpoint import preview_patch

        (args.output / "live.patch").write_text(preview_patch(env))
    finally:
        env.cleanup()


if __name__ == "__main__":
    main()
