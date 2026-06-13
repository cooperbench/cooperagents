"""The CooperBench eval bridge: output layout + command construction."""

from __future__ import annotations

import json

import pytest

from cooperagents.eval.cooperbench import build_eval_command, run_eval, write_run_outputs
from cooperagents.types import AgentResult, RunResult


def _result():
    return RunResult(
        run_id="abc",
        repo="demo_task",
        task_id=42,
        features=[1, 2],
        seeds={
            "agent1": AgentResult(agent_id="agent1", role="lead", status="submitted", patch="diff a", feature_id=1, cost=0.1, steps=3),
            "agent2": AgentResult(agent_id="agent2", role="member", status="submitted", patch="diff b", feature_id=2, cost=0.2, steps=4),
        },
        helpers={"helper1": AgentResult(agent_id="helper1", role="helper", status="submitted", patch="diff h")},
        metrics={"tasks_total": 1},
        spawn_metrics={"granted": 1},
    )


def test_write_outputs_layout(tmp_path):
    log_dir = write_run_outputs(_result(), run_name="exp", logs_dir=tmp_path, model="m")
    # CooperBench expects logs/<run>/team/<repo>/<task>/f1_f2/
    assert log_dir == tmp_path / "exp" / "team" / "demo_task" / "42" / "f1_f2"
    assert (log_dir / "agent1.patch").read_text() == "diff a"
    assert (log_dir / "agent2.patch").read_text() == "diff b"
    assert (log_dir / "helper1.patch").read_text() == "diff h"
    assert (log_dir / "agent1_traj.json").exists()


def test_result_json_shape(tmp_path):
    log_dir = write_run_outputs(_result(), run_name="exp", logs_dir=tmp_path)
    data = json.loads((log_dir / "result.json").read_text())
    assert data["setting"] == "team"  # so CooperBench scores per-feature patches
    assert data["features"] == [1, 2]
    assert data["repo"] == "demo_task"
    assert data["task_id"] == 42
    assert data["log_dir"] == str(log_dir)
    assert data["spawn_metrics"] == {"granted": 1}
    assert set(data["agents"]) == {"agent1", "agent2"}
    assert "helper1" in data["helpers"]


def test_missing_feature_patch_is_empty(tmp_path):
    # A run where feature 2's seed errored with no patch still writes an
    # (empty) agent2.patch so eval can attribute a clean fail.
    res = _result()
    res.seeds["agent2"] = AgentResult(agent_id="agent2", role="member", status="error", feature_id=2)
    log_dir = write_run_outputs(res, run_name="exp", logs_dir=tmp_path)
    assert (log_dir / "agent2.patch").read_text() == ""


def test_build_eval_command(tmp_path):
    try:
        cmd = build_eval_command("exp", logs_dir=tmp_path)
    except FileNotFoundError:
        pytest.skip("CooperBench checkout not found")
    assert cmd[:3] == ["cooperbench", "eval", "-n"]
    assert "exp" in cmd
    assert "--dataset-dir" in cmd and "--log-dir" in cmd


def test_run_eval_dry_run(tmp_path):
    try:
        cmd = run_eval("exp", logs_dir=tmp_path, dry_run=True)
    except FileNotFoundError:
        pytest.skip("CooperBench checkout not found")
    assert cmd[:2] == ["uv", "run"]
    assert "cooperbench" in cmd and "eval" in cmd
