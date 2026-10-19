"""Control SDK and queue timing without network calls or real timeout waits."""

import json
import queue
from functools import partial
from threading import Event

import litellm
import pytest

from cooperagents.trajectory import Trajectory, replay
from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModel


@pytest.mark.parametrize("traced", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_normal_result_has_no_abandonment(monkeypatch, tmp_path, traced, fails):
    journal = Trajectory(tmp_path / "trajectory.jsonl")
    model = LitellmModel(model_name="openai/dummy")
    if traced:
        model.trace = partial(journal.emit, "agent1")

    def complete(**kwargs):
        assert "_trace" not in kwargs
        if fails:
            raise ValueError("SDK failure")
        return {"reply": "ok"}

    monkeypatch.setattr(litellm, "completion", complete)
    try:
        if fails:
            with pytest.raises(ValueError, match="SDK failure"):
                model._query([])
        else:
            assert model._query([]) == {"reply": "ok"}
    finally:
        journal.close()
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert all(row["event"] != "call_abandoned" for row in rows)
    assert not replay(journal.path)["pending_calls"]


@pytest.mark.parametrize(("boundary", "fails"), [("sdk", False), ("sdk", True), ("request", False), ("queue", False)])
def test_abandonment_preserves_sdk_outcome(monkeypatch, tmp_path, boundary, fails):
    journal = Trajectory(tmp_path / "trajectory.jsonl")
    model = LitellmModel(model_name="openai/dummy")
    blocked, release, finished = Event(), Event(), Event()

    def pause():
        blocked.set()
        assert release.wait(5)

    def trace(event, **data):
        if boundary == "request" and event == "request":
            pause()
        return journal.emit("agent1", event, **data)

    def complete(**kwargs):
        if boundary == "sdk":
            pause()
        if fails:
            raise ValueError("late SDK failure")
        return {"reply": "late"}

    class TimeoutQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            assert blocked.wait(5)
            assert timeout == model._HARD_TIMEOUT_S
            raise queue.Empty

        def put(self, item, block=True, timeout=None):
            if boundary == "queue":
                pause()
            super().put(item, block=block, timeout=timeout)
            finished.set()

    model.trace = trace
    monkeypatch.setattr(litellm, "completion", complete)
    monkeypatch.setattr(queue, "Queue", TimeoutQueue)
    try:
        with pytest.raises(litellm.Timeout, match="exceeded hard cap"):
            model._query([])
        rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
        abandoned = rows[-1]
        request_id = None if boundary == "request" else rows[0]["seq"]
        assert abandoned["event"] == "call_abandoned"
        assert abandoned["data"] == {
            "call_id": request_id,
            "reason": "hard_timeout",
            "wait_s": model._HARD_TIMEOUT_S,
        }
        pending = replay(journal.path)["pending_calls"]
        if boundary == "sdk":
            # No SDK terminal result yet: abandonment must not close its I/O.
            assert set(pending) == {request_id}
        else:
            assert not pending
    finally:
        release.set()
        assert finished.wait(5)
        journal.close()
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    terminal = next(row for row in rows if row["event"] in ("response", "error"))
    request = next(row for row in rows if row["event"] == "request")
    assert terminal["data"]["call_id"] == request["seq"]
    if fails:
        assert terminal["event"] == "error"
        assert terminal["data"]["error"] == "late SDK failure"
    else:
        assert terminal["data"]["response"] == {"reply": "late"}
    if boundary == "queue":
        assert terminal["seq"] < abandoned["seq"]
    assert not replay(journal.path)["pending_calls"]
