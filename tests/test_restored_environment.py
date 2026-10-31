import pytest

from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.base import ExecResult


def state():
    return dict(
        repo_path="/workspace/repo",
        base_commit="a" * 40,
        runtime=dict(image="/old/image.sif", image_sha256="b" * 64, mounts=[]),
        git=dict(head={"stdout": "head\n"}, status={"stdout": ""}, stashes={"stdout": ""}),
    )


def test_restored_constructor_reuses_execute_prefix_and_owns_lifetime(tmp_path, monkeypatch):
    root = tmp_path / "rollout"
    root.mkdir()
    (root / "fs").mkdir()
    closed = []
    commands = []

    def execute(self, command, timeout=60):
        commands.append(command)
        return ExecResult("head\n" if command.endswith("rev-parse HEAD") else "", 0)

    monkeypatch.setattr(ApptainerEnv, "execute", execute)
    monkeypatch.setattr("cooperagents.checkpoint.preview_patch", lambda env: "")
    env = ApptainerEnv.from_filesystem(
        root, state(), exec_prefix=("nsenter", "--mount=/proc/1/fd/8"), cleanup=lambda: closed.append("closed"), checkpoint=None
    )
    assert env._argv[:2] == ["nsenter", "--mount=/proc/1/fd/8"]
    assert env.repo_path == "/workspace/repo"
    assert any("update-index" in c for c in commands)
    env.cleanup()
    env.cleanup()
    assert closed == ["closed"] and not root.exists()


def test_invalid_restored_metadata_cannot_install_command_prefix(tmp_path):
    with pytest.raises(ValueError):
        ApptainerEnv.from_filesystem(tmp_path, {**state(), "repo_path": "../escape"}, exec_prefix=())


def test_overlay_environment_cannot_capture_empty_host_mountpoint(tmp_path):
    env = ApptainerEnv.__new__(ApptainerEnv)
    env._restore_cleanup = lambda: None
    with pytest.raises(ValueError, match='capture'):
        env.checkpoint(tmp_path/'invalid-checkpoint')
    assert not (tmp_path/'invalid-checkpoint').exists()
