"""Run a node-local difficulty controller with one authenticated shared image.

Slurm children receive the capability over a private UNIX socket, never by
assuming that srun inherits the controller's file descriptors. Image admission
cost is recorded separately and is paid once per controller allocation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from cooperagents.env.image_base import ImageBase
from cooperagents.env.image_channel import ImageChannel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a controller command is required")
    with ImageBase.prepare(args.image, args.sha256, args.scratch) as base:
        with args.receipt.open("x") as stream:
            json.dump(base.proof, stream, indent=2)
        with tempfile.TemporaryDirectory(prefix="ca-img-", dir="/tmp") as directory:
            with ImageChannel(base.descriptor, base.proof, Path(directory)) as channel:
                environment = dict(os.environ, COOPER_REPAIR_BASE_CHANNEL=str(channel.path))
                return subprocess.run(command, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
