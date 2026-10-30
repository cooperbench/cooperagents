"""Container adapter for the pinned CooperBench evaluator (scoring stays upstream)."""

from __future__ import annotations

import re
import shlex
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.image_base import ImageBase


@dataclass
class EvalResult:
    returncode: int
    output: str

    def stdout_read(self) -> str:
        return self.output

    def stderr_read(self) -> str:
        return ""


class EvalSandbox:
    def __init__(self, env: ApptainerEnv, timeout: int) -> None:
        self.env, self.timeout = env, timeout

    def exec(self, *args: str) -> EvalResult:
        result = self.env.execute(shlex.join(args), timeout=self.timeout)
        return EvalResult(result.exit_code, result.stdout)

    def terminate(self) -> None:
        self.env.cleanup()


class ApptainerEvalBackend:
    def __init__(
        self, images: dict[str, str], scratch: str, *, image_base: ImageBase | None = None, shared_image: tuple[str, str] | None = None
    ) -> None:
        if (image_base is None) != (shared_image is None):
            raise ValueError("shared evaluator requires image identity and capability together")
        if shared_image is not None and (
            not Path(shared_image[0]).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", shared_image[1])
        ):
            raise ValueError("invalid shared evaluator image identity")
        self.images, self.scratch = images, scratch
        self.image_base, self.shared_image = image_base, shared_image

    def create_sandbox(self, image: str, timeout: int = 600, workdir: str = "/workspace") -> EvalSandbox:
        path = Path(self.images[image])
        if self.image_base is None:
            env = ApptainerEnv(str(path), scratch=self.scratch)
            env.repo_path = workdir
        else:
            assert self.shared_image is not None
            expected_path, checksum = self.shared_image
            if path.resolve() != Path(expected_path).resolve():
                raise ValueError("evaluator requested a different image from shared base")
            env = ApptainerEnv.from_image_base(path, checksum, scratch=Path(self.scratch), base=self.image_base, workdir=workdir)
        return EvalSandbox(env, timeout)


def main() -> None:
    import argparse
    import json
    import os
    from pathlib import Path

    from cooperbench.eval import evaluate
    from cooperbench.eval.runs import discover_runs

    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--logs", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--shared-image-path")
    parser.add_argument("--shared-image-sha256")
    args = parser.parse_args()
    runs = discover_runs(run_name=args.run, logs_dir=args.logs, dataset_dir=args.dataset)
    if not runs:
        raise RuntimeError("Official evaluator discovered no runs")
    if bool(args.shared_image_path) != bool(args.shared_image_sha256):
        raise ValueError("complete shared evaluator image identity required")
    if os.environ.get("COOPER_REPAIR_BASE_CHANNEL") and not args.shared_image_path:
        raise ValueError("shared image controller requires an explicit evaluation image identity")
    with ExitStack() as stack:
        base = None
        identity = None
        if args.shared_image_path:
            identity = (args.shared_image_path, args.shared_image_sha256)
            base = ImageBase.from_environment(args.shared_image_sha256)
            if base is None:
                raise ValueError("shared evaluator requires the prepared image controller")
            stack.enter_context(base)
            base.validate(args.shared_image_sha256)
        backend = ApptainerEvalBackend(
            json.loads(Path(os.environ["COOPER_IMAGE_MANIFEST"]).read_text()),
            os.environ["COOPER_SCRATCH"],
            image_base=base,
            shared_image=identity,
        )
        evaluate(args.run, backend=backend, dataset_dir=args.dataset, logs_dir=args.logs, concurrency=args.concurrency, force=True)
        for run in runs:
            result = json.loads((Path(run["log_dir"]) / "eval.json").read_text())
            if result.get("error") or not all(result.get(f"feature{i}") for i in (1, 2)):
                raise RuntimeError(f"Official evaluation failed: {result.get('error')}")


if __name__ == "__main__":
    main()
