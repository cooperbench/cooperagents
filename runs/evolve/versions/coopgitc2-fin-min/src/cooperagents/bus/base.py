"""The team bus: the single shared substrate the whole team coordinates on.

A bus bundles the three real-time channels the North Star calls for:

  * **task list** — a shared, claimable to-do list with an audit log,
  * **messaging** — per-agent inboxes (direct + broadcast),
  * **spawn queue** — the channel an agent uses to recruit a helper, which
    the host supervisor drains to grow the team at runtime.

Keeping all three behind one interface is the crux of the team/agent
co-design: the orchestrator and every agent hold the *same* bus object, so
there is no second "team harness" layer wrapping the agents — there is one
harness, and the bus is its nervous system.

Two backends implement this ABC: :class:`InMemoryBus` (default; thread-safe,
needs nothing external) and :class:`RedisBus` (for real multi-process /
multi-host runs).  Tests run against the in-memory one.
"""

from __future__ import annotations

import abc
from typing import Any

from cooperagents.types import SpawnRequest

VALID_STATUSES = frozenset({"open", "in_progress", "blocked", "done"})


class TeamBus(abc.ABC):
    """Abstract coordination bus, namespaced to a single run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    # --- roster --------------------------------------------------------

    @abc.abstractmethod
    def register_agent(self, agent_id: str, *, role: str) -> None:
        """Announce an agent (including helpers spawned mid-run)."""

    @abc.abstractmethod
    def list_agents(self) -> list[dict[str, str]]:
        """All agents seen so far, as ``[{"id", "role"}, ...]``."""

    # --- task list -----------------------------------------------------

    @abc.abstractmethod
    def create_task(self, *, title: str, created_by: str, owner: str = "", metadata: dict | None = None) -> str:
        """Create a task (status ``open``); return its id."""

    @abc.abstractmethod
    def claim_task(self, task_id: str, *, by: str) -> bool:
        """Atomically claim a task. True on success, False if already owned."""

    @abc.abstractmethod
    def update_task(self, task_id: str, *, by: str, status: str | None = None, note: str | None = None) -> None:
        """Update status/note (owner only)."""

    @abc.abstractmethod
    def list_tasks(self, *, owner: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """All tasks, optionally filtered."""

    @abc.abstractmethod
    def task_events(self) -> list[dict[str, Any]]:
        """The task-list audit log."""

    # --- messaging -----------------------------------------------------

    @abc.abstractmethod
    def send(self, *, sender: str, to: str, content: str) -> None:
        """Direct message to one agent's inbox."""

    @abc.abstractmethod
    def broadcast(self, *, sender: str, content: str) -> None:
        """Message every other known agent."""

    @abc.abstractmethod
    def receive(self, agent_id: str) -> list[dict[str, Any]]:
        """Drain and return ``agent_id``'s inbox."""

    @abc.abstractmethod
    def message_log(self) -> list[dict[str, Any]]:
        """Every message sent during the run (for trajectory analysis)."""

    # --- spawn queue ---------------------------------------------------

    @abc.abstractmethod
    def spawn_request(self, *, requested_by: str, task: str, role: str = "helper", metadata: dict | None = None) -> str:
        """Enqueue a helper-spawn request; return its id."""

    @abc.abstractmethod
    def spawn_pop(self, *, timeout: float = 1.0) -> SpawnRequest | None:
        """Block up to ``timeout`` for the next request (supervisor side)."""

    @abc.abstractmethod
    def spawn_next_index(self) -> int:
        """Atomically hand out the next 1-based helper index."""

    @abc.abstractmethod
    def spawn_mark(self, request_id: str, *, outcome: str, agent_id: str | None = None) -> None:
        """Record the supervisor's decision (``granted``/``capped``/``failed``)."""

    @abc.abstractmethod
    def spawn_events(self) -> list[dict[str, Any]]:
        """The spawn audit log."""

    # --- lifecycle -----------------------------------------------------

    def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any resources (no-op for the in-memory bus)."""
