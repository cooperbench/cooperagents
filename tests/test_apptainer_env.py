"""Local adapter checks; real Apptainer coverage lives in the CPU dummy smoke."""

import threading

from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.runtime import task_environment


def test_execute_preserves_stdin_and_kills_timeout(tmp_path):
    # Use host bash only for this transport unit test, never for an agent run.
    env = object.__new__(ApptainerEnv)
    env._lock = threading.RLock()
    env._closed = False
    env._host_env = {"PATH": "/usr/bin:/bin"}
    env._argv = []
    env.repo_path = str(tmp_path)
    value = "x" * 100000
    env.write_file("large.txt", value)
    assert env.read_file("large.txt") == value
    assert env.execute("sleep 10", timeout=1).exit_code == 124
    assert env.execute("echo alive").stdout.strip() == "alive"


def test_unknown_runtime_fails_early(monkeypatch):
    monkeypatch.setenv("COOPER_RUNTIME", "invalid")
    import pytest

    with pytest.raises(ValueError):
        task_environment("unused")


def test_build_discovery_uses_go_when_make_is_missing(tmp_path):
    from cooperagents.verification import discover_build

    env = object.__new__(ApptainerEnv)
    env._lock = threading.RLock()
    env._closed = False
    env._host_env = {"PATH": str(tmp_path)}
    # Absolute bash lets the test supply an otherwise empty command search path.
    env._argv = ["/bin/bash", "-c", "exec /bin/bash -s", "--"]
    env.repo_path = str(tmp_path)
    (tmp_path / "Makefile").touch()
    (tmp_path / "go.mod").touch()
    assert discover_build(env) == "go build ./..."


def test_missing_repository_is_infrastructure_failure_but_command_125_is_observation(tmp_path):
    import pytest

    env = object.__new__(ApptainerEnv)
    env._lock = threading.RLock()
    env._closed = False
    env._host_env = {"PATH": "/usr/bin:/bin"}
    env._argv = []
    env.repo_path = str(tmp_path)
    assert env.execute("exit 125").exit_code == 125
    env.repo_path = str(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="repository is unavailable"):
        env.execute("echo should-not-run")
