"""Container adapter for the pinned CooperBench evaluator (scoring stays upstream)."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from cooperagents.env.apptainer import ApptainerEnv


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
    def __init__(self, images: dict[str, str], scratch: str) -> None:
        self.images, self.scratch = images, scratch

    def create_sandbox(self, image: str, timeout: int = 600, workdir: str = "/workspace") -> EvalSandbox:
        env = ApptainerEnv(self.images[image], scratch=self.scratch)
        env.repo_path = workdir
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
    args = parser.parse_args()
    runs = discover_runs(run_name=args.run, logs_dir=args.logs, dataset_dir=args.dataset)
    if not runs:
        raise RuntimeError("Official evaluator discovered no runs")
    backend = ApptainerEvalBackend(json.loads(Path(os.environ["COOPER_IMAGE_MANIFEST"]).read_text()), os.environ["COOPER_SCRATCH"])
    evaluate(args.run, backend=backend, dataset_dir=args.dataset, logs_dir=args.logs, concurrency=args.concurrency, force=True)
    for run in runs:
        result = json.loads((Path(run["log_dir"]) / "eval.json").read_text())
        if result.get("error") or not all(result.get(f"feature{i}") for i in (1, 2)):
            raise RuntimeError(f"Official evaluation failed: {result.get('error')}")


if __name__ == "__main__":
    main()
