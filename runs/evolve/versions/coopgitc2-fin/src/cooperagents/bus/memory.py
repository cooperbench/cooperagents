"""In-memory :class:`TeamBus` — thread-safe, zero external dependencies.

This is the default backend.  Because the unified harness runs every agent
loop as a host-side thread (the agent execs commands *into* its environment
rather than living inside it), a single in-process bus is enough for a
local run and for the entire test suite.  Switch to :class:`RedisBus` only
when agents must coordinate across processes or hosts.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any

from cooperagents.bus.base import VALID_STATUSES, TeamBus
from cooperagents.types import SpawnRequest


class InMemoryBus(TeamBus):
    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._agents: dict[str, str] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._task_log: list[dict[str, Any]] = []
        self._inboxes: dict[str, deque[dict[str, Any]]] = {}
        self._message_log: list[dict[str, Any]] = []
        self._spawn_queue: deque[SpawnRequest] = deque()
        self._spawn_log: list[dict[str, Any]] = []
        self._spawn_counter = 0

    # --- roster --------------------------------------------------------

    def register_agent(self, agent_id: str, *, role: str) -> None:
        with self._lock:
            self._agents[agent_id] = role
            self._inboxes.setdefault(agent_id, deque())

    def list_agents(self) -> list[dict[str, str]]:
        with self._lock:
            return [{"id": a, "role": r} for a, r in self._agents.items()]

    # --- task list -----------------------------------------------------

    def create_task(self, *, title: str, created_by: str, owner: str = "", metadata: dict | None = None) -> str:
        task_id = uuid.uuid4().hex[:10]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "title": title,
                "owner": owner,
                "status": "open",
                "created_by": created_by,
                "created_at": time.time(),
                "last_note": "",
                "metadata": metadata or {},
            }
            self._task_log.append({"kind": "create", "task_id": task_id, "by": created_by, "title": title, "ts": time.time()})
        return task_id

    def claim_task(self, task_id: str, *, by: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["owner"] and task["owner"] != by:
                return False
            task["owner"] = by
            task["status"] = "in_progress"
            self._task_log.append({"kind": "claim", "task_id": task_id, "by": by, "ts": time.time()})
            return True

    def update_task(self, task_id: str, *, by: str, status: str | None = None, note: str | None = None) -> None:
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["owner"] != by:
                raise PermissionError(f"task {task_id} not owned by {by!r}")
            if status is not None:
                task["status"] = status
            if note is not None:
                task["last_note"] = note
            event: dict[str, Any] = {"kind": "update", "task_id": task_id, "by": by, "ts": time.time()}
            if status is not None:
                event["status"] = status
            if note is not None:
                event["note"] = note
            self._task_log.append(event)

    def list_tasks(self, *, owner: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for task in self._tasks.values():
                if owner is not None and task["owner"] != owner:
                    continue
                if status is not None and task["status"] != status:
                    continue
                out.append(dict(task))
            return out

    def task_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._task_log]

    # --- messaging -----------------------------------------------------

    def _deliver(self, msg: dict[str, Any]) -> None:
        self._inboxes.setdefault(msg["to"], deque()).append(msg)
        self._message_log.append(dict(msg))

    def send(self, *, sender: str, to: str, content: str) -> None:
        with self._cond:
            self._deliver({"from": sender, "to": to, "content": content, "ts": time.time()})
            self._cond.notify_all()

    def broadcast(self, *, sender: str, content: str) -> None:
        with self._cond:
            for agent_id in list(self._agents):
                if agent_id != sender:
                    self._deliver({"from": sender, "to": agent_id, "content": content, "ts": time.time()})
            self._cond.notify_all()

    def receive(self, agent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            box = self._inboxes.setdefault(agent_id, deque())
            out = list(box)
            box.clear()
            return out

    def message_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(m) for m in self._message_log]

    # --- spawn queue ---------------------------------------------------

    def spawn_request(self, *, requested_by: str, task: str, role: str = "helper", metadata: dict | None = None) -> str:
        request_id = uuid.uuid4().hex[:10]
        req = SpawnRequest(id=request_id, requested_by=requested_by, task=task, role=role, metadata=metadata or {})
        with self._cond:
            self._spawn_queue.append(req)
            self._spawn_log.append({"kind": "request", "request_id": request_id, "by": requested_by, "role": role, "ts": time.time()})
            self._cond.notify_all()
        return request_id

    def spawn_pop(self, *, timeout: float = 1.0) -> SpawnRequest | None:
        deadline = time.time() + max(0.0, timeout)
        with self._cond:
            while not self._spawn_queue:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
            return self._spawn_queue.popleft()

    def spawn_next_index(self) -> int:
        with self._lock:
            self._spawn_counter += 1
            return self._spawn_counter

    def spawn_mark(self, request_id: str, *, outcome: str, agent_id: str | None = None) -> None:
        with self._lock:
            event: dict[str, Any] = {"kind": "outcome", "request_id": request_id, "outcome": outcome, "ts": time.time()}
            if agent_id is not None:
                event["agent_id"] = agent_id
            self._spawn_log.append(event)

    def spawn_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._spawn_log]
