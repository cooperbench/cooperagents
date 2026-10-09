"""SWE-bench Verified adapter (git substrate).

Instance = a SWE-bench Verified task (dataset ``princeton-nlp/SWE-bench_Verified``).
Each task ships a per-instance Docker image with the target repository checked
out at ``base_commit`` under ``/testbed``; the agent edits the repository and the
submission is its git diff. Scoring is the official SWE-bench harness
(``swebench.harness.run_evaluation``), which applies the predicted patch to a
fresh image and runs the task's FAIL_TO_PASS / PASS_TO_PASS tests.

Both declarative facts are ``None`` (there is no single build artifact and no
reference binary); the real score comes from :meth:`evaluate`, like CooperBench
and Terminal-Bench.

Per-instance image names vary by registry (Docker Hub ``swebench/*``, Epoch
``ghcr.io/epoch-research/*``, locally built ``sweb.eval.*``), so the image name
is a configurable template (``SWEBENCH_IMAGE_TEMPLATE``) rather than a baked
convention. The dataset id (``SWEBENCH_DATASET``), a local dataset path
(``SWEBENCH_DATA``), the submitted model name (``SWEBENCH_MODEL_NAME``), and the
evaluator worker count (``SWEBENCH_MAX_WORKERS``) are also configurable.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cooperagents.adapters.base import BenchmarkAdapter

WORKSPACE = "/testbed"
_DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"
_DEFAULT_IMAGE_TEMPLATE = "swebench/sweb.eval.x86_64.{id}:latest"


def _dataset() -> str:
    return os.getenv("SWEBENCH_DATASET", _DEFAULT_DATASET)


def _image_template() -> str:
    return os.getenv("SWEBENCH_IMAGE_TEMPLATE", _DEFAULT_IMAGE_TEMPLATE)


def _model_name() -> str:
    return os.getenv("SWEBENCH_MODEL_NAME", "cooperagents")


@dataclass
class SWEBenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str


def _load_local(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _load_hf(split: str) -> list[dict[str, Any]]:
    from datasets import load_dataset

    return [dict(r) for r in load_dataset(_dataset(), split=split)]


class SWEBenchAdapter(BenchmarkAdapter):
    name = "swebench"
    build_artifact = None
    reference_binary = None

    def instances(self, split: str = "test") -> list[SWEBenchInstance]:
        """Load SWE-bench Verified tasks. Uses a local JSONL at ``SWEBENCH_DATA``
        when set (offline), otherwise the HuggingFace dataset."""
        local = os.getenv("SWEBENCH_DATA")
        rows = _load_local(local) if local else _load_hf(split)
        return [
            SWEBenchInstance(
                instance_id=str(r["instance_id"]),
                repo=str(r.get("repo", "")),
                base_commit=str(r.get("base_commit", "")),
                problem_statement=str(r.get("problem_statement", "")),
            )
            for r in rows
        ]

    def image(self, instance: Any) -> str:
        iid = instance.instance_id if isinstance(instance, SWEBenchInstance) else str(instance)
        return _image_template().format(id=iid)

    def env_kwargs(self) -> dict:
        # SWE-bench images check the repo out at /testbed with deps pre-installed.
        return {"repo_path": WORKSPACE, "keepalive": "4h"}

    def task_for(self, instance: Any, agent_index: int = 0, team_size: int = 1) -> str:
        ps = instance.problem_statement if isinstance(instance, SWEBenchInstance) else str(instance)
        return (
            f"## Task\n\nResolve the following GitHub issue in the repository at {WORKSPACE}. "
            "Make the source changes needed to fix the issue and keep the project's tests passing. "
            "Do NOT edit the test files.\n\n## Issue\n\n" + ps
        )

    def submit(self, instance: Any, patch: str, out_dir: Path) -> None:
        """Write a SWE-bench predictions.jsonl entry (the submitted model_patch)."""
        iid = instance.instance_id if isinstance(instance, SWEBenchInstance) else str(instance)
        out_dir.mkdir(parents=True, exist_ok=True)
        record = {"instance_id": iid, "model_name_or_path": _model_name(), "model_patch": patch}
        (out_dir / "predictions.jsonl").write_text(json.dumps(record) + "\n")

    def evaluate(self, run_dir: Path) -> Any:
        """Run the official SWE-bench harness on the predictions in ``run_dir``."""
        return subprocess.run(
            [
                "python",
                "-m",
                "swebench.harness.run_evaluation",
                "--dataset_name",
                _dataset(),
                "--predictions_path",
                str(run_dir / "predictions.jsonl"),
                "--run_id",
                run_dir.name,
                "--max_workers",
                os.getenv("SWEBENCH_MAX_WORKERS", "4"),
            ],
            capture_output=True,
            text=True,
        )


__all__ = ["SWEBenchAdapter", "SWEBenchInstance"]
