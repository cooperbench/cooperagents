"""Redis-backed :class:`TeamBus` for real multi-process / multi-host runs.

Semantics match :class:`InMemoryBus` exactly; only the storage changes.
All keys are namespaced ``ca:<run_id>:`` so independent runs can share one
Redis.  ``redis`` is imported lazily so the package installs and tests run
without it.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from cooperagents.bus.base import VALID_STATUSES, TeamBus
from cooperagents.types import SpawnRequest


def _decode(value: Any) -> Any:
    return value.decode("utf-8") if isinstance(value, bytes) else value


class RedisBus(TeamBus):
    def __init__(self, run_id: str, url: str = "redis://localhost:6379", *, client: Any = None) -> None:
        super().__init__(run_id)
        self._r: Any
        if client is not None:
            # Injected client (e.g. fakeredis in tests).
            self._r = client
        else:
            import redis  # lazy

            if "#" in url:
                url = url.split("#", 1)[0]
            self._r = redis.from_url(url)
        self._ns = f"ca:{run_id}"

    # --- roster --------------------------------------------------------

    def register_agent(self, agent_id: str, *, role: str) -> None:
        self._r.hset(f"{self._ns}:agents", agent_id, role)

    def list_agents(self) -> list[dict[str, str]]:
        raw = self._r.hgetall(f"{self._ns}:agents")
        return [{"id": _decode(a), "role": _decode(r)} for a, r in raw.items()]

    # --- task list -----------------------------------------------------

    def _log_task(self, **event: Any) -> None:
        event["ts"] = time.time()
        self._r.rpush(f"{self._ns}:task-log", json.dumps(event))

    def create_task(self, *, title: str, created_by: str, owner: str = "", metadata: dict | None = None) -> str:
        task_id = uuid.uuid4().hex[:10]
        self._r.hset(
            f"{self._ns}:task:{task_id}",
            mapping={
                "id": task_id,
                "title": title,
                "owner": owner,
                "status": "open",
                "created_by": created_by,
                "created_at": str(time.time()),
                "last_note": "",
                "metadata": json.dumps(metadata or {}),
            },
        )
        self._r.sadd(f"{self._ns}:tasks:all", task_id)
        self._log_task(kind="create", task_id=task_id, by=created_by, title=title)
        return task_id

    def claim_task(self, task_id: str, *, by: str) -> bool:
        key = f"{self._ns}:task:{task_id}"
        if not self._r.exists(key):
            raise KeyError(task_id)
        owner = _decode(self._r.hget(key, "owner")) or ""
        if owner and owner != by:
            return False
        self._r.hset(key, mapping={"owner": by, "status": "in_progress"})
        self._log_task(kind="claim", task_id=task_id, by=by)
        return True

    def update_task(self, task_id: str, *, by: str, status: str | None = None, note: str | None = None) -> None:
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        key = f"{self._ns}:task:{task_id}"
        owner = _decode(self._r.hget(key, "owner"))
        if owner is None:
            raise KeyError(task_id)
        if owner != by:
            raise PermissionError(f"task {task_id} not owned by {by!r}")
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        if note is not None:
            updates["last_note"] = note
        if updates:
            self._r.hset(key, mapping=updates)
        event: dict[str, Any] = {"kind": "update", "task_id": task_id, "by": by}
        if status is not None:
            event["status"] = status
        if note is not None:
            event["note"] = note
        self._log_task(**event)

    def _get_task(self, task_id: str) -> dict[str, Any]:
        raw = {_decode(k): _decode(v) for k, v in self._r.hgetall(f"{self._ns}:task:{task_id}").items()}
        if "created_at" in raw:
            try:
                raw["created_at"] = float(raw["created_at"])
            except ValueError:
                pass
        if "metadata" in raw:
            try:
                raw["metadata"] = json.loads(raw["metadata"])
            except (TypeError, json.JSONDecodeError):
                raw["metadata"] = {}
        return raw

    def list_tasks(self, *, owner: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        ids = sorted(_decode(m) for m in self._r.smembers(f"{self._ns}:tasks:all"))
        out = []
        for tid in ids:
            task = self._get_task(tid)
            if not task:
                continue
            if owner is not None and task.get("owner") != owner:
                continue
            if status is not None and task.get("status") != status:
                continue
            out.append(task)
        return out

    def task_events(self) -> list[dict[str, Any]]:
        return [json.loads(_decode(e)) for e in self._r.lrange(f"{self._ns}:task-log", 0, -1)]

    # --- messaging -----------------------------------------------------

    def send(self, *, sender: str, to: str, content: str) -> None:
        msg = {"from": sender, "to": to, "content": content, "ts": time.time()}
        self._r.rpush(f"{self._ns}:inbox:{to}", json.dumps(msg))
        self._r.rpush(f"{self._ns}:msg-log", json.dumps(msg))

    def broadcast(self, *, sender: str, content: str) -> None:
        for agent in self.list_agents():
            if agent["id"] != sender:
                self.send(sender=sender, to=agent["id"], content=content)

    def receive(self, agent_id: str) -> list[dict[str, Any]]:
        key = f"{self._ns}:inbox:{agent_id}"
        out = [json.loads(_decode(m)) for m in self._r.lrange(key, 0, -1)]
        self._r.delete(key)
        return out

    def message_log(self) -> list[dict[str, Any]]:
        return [json.loads(_decode(m)) for m in self._r.lrange(f"{self._ns}:msg-log", 0, -1)]

    # --- spawn queue ---------------------------------------------------

    def spawn_request(self, *, requested_by: str, task: str, role: str = "helper", metadata: dict | None = None) -> str:
        request_id = uuid.uuid4().hex[:10]
        payload = {"id": request_id, "requested_by": requested_by, "task": task, "role": role, "metadata": metadata or {}}
        self._r.rpush(f"{self._ns}:spawn:queue", json.dumps(payload))
        self._r.rpush(
            f"{self._ns}:spawn:log",
            json.dumps({"kind": "request", "request_id": request_id, "by": requested_by, "role": role, "ts": time.time()}),
        )
        return request_id

    def spawn_pop(self, *, timeout: float = 1.0) -> SpawnRequest | None:
        result = self._r.blpop([f"{self._ns}:spawn:queue"], timeout=max(0.0, timeout))
        if result is None:
            return None
        _key, raw = result
        data = json.loads(_decode(raw))
        return SpawnRequest(
            id=data["id"],
            requested_by=data["requested_by"],
            task=data["task"],
            role=data.get("role", "helper"),
            metadata=data.get("metadata", {}),
        )

    def spawn_next_index(self) -> int:
        return int(self._r.incr(f"{self._ns}:spawn:counter"))

    def spawn_mark(self, request_id: str, *, outcome: str, agent_id: str | None = None) -> None:
        event: dict[str, Any] = {"kind": "outcome", "request_id": request_id, "outcome": outcome, "ts": time.time()}
        if agent_id is not None:
            event["agent_id"] = agent_id
        self._r.rpush(f"{self._ns}:spawn:log", json.dumps(event))

    def spawn_events(self) -> list[dict[str, Any]]:
        return [json.loads(_decode(e)) for e in self._r.lrange(f"{self._ns}:spawn:log", 0, -1)]

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
