"""Non-code benchmarks: the state-substrate counterpart of ``adapters``.

The git ``BenchmarkAdapter`` answers image/task_for/submit/evaluate for a code
repo.  A :class:`StateBenchmark` answers the equivalent for a tool-use /
reasoning benchmark that runs on the state substrate: it supplies the task
prompt, the per-agent :class:`~cooperagents.env.state.StateEnv` (with its
benchmark sandbox), the :class:`~cooperagents.tools.ToolSet` the agents use in
place of shell/file tools, and the :class:`~cooperagents.eval.scoring.Scorer`
that grades the submitted deliverable with the benchmark's official evaluator.

The model-sweep box runner consumes exactly this interface, so adding a
benchmark is writing one :class:`StateBenchmark`.
"""

from __future__ import annotations

import abc
from typing import Any

from cooperagents.env.state import StateEnv
from cooperagents.eval.scoring import Scorer
from cooperagents.tools import ToolSet


class StateBenchmark(abc.ABC):
    """One non-code benchmark wired for the cooperagents harness."""

    name: str = "state-benchmark"

    @abc.abstractmethod
    def instances(self, split: str) -> list[Any]:
        """Load the task instances for ``split``."""

    @abc.abstractmethod
    def task_prompt(self, instance: Any) -> str:
        """The instruction handed to each agent for ``instance``."""

    @abc.abstractmethod
    def make_env(self, instance: Any) -> StateEnv:
        """A FRESH, isolated environment (with its own sandbox) for one agent on
        ``instance``. Called once per agent so agents never share live state."""

    @abc.abstractmethod
    def toolset(self) -> ToolSet:
        """The (stateless) tool set agents use; operates on each ``env``."""

    @abc.abstractmethod
    def scorer(self) -> Scorer:
        """The official grader for this benchmark's deliverable."""

    def instance_id(self, instance: Any) -> str:
        return str(getattr(instance, "id", getattr(instance, "instance_id", "")))


__all__ = ["StateBenchmark"]
