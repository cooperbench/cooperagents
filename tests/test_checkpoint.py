"""Checkpoint boundaries round-trip real Git state without containers/inference."""

import json
import tarfile
from pathlib import Path

import pytest

from cooperagents.checkpoint import preview_patch, save_checkpoint, verify_checkpoint
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.types import AgentResult, Assignment, TeamSpec


def test_preview_preserves_tracked_files_that_match_gitignore(tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        env.write_file("tracked.txt", "base\n")
        assert env.execute("git add tracked.txt && git commit -qm tracked").exit_code == 0
        env._base_commit = env.execute("git rev-parse HEAD").stdout.strip()
        env.write_file(".gitignore", "tracked.txt\n")
        env.write_file("staged.txt", "staged\n")
        assert env.execute("git add .gitignore staged.txt").exit_code == 0
        env.write_file("staged.txt", "unstaged\n")
        index = Path(env.repo_path) / ".git/index"
        before = index.read_bytes()
        preview = preview_patch(env)
        assert index.read_bytes() == before
        assert "diff --git a/tracked.txt" not in preview
        assert preview == env.git_diff()
    finally:
        env.cleanup()


def test_delivery_snapshot_precedes_staging_and_preserves_non_patch_files(tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        env.write_file(".gitignore", "ignored\n")
        env.write_file("file.txt", "staged\n")
        env.execute("git add .gitignore file.txt")
        env.write_file("file.txt", "unstaged\n")
        env.write_file("ignored", "runtime cache\n")
        env.write_file("tests/test_check.py", "assert True\n")
        env.write_file(".cb_checks/f1.py", "assert True\n")
        binary = bytes(range(256))
        (Path(env.repo_path) / "binary").write_bytes(binary)
        env.execute("ln -s file.txt link; chmod +x file.txt")
        before = env.execute("git status --porcelain=v1 --untracked-files=all").stdout
        assert "GIT binary patch" in preview_patch(env)
        assert env.execute("git status --porcelain=v1 --untracked-files=all").stdout == before
        path = tmp_path / "worker"
        patch = save_checkpoint(env, path, metadata=dict(boundary="worker_end_before_collect_diff"),
                                collect_patch=lambda: (env.git_diff(), "working_tree"))
        assert "test_check.py" not in patch and ".cb_checks" in patch
        assert ".cb_checks" not in (path / "submission.patch").read_text()
        assert "test_check.py" in (path / "raw.patch").read_text()
        state = verify_checkpoint(path)
        assert state["git"]["status"]["stdout"] == before
        restored = tmp_path / "restored"
        with tarfile.open(path / "repo.tar.gz") as archive:
            archive.extractall(restored, filter="data")
        clone = LocalEnv(str(restored), base_commit=env._base_commit)
        assert clone.execute("git status --porcelain=v1 --untracked-files=all").stdout == before
        assert clone.execute("git show :file.txt").stdout == "staged\n"
        assert clone.read_file("file.txt") == "unstaged\n"
        assert clone.read_file("ignored") == "runtime cache\n"
        assert (restored / "binary").read_bytes() == binary
        assert (restored / "link").is_symlink()
        assert (restored / "file.txt").stat().st_mode & 0o111
        (path / "raw.patch").write_text("corrupted")
        with pytest.raises(ValueError, match="checksum mismatch"):
            verify_checkpoint(path)
        with pytest.raises(FileExistsError):
            save_checkpoint(env, path, metadata=dict(boundary="duplicate"))
    finally:
        env.cleanup()


@pytest.mark.parametrize("repair,broken", [(False, True), (True, False), (True, True)])
def test_team_captures_actual_delivery_and_repair_handoff(monkeypatch, tmp_path, repair, broken):
    import cooperagents.workers.mini_swe_worker as worker

    def run(env, *, agent_id, role, **kwargs):
        if agent_id == "agent1":
            env.write_file("a.py", "broken(\n" if broken else "value = 1\n")
            status = "limit"  # Ending on a budget limit is still a captured delivery.
        elif agent_id == "agent2":
            env.write_file("b.py", "value = 2\n")
            status = "submitted"
        else:
            assert env.read_file("a.py") == "broken(\n"
            env.write_file("a.py", "value = 1\n")
            status = "submitted"
        return AgentResult(agent_id=agent_id, role=role, status=status, steps=1, messages=[dict(role="user", content="task")])

    monkeypatch.setattr(worker, "run_mini_swe_agent", run)
    spec = TeamSpec(run_id="checkpoint", repo="demo", task_id=1, features=[1, 2],
                    assignments=[Assignment(agent_id=f"agent{i}", task="task", feature_id=i) for i in (1, 2)],
                    shared_workspace=True, seed_prior=False, coop_tools=True, worker="mini_swe",
                    repair_integrator=repair, repair_attempts=2)
    root = tmp_path / "checkpoints"
    result = UnifiedHarness(checkpoint_dir=root).run(spec, env_factory=lambda _: LocalEnv.fresh(workdir=str(tmp_path)))
    before = verify_checkpoint(root / "pre-repair")
    after = verify_checkpoint(root / "post-repair")
    assert before["metadata"]["healthy"] == (not broken if repair else None)
    assert (root / "post-repair/integration.patch").read_text() == result.integrated.patch
    worker_state = verify_checkpoint(root / "worker-agent1")
    assert worker_state["metadata"]["result"]["status"] == "limit"
    assert "a.py" in (root / "worker-agent1/integration.patch").read_text()
    assert json.loads((root / "run.json").read_text())["spec"]["repair_integrator"] == repair
    if repair and broken:
        assert set(result.seeds) == {"agent1", "agent2", "integrator1"}
        handoff = verify_checkpoint(root / "before-integrator1")
        assert handoff["metadata"]["task"]
        assert before["metadata"]["gate_checks"][0]["exit_code"] == 1
        assert after["metadata"]["healthy"] is True
        assert after["metadata"]["attempts"][0]["duration_seconds"] > 0
        assert "broken(" in (root / "pre-repair/raw.patch").read_text()
        assert "broken(" not in result.integrated.patch
    else:
        assert not after["metadata"]["attempts"]
        assert not (root / "before-integrator1").exists()


def test_failed_capture_has_no_completion_manifest(monkeypatch, tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        monkeypatch.setattr(env, "checkpoint", lambda _: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            save_checkpoint(env, tmp_path / "failed", metadata=dict(boundary="worker"))
        assert not (tmp_path / "failed/manifest.json").exists()
    finally:
        env.cleanup()


def test_apptainer_archives_rootfs_and_external_mounts(tmp_path):
    import threading

    from cooperagents.env.apptainer import ApptainerEnv

    env = object.__new__(ApptainerEnv)
    env._lock = threading.RLock()
    env._closed = False
    env.root = tmp_path / "sandbox"
    (env.root / "fs/tmp").mkdir(parents=True)
    (env.root / "fs/tmp/cache").write_text("runtime cache")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "branch").write_text("worker head")
    env.image = tmp_path / "base.sif"
    env.image.write_bytes(b"image")
    env._checkpoint_mounts = {"/cbshared": shared}
    output = tmp_path / "snapshot"
    output.mkdir()
    saved = env.checkpoint(output)
    assert saved["image_sha256"]
    assert saved["mounts"] == [dict(target="/cbshared", archive="mount-0.tar.gz", readonly=False)]
    with tarfile.open(output / "rootfs.tar.gz") as archive:
        assert archive.extractfile("./tmp/cache").read() == b"runtime cache"
    with tarfile.open(output / "mount-0.tar.gz") as archive:
        assert archive.extractfile("./branch").read() == b"worker head"


def test_git_sync_waits_for_capture_and_resumes_afterwards():
    import threading
    from types import SimpleNamespace

    from cooperagents.harness import _GitShareSync

    entered, executed = threading.Event(), threading.Event()
    sync = _GitShareSync({"agent1": SimpleNamespace(execute=lambda *a, **k: executed.set())})
    waits = iter([False, True])
    sync._stop = SimpleNamespace(wait=lambda _: (entered.set(), next(waits))[1])
    with sync._locks["agent1"]:
        thread = threading.Thread(target=sync.run)
        thread.start()
        assert entered.wait(1)
        assert not executed.wait(0.02)
    assert executed.wait(1)
    thread.join(1)
    assert not thread.is_alive()
    executed.clear()
    waits = iter([False, True])
    sync.run()
    assert executed.is_set()


@pytest.mark.parametrize("failure,already_paused", [(False, False), (True, False), (False, True)])
def test_docker_snapshot_exports_mounts_and_always_unpauses(monkeypatch, tmp_path, failure, already_paused):
    from cooperagents.env.docker import DockerEnv

    env = object.__new__(DockerEnv)
    env.name, env.image = "worker", "image"
    info = dict(State=dict(Paused=already_paused), Image="sha256:base", Config=dict(WorkingDir="/workspace/repo"),
                HostConfig=dict(NetworkMode="none"),
                Mounts=[dict(Type="volume", Destination="/cbshared", RW=True)])
    monkeypatch.setattr("cooperagents.env.docker.subprocess.check_output", lambda _: json.dumps([info]).encode())
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "export":
            if failure:
                raise OSError("export failed")
            Path(argv[3]).write_bytes(b"filesystem archive")
        if argv[1] == "cp":
            copied = Path(argv[3])
            copied.mkdir()
            (copied / "branch").write_text("worker head")

    monkeypatch.setattr("cooperagents.env.docker.subprocess.run", run)
    if failure:
        with pytest.raises(OSError, match="export failed"):
            env.checkpoint(tmp_path)
    else:
        saved = env.checkpoint(tmp_path)
        assert saved["image_id"] == "sha256:base"
        assert saved["network_mode"] == "none"
        assert saved["mounts"] == [dict(target="/cbshared", archive="mount-0.tar.gz", readonly=False)]
        with tarfile.open(tmp_path / "mount-0.tar.gz") as archive:
            assert archive.extractfile("./branch").read() == b"worker head"
    if already_paused:
        assert not any(call[1] in {"pause", "unpause"} for call in calls)
    else:
        assert calls[0] == ["docker", "pause", "worker"]
        assert calls[-1] == ["docker", "unpause", "worker"]
