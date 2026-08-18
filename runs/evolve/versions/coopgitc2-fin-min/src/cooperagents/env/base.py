"""Execution environment abstraction.

An agent never touches the filesystem directly — it acts through an
``Environment`` that runs shell commands and reports a unified diff.  Two
backends exist: :class:`LocalEnv` (a git checkout on the host, used by
tests and local runs) and :class:`DockerEnv` (a container built from a
CooperBench task image).  Both honor the same protocol so the agent loop is
backend-agnostic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class ExecResult:
    stdout: str
    exit_code: int


class Environment(abc.ABC):
    """A sandbox the agent runs commands in."""

    repo_path: str

    @abc.abstractmethod
    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        """Run ``command`` in ``repo_path`` and capture combined output."""

    @abc.abstractmethod
    def read_file(self, path: str) -> str:
        """Read a file relative to ``repo_path`` (empty string if missing)."""

    @abc.abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Write a file relative to ``repo_path``."""

    @abc.abstractmethod
    def git_diff(self) -> str:
        """Unified diff of the working tree vs. the base commit."""

    def cleanup(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Tear the environment down."""
