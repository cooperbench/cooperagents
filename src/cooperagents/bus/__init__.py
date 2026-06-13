"""Coordination buses for the unified harness."""

from __future__ import annotations

from cooperagents.bus.base import TeamBus
from cooperagents.bus.memory import InMemoryBus


def make_bus(run_id: str, *, backend: str = "memory", url: str = "redis://localhost:6379") -> TeamBus:
    """Construct a bus by backend name (``"memory"`` or ``"redis"``)."""
    if backend == "memory":
        return InMemoryBus(run_id)
    if backend == "redis":
        from cooperagents.bus.redis_bus import RedisBus

        return RedisBus(run_id, url=url)
    raise ValueError(f"unknown bus backend {backend!r}; expected 'memory' or 'redis'")


__all__ = ["TeamBus", "InMemoryBus", "make_bus"]
