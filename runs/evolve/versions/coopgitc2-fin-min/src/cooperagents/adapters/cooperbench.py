"""CooperBench adapter: implement N feature specs on one repository task.

Instance = an ``eval.dataset.WorkItem`` (repo, task_id, features). Unlike
ProgramBench's one-shared-objective shape, CooperBench assigns agent i the
spec of feature i — task_for differs per agent. Score = features passing
CooperBench's hidden tests (``cooperbench eval``).

This adapter wraps the existing integration modules (``eval.dataset`` for
task source and images, ``eval.cooperbench`` for the log layout and the
evaluator invocation). Verification is the harness's, uniformly: with no
build_artifact and no reference_binary declared, validate reduces to the
discovered-build check and score to build-tier + patch size.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cooperagents.adapters.base import BenchmarkAdapter
from cooperagents.eval import cooperbench as cb
from cooperagents.eval.dataset import WorkItem, image_name, read_feature


class CooperBenchAdapter(BenchmarkAdapter):
    name = "cooperbench"

    # -- environment ----------------------------------------------------
    def image(self, instance: WorkItem) -> str:
        return image_name(instance.repo, instance.task_id)

    def env_kwargs(self) -> dict:
        # CooperBench images ship the repo at /workspace/repo and are built
        # to run with network available (deps pre-installed; tests offline).
        return {"keepalive": "4h"}

    def setup_env(self, env, agent_id: str) -> None:
        env.execute(f"echo {agent_id} > /tmp/.agent_id")

    # -- task -----------------------------------------------------------
    def task_for(self, instance: WorkItem, agent_index: int, team_size: int) -> str:
        """Agent i implements feature i. With more agents than features the
        extras share the last feature; solo (team_size=1) gets ALL specs."""
        feats = instance.features
        if team_size == 1:
            specs = "\n\n".join(
                read_feature(instance.repo, instance.task_id, f) for f in feats)
            return ("Implement ALL of the following features in this "
                    "repository:\n\n" + specs)
        f = feats[min(agent_index, len(feats) - 1)]
        return read_feature(instance.repo, instance.task_id, f)

    # -- submission -----------------------------------------------------
    def submit(self, instance: WorkItem, patch: str, out_dir: Path) -> None:
        """CooperBench scoring consumes the log layout written by
        ``eval.cooperbench.write_run_outputs`` from a RunResult; runners call
        that directly. Here we persist the raw integrated patch alongside."""
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "integrated.patch").write_text(patch)

    def evaluate(self, run_dir: Path) -> subprocess.CompletedProcess | list[str]:
        return cb.run_eval(run_dir.name, logs_dir=run_dir.parent)
