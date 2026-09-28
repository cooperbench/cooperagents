"""Offline checks for source boundaries, judge blinding, and replay state gates."""

import json
from pathlib import Path

import pytest
from scripts.coordinator_research import choose_boundary
from scripts.coordinator_research_score import aggregate, packet
from scripts.coordinator_worker_replay import replay_environment, seed_environment, verified_patch

from cooperagents.env.base import ExecResult


def test_early_and_budget_boundaries_use_worker_requests():
    rows = [
        {"seq": 1, "actor": "agent1", "event": "request", "data": {"request": {"messages": []}}},
        {"seq": 2, "actor": "agent1", "event": "request", "data": {"request": {"messages": []}}},
        {"seq": 3, "actor": "agent2", "event": "request", "data": {"request": {"messages": []}}},
        {"seq": 4, "actor": "agent1", "event": "agent_end", "data": {}},
        {"seq": 5, "actor": "agent2", "event": "agent_end", "data": {}},
    ]
    early, request, overlap = choose_boundary(rows, "early")
    assert (early["seq"], request["seq"], overlap) == (1, 1, None)
    budget, request, overlap = choose_boundary(rows, "budget")
    assert (budget["seq"], request["seq"], overlap) == (1, 1, None)


def test_noninitial_replay_requires_reviewed_matching_snapshot(tmp_path: Path):
    point = {
        "id": "example",
        "trajectory": "/archive/example.jsonl.gz",
        "worker_request_seq": 42,
        "archive_dirty_files": ["a.py"],
        "workspace_status": "needs_reconstruction",
    }
    with pytest.raises(ValueError, match="reviewed workspace snapshot"):
        verified_patch(point, None)
    patch = tmp_path / "example.patch"
    patch.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    (tmp_path / "example.json").write_text(json.dumps({"reviewed_against_archive": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        verified_patch(point, tmp_path)


def test_replay_runtime_keeps_notebook_mount_and_docker_network_off(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setenv("COOPER_RUNTIME", "docker")
    monkeypatch.setattr(
        "scripts.coordinator_worker_replay.DockerEnv",
        lambda image, **kwargs: calls.append((image, kwargs)),
    )
    replay_environment("image", tmp_path)
    assert calls == [("image", {"network": "none", "volumes": [f"{tmp_path}:/coordination:ro"], "keepalive": "8h"})]

    calls.clear()
    monkeypatch.setenv("COOPER_RUNTIME", "apptainer")
    monkeypatch.setattr(
        "scripts.coordinator_worker_replay.task_environment",
        lambda image, **kwargs: calls.append((image, kwargs)),
    )
    replay_environment("image", tmp_path)
    assert calls == [("image", {"coordinator_dir": tmp_path})]


def test_replay_seed_applies_patch_and_checks_exact_workspace(tmp_path: Path):
    class FakeEnv:
        def __init__(self) -> None:
            self.commands = []
            self.files = {}

        def write_file(self, path: str, content: str) -> None:
            self.files[path] = content

        def execute(self, command: str) -> ExecResult:
            self.commands.append(command)
            if command.startswith("git -c"):
                return ExecResult(" M a.py\0", 0)
            return ExecResult("", 0)

    patch = tmp_path / "state.patch"
    patch.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    env = FakeEnv()
    seed_environment(env, {"archive_dirty_files": ["a.py"]}, patch)
    assert env.files["/tmp/coordinator-replay.patch"] == patch.read_text(encoding="utf-8")
    assert env.commands[0] == "git apply /tmp/coordinator-replay.patch"
    with pytest.raises(ValueError, match="Workspace files differ"):
        seed_environment(env, {"archive_dirty_files": ["other.py"]}, None)


def test_judge_packet_blinds_candidate_and_keeps_behavioral_trace():
    point = {
        "id": "example",
        "phase": "early",
        "expected_coordinator_behavior": "ask for scope",
        "expected_worker_reaction": "send a scope proposal",
    }
    control = {
        "case_id": "example",
        "notices": [],
        "steps": [{"index": 1, "actions": []}],
        "final_diff": "",
        "workspace_evidence": "base_image",
    }
    candidate = {**control, "notices": ["[coordinator] Report scope"]}
    data, arm = packet(point, control, candidate)
    assert data["arms"][arm]["notices"] == candidate["notices"]
    assert data["arms"]["B" if arm == "A" else "A"]["notices"] == []
    assert all("label" not in trace for trace in data["arms"].values())


def test_partial_score_is_visible_but_not_on_pareto_frontier(tmp_path: Path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "candidate.json").write_text(json.dumps({"candidate": {"actions": []}}), encoding="utf-8")
    for split in ("train", "validation"):
        path = tmp_path / split / "score.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {
                    "candidate_id": "v1",
                    "split": split,
                    "effect": 2,
                    "cost": 10,
                    "worker_token_usage_available": True,
                    "replay_path": str(replay),
                }
            ),
            encoding="utf-8",
        )
    output = tmp_path / "frontier.json"
    aggregate(tmp_path, output)
    point = json.loads(output.read_text(encoding="utf-8"))[0]
    assert (point["train_count"], point["validation_count"], point["pareto"]) == (1, 1, False)
