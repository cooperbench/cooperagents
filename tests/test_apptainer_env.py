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


def test_notebook_mounts_reuse_existing_runtime_options(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from cooperagents.env.base import ExecResult

    directory = tmp_path / "notes"
    directory.mkdir()
    captured = []
    monkeypatch.setenv("COOPER_RUNTIME", "docker")
    monkeypatch.setattr("cooperagents.env.docker.DockerEnv", lambda *a, **k: captured.append(k))
    task_environment("image", volumes=["shared:/cbshared"], coordinator_dir=directory)
    assert captured == [{"volumes": ["shared:/cbshared", f"{directory}:/coordination:ro"]}]
    sif = tmp_path / "image.sif"
    sif.touch()
    manifest = tmp_path / "images.json"
    manifest.write_text(json.dumps({"image": str(sif)}))
    monkeypatch.setenv("COOPER_RUNTIME", "apptainer")
    monkeypatch.setenv("COOPER_IMAGE_MANIFEST", str(manifest))
    monkeypatch.setenv("COOPER_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr("cooperagents.env.apptainer.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(ApptainerEnv, "execute", lambda *a, **k: ExecResult("commit\n", 0))
    env = task_environment("image", volumes=["shared:/cbshared"], coordinator_dir=directory)
    try:
        assert f"{directory}:/coordination:ro" in env._argv
        assert f"{tmp_path}/scratch/shared/shared:/cbshared" in env._argv
        assert (env.root / "fs/coordination").is_dir()
    finally:
        env.cleanup()
