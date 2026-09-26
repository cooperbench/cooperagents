"""Non-git execution environment for tool-use / reasoning benchmarks.

A :class:`StateEnv` has no filesystem and no git.  Benchmark tools mutate its
``store`` (a scratch dict), record actions in ``log``, set ``answer``, and may
attach a benchmark ``sandbox`` (pandas DBs, a simulator, ...).  The
contribution is harvested as a :class:`StateArtifact` instead of a diff.  This
is the non-code counterpart of ``LocalEnv`` / ``DockerEnv`` and satisfies the
same :class:`Environment` protocol, so the team/agent loop is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cooperagents.env.artifact import Artifact, StateArtifact
from cooperagents.env.base import Environment, ExecResult


class StateEnv(Environment):
    """A stateful, git-free sandbox harvested as a :class:`StateArtifact`.

    ``harvest`` customises what the contribution captures (e.g. a serialized
    final DB state, an action trajectory + terminal inventory).  When unset,
    the default harvest returns the ``answer`` and the recorded ``log``.
    """

    def __init__(
        self,
        *,
        harvest: Callable[[StateEnv], Artifact] | None = None,
        repo_path: str = "/state",
    ) -> None:
        self.repo_path = repo_path
        self.answer: str = ""
        self.store: dict[str, Any] = {}
        self.log: list[str] = []
        self.sandbox: Any = None  # benchmark-specific state (DBs, simulator, ...)
        self._harvest = harvest

    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        # No shell in a non-code environment; benchmark actions go through a ToolSet.
        return ExecResult(stdout="", exit_code=0)

    def read_file(self, path: str) -> str:
        val = self.store.get(path, "")
        return val if isinstance(val, str) else str(val)

    def write_file(self, path: str, content: str) -> None:
        self.store[path] = content

    def record(self, entry: str) -> None:
        """Append one action/observation to the trajectory log."""
        self.log.append(entry)

    def contribution(self) -> Artifact:
        if self._harvest is not None:
            return self._harvest(self)
        return StateArtifact(answer=self.answer, trajectory="\n".join(self.log))

    def cleanup(self) -> None:
        close = getattr(self.sandbox, "close", None)
        if callable(close):
            close()


__all__ = ["StateEnv"]
