"""Local adapter checks; real Apptainer coverage lives in the CPU dummy smoke."""

import importlib.util
import json
import shlex
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert env.execute("printf ordinary; exit 255").stdout == "ordinary"
    assert env.execute("exit 255").exit_code == 255
    env.repo_path = str(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="repository is unavailable"):
        env.execute("echo should-not-run")


def test_container_startup_failure_is_not_a_tool_observation(monkeypatch, tmp_path):
    import pytest

    from cooperagents.env import apptainer
    from cooperagents.env.base import ExecResult

    env = object.__new__(ApptainerEnv)
    env._lock = threading.RLock()
    env._closed = False
    env._host_env = {"PATH": "/usr/bin:/bin"}
    env._argv = []
    env.repo_path = str(tmp_path)
    original = apptainer.run_limited
    with monkeypatch.context() as failure:
        failure.setattr(apptainer, "run_limited", lambda *a, **k: (ExecResult("FATAL: startup failed", 255), False))
        with pytest.raises(RuntimeError, match="before container shell startup"):
            env.execute("true")
    assert apptainer.run_limited is original
    assert env.execute("printf recovered").stdout == "recovered"


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
    def build(*args, **kwargs):
        assert kwargs["timeout"] == 1800
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("cooperagents.env.apptainer.subprocess.run", build)
    monkeypatch.setattr(ApptainerEnv, "execute", lambda *a, **k: ExecResult("commit\n", 0))
    env = task_environment("image", volumes=["shared:/cbshared"], coordinator_dir=directory)
    try:
        assert f"{directory}:/coordination:ro" in env._argv
        assert f"{tmp_path}/scratch/shared/shared:/cbshared" in env._argv
        assert (env.root / "fs/coordination").is_dir()
    finally:
        env.cleanup()


@pytest.mark.parametrize("fail", [False, True])
def test_benchmark_apptainer_git_share_uses_temporary_host_mount(monkeypatch, tmp_path, fail):
    from cooperagents.env.local import LocalEnv

    script = Path(__file__).parents[1] / "scripts/bench_compare.py"
    spec = importlib.util.spec_from_file_location("bench_compare", script)
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    manifest = tmp_path / "images.json"
    monkeypatch.setattr(bench, "image_name", lambda *a: "image")
    manifest.write_text(json.dumps({"image": "fixture.sif"}))
    monkeypatch.setenv("COOPER_RUNTIME", "apptainer")
    monkeypatch.setenv("COOPER_IMAGE_MANIFEST", str(manifest))
    monkeypatch.setenv("COOPER_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(bench, "read_feature", lambda *a: "feature")
    monkeypatch.setattr(bench, "write_run_outputs", lambda *a, **k: None)
    mounts, commands = [], []
    real_run = subprocess.run

    def run_command(args, *rest, **kwargs):
        assert args[0] != "docker"
        commands.append(args)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_command)

    def environment(sif, *, scratch, shared, coordinator_dir):
        host = Path(shared)
        assert host.is_absolute() and host.is_dir()
        mounts.append(host)
        env = object.__new__(ApptainerEnv)
        env._checkpoint_mounts = {"/cbshared": host}
        return env

    monkeypatch.setattr("cooperagents.env.apptainer.ApptainerEnv", environment)

    def run(self, team, *, env_factory, **kwargs):
        assert team.git_share
        worker = env_factory("agent1")
        env_factory("agent2")
        assert mounts[0] == mounts[1]
        bare = shlex.quote(str(mounts[0] / "repo.git"))
        seed = LocalEnv.fresh(workdir=str(tmp_path))
        try:
            worker._base_commit = seed.execute("git rev-parse HEAD").stdout.strip()
            seed.write_file("implementation.txt", "feature\n")
            result = seed.execute(
                f"git init -q --bare {bare} && git add . && git commit -qm 'implement' && git push -q {bare} HEAD:refs/heads/agent1"
            )
            assert result.exit_code == 0, result.stdout
            assert "+feature" in worker.recover_shared_diff("agent1")
        finally:
            seed.cleanup()
        if fail:
            raise RuntimeError("fixture rollout failed")
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={})

    monkeypatch.setattr(bench.UnifiedHarness, "run", run)
    item = bench.WorkItem("go_chi_task", 26, [1, 2])
    kwargs = dict(run_name="share", logs_dir=tmp_path, step_limit=10, max_agents=2, git_share=True)
    if fail:
        with pytest.raises(RuntimeError, match="fixture rollout failed"):
            bench.run_team(item, **kwargs)
    else:
        bench.run_team(item, **kwargs)
    assert not mounts[0].exists()
    assert ["git", f"--git-dir={mounts[0] / 'repo.git'}"] in [args[:2] for args in commands]
