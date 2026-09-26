"""Pluggable tool sets for the builtin :class:`~cooperagents.agent.Agent`.

The agent's default catalog is code-editing (``bash`` / ``read_file`` /
``write_file``) plus coordination tools (``send_message`` / ``task_*`` /
``finish`` / ``spawn_helper``).  A :class:`ToolSet` lets a non-code benchmark
REPLACE the code-editing tools with its own (``search``, ``submit_answer``,
typed game actions, ...) while keeping the coordination tools intact — so the
same team/agent loop runs on a non-git substrate.

A ToolSet is inert unless passed to the agent; when unset the agent behaves
byte-for-byte as before.
"""

from __future__ import annotations

import abc

from cooperagents.env.base import Environment
from cooperagents.llm import Action

# Tool names a ToolSet REPLACES (the code-editing surface). The coordination
# and finish tools are always kept so a team still communicates and terminates.
CODE_TOOL_NAMES = frozenset({"bash", "read_file", "write_file"})


class ToolSet(abc.ABC):
    """Benchmark-specific tools layered onto the agent loop."""

    @abc.abstractmethod
    def specs(self) -> list[dict[str, str]]:
        """Catalog entries (``{"name", "description"}``) offered to the policy."""

    @abc.abstractmethod
    def dispatch(self, env: Environment, action: Action) -> tuple[str, bool] | None:
        """Execute one of this set's tools against ``env``.

        Return ``(observation, finished)`` when ``action.tool`` is one of ours,
        or ``None`` when it is not (the agent then reports an unknown tool).
        """


__all__ = ["CODE_TOOL_NAMES", "ToolSet"]
