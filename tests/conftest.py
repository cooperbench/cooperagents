"""Shared fixtures."""

from __future__ import annotations

import pytest

from cooperagents.bus.memory import InMemoryBus


@pytest.fixture
def mem_bus():
    return InMemoryBus("test-run")


@pytest.fixture
def redis_bus():
    """A RedisBus backed by fakeredis (no daemon needed)."""
    fakeredis = pytest.importorskip("fakeredis")
    from cooperagents.bus.redis_bus import RedisBus

    return RedisBus("test-run", client=fakeredis.FakeRedis())
