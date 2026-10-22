"""Offline trace check for coordinator input probes."""

import json
from pathlib import Path

import pytest
from scripts import coordinator_input_probe as probe

from cooperagents.trajectory import record_call


def test_live_probe_records_raw_usage_and_dry_run_skips_trace(monkeypatch, tmp_path: Path):
    rows = [
        {"time": "2026-09-28T09:00:00+00:00", "actor": "agent1", "event": "agent_start", "data": {}},
        {"time": "2026-09-28T09:00:00+00:00", "actor": "agent2", "event": "agent_start", "data": {}},
        {"time": "2026-09-28T09:00:01+00:00", "actor": "coordinator", "event": "decision", "data": {}},
    ]
    starts = {aid: {"task": "feature", "role": "worker", "feature_id": index} for index, aid in enumerate(("agent1", "agent2"), 1)}
    monkeypatch.setattr(probe, "archived_state", lambda point: (rows, {"agent1": [], "agent2": []}, starts, {"agent1": [], "agent2": []}))

    class FakeCoordinator:
        def __init__(self, *args, complete, notebook_path, **kwargs):
            self.complete = complete
            self.error = None
            notebook_path.write_text("", encoding="utf-8")

        def register(self, *args):
            pass

        def decide(self, *, initial):
            self.complete("Instructions\nOBSERVATION:\n{}")

        def drain(self, aid):
            return []

    monkeypatch.setattr(probe, "_Coordinator", FakeCoordinator)
    planner_calls = []

    class FakeResponse:
        def model_dump(self, *, mode):
            return {"usage": {"prompt_tokens": 11, "completion_tokens": 8, "total_tokens": 19}}

    def fake_planner(*args, trace, **kwargs):
        planner_calls.append(trace)

        def complete(prompt):
            record_call(trace, lambda **request: FakeResponse(), model="fake", messages=[{"role": "user", "content": prompt}])
            return []

        return complete

    monkeypatch.setattr(probe, "_default_planner_complete", fake_planner)
    point = {
        "id": "train-case",
        "phase": "early",
        "target": "agent1",
        "repo": "example",
        "pair": "example/1",
        "boundary_seq": 3,
        "replay_model_calls": 5,
    }
    variant = {"case_id": point["id"], "instruction_addendum": "Ask for scope"}
    output = tmp_path / "probe.json"
    result = probe.run(point, variant, output)
    trace_path = output.with_suffix(".trace.jsonl")
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    assert len(planner_calls) == 1 and planner_calls[0] is not None
    assert result["trace_path"] == str(trace_path)
    assert json.loads(output.read_text(encoding="utf-8"))["trace_path"] == str(trace_path)
    assert [(row["actor"], row["event"]) for row in records] == [("coordinator", "request"), ("coordinator", "response")]
    assert "Ask for scope" in records[0]["data"]["request"]["messages"][0]["content"]
    assert records[1]["data"]["response"]["usage"]["total_tokens"] == 19
    with pytest.raises(FileExistsError):
        probe.run(point, variant, output)

    dry_output = tmp_path / "dry.json"
    dry = probe.run(point, variant, dry_output, dry_run=True)
    assert dry["trace_path"] is None
    assert not dry_output.with_suffix(".trace.jsonl").exists()
    assert len(planner_calls) == 1
