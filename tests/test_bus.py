"""Bus behavior — exercised against BOTH backends so they stay in lockstep."""

from __future__ import annotations

import threading

import pytest

BUSES = ["mem_bus", "redis_bus"]


@pytest.fixture(params=BUSES)
def bus(request):
    return request.getfixturevalue(request.param)


class TestTaskList:
    def test_create_and_list(self, bus):
        tid = bus.create_task(title="do X", created_by="agent1")
        tasks = bus.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["id"] == tid
        assert tasks[0]["status"] == "open"

    def test_claim_is_exclusive(self, bus):
        tid = bus.create_task(title="do X", created_by="lead")
        assert bus.claim_task(tid, by="agent1") is True
        # Second claimant loses.
        assert bus.claim_task(tid, by="agent2") is False
        # Original owner is idempotent.
        assert bus.claim_task(tid, by="agent1") is True
        assert bus.list_tasks()[0]["owner"] == "agent1"
        assert bus.list_tasks()[0]["status"] == "in_progress"

    def test_claim_unknown_raises(self, bus):
        with pytest.raises(KeyError):
            bus.claim_task("nope", by="agent1")

    def test_update_owner_only(self, bus):
        tid = bus.create_task(title="t", created_by="lead", owner="agent1")
        bus.claim_task(tid, by="agent1")
        bus.update_task(tid, by="agent1", status="done", note="finished")
        assert bus.list_tasks(status="done")[0]["last_note"] == "finished"
        with pytest.raises(PermissionError):
            bus.update_task(tid, by="agent2", status="open")

    def test_update_invalid_status(self, bus):
        tid = bus.create_task(title="t", created_by="a")
        bus.claim_task(tid, by="a")
        with pytest.raises(ValueError):
            bus.update_task(tid, by="a", status="bogus")

    def test_audit_log(self, bus):
        tid = bus.create_task(title="t", created_by="a")
        bus.claim_task(tid, by="a")
        kinds = [e["kind"] for e in bus.task_events()]
        assert kinds == ["create", "claim"]


class TestMessaging:
    def test_direct(self, bus):
        bus.register_agent("a", role="lead")
        bus.register_agent("b", role="member")
        bus.send(sender="a", to="b", content="hi")
        inbox = bus.receive("b")
        assert len(inbox) == 1 and inbox[0]["content"] == "hi"
        # Draining is destructive.
        assert bus.receive("b") == []

    def test_broadcast_excludes_sender(self, bus):
        for x in ("a", "b", "c"):
            bus.register_agent(x, role="member")
        bus.broadcast(sender="a", content="all hands")
        assert bus.receive("a") == []
        assert len(bus.receive("b")) == 1
        assert len(bus.receive("c")) == 1


class TestSpawnQueue:
    def test_request_pop_roundtrip(self, bus):
        rid = bus.spawn_request(requested_by="lead", task="help me")
        req = bus.spawn_pop(timeout=0.2)
        assert req is not None
        assert req.id == rid
        assert req.task == "help me"
        assert req.requested_by == "lead"

    def test_pop_timeout_returns_none(self, bus):
        assert bus.spawn_pop(timeout=0.1) is None

    def test_indices_are_monotonic(self, bus):
        assert [bus.spawn_next_index() for _ in range(3)] == [1, 2, 3]

    def test_outcomes_logged(self, bus):
        rid = bus.spawn_request(requested_by="lead", task="t")
        bus.spawn_mark(rid, outcome="granted", agent_id="helper1")
        events = bus.spawn_events()
        kinds = [e["kind"] for e in events]
        assert "request" in kinds and "outcome" in kinds

    def test_blocking_pop_wakes_on_request(self, bus):
        got = {}

        def consumer():
            got["req"] = bus.spawn_pop(timeout=2.0)

        t = threading.Thread(target=consumer)
        t.start()
        bus.spawn_request(requested_by="lead", task="async")
        t.join(timeout=3.0)
        assert got["req"] is not None and got["req"].task == "async"
