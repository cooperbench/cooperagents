"""The Docker command trace survives a failed agent process."""

import json
import subprocess

import pytest

from cooperagents.env.docker import DockerEnv


def test_execute_records_command_before_subprocess_failure(monkeypatch, tmp_path):
    env = DockerEnv.__new__(DockerEnv)
    env.name = "test-container"
    env.repo_path = "/workspace/repo"
    monkeypatch.setenv("COOPER_DIAG_DIR", str(tmp_path))

    def fail(*args, **kwargs):
        traces = list(tmp_path.glob("exec-*.json"))
        assert len(traces) == 1
        assert json.loads(traces[0].read_text())["command"] == "produce huge output"
        raise MemoryError("simulated OOM")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(MemoryError):
        env.execute("produce huge output")
    records = [json.loads(line) for line in next(tmp_path.glob("exec-*.json")).read_text().splitlines()]
    assert records[1]["error"] == "MemoryError"
