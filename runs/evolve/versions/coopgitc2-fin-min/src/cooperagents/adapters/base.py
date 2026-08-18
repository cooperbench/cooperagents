"""Benchmark adapter contract — the MOST BASIC adaptation only.

Verification (validate / score / repair) is part of the harness
(``cooperagents.verification``) and is implemented once, identically for
every benchmark: the harness discovers the build command mechanically and
runs reference-comparative behavior probes when a reference binary exists.
An adapter therefore contains no verification logic — it adapts the harness
to a benchmark's I/O and declares two facts verification needs:

Required methods (I/O adaptation)
  image(instance)                    container image for a task instance
  task_for(instance, i, team_size)   prompt for agent i of a team of N
  submit(instance, patch, out_dir)   package the final tree for scoring

Declarative facts (data, not code)
  build_artifact    file the build must (re)produce (e.g. "executable"),
                    or None when there is no single build product
  reference_binary  path of the runnable reference implementation the task
                    provides (e.g. "./executable"), or None

Optional methods
  env_kwargs()                       DockerEnv kwargs (user, network, ...)
  setup_env(env, agent_id)           prepare a fresh agent environment
  brief(instance)                    text prepended to every task
  evaluate(run_dir)                  invoke the benchmark's official scorer
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BenchmarkAdapter(ABC):
    name: str = "benchmark"

    build_artifact: str | None = None
    reference_binary: str | None = None

    @abstractmethod
    def image(self, instance: Any) -> str: ...

    @abstractmethod
    def task_for(self, instance: Any, agent_index: int, team_size: int) -> str: ...

    @abstractmethod
    def submit(self, instance: Any, patch: str, out_dir: Path) -> None: ...

    # -- optional --------------------------------------------------------
    def env_kwargs(self) -> dict:
        return {}

    def setup_env(self, env, agent_id: str) -> None:  # noqa: B027 - optional hook, intentionally a no-op
        pass

    def brief(self, instance: Any) -> str:
        return ""

    def evaluate(self, run_dir: Path) -> Any:
        raise NotImplementedError(f"{self.name} has no evaluate hook")
