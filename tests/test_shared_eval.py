from pathlib import Path
from unittest.mock import Mock

import pytest

from cooperagents.env import image_base
from cooperagents.eval.apptainer import ApptainerEvalBackend


def test_pack_image_bounds_compression_cache(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(image_base.subprocess, "run", run)
    image_base._pack_image(Path("/image.sif"), tmp_path)
    args = run.call_args_list[-1].args[0]
    assert args[args.index("-mem") + 1] == "256M"


def test_shared_evaluator_does_not_build_a_new_base(tmp_path, monkeypatch):
    base = Mock()
    env = Mock()
    native = Mock(side_effect=AssertionError("native expansion forbidden"))
    shared = Mock(return_value=env)
    monkeypatch.setattr("cooperagents.eval.apptainer.ApptainerEnv", native)
    monkeypatch.setattr(native, "from_image_base", shared, raising=False)
    backend = ApptainerEvalBackend({"task": "/image.sif"}, str(tmp_path), image_base=base, shared_image=("/image.sif", "a" * 64))
    sandbox = backend.create_sandbox("task", 42, "/workspace")
    native.assert_not_called()
    shared.assert_called_once()
    assert shared.call_args.kwargs["base"] is base
    assert sandbox.timeout == 42
    sandbox.terminate()
    env.cleanup.assert_called_once()
    base.close.assert_not_called()


def test_shared_evaluator_rejects_other_image_before_mount(tmp_path):
    backend = ApptainerEvalBackend({"task": "/different.sif"}, str(tmp_path), image_base=Mock(), shared_image=("/image.sif", "a" * 64))
    with pytest.raises(ValueError, match="image"):
        backend.create_sandbox("task")


def test_eval_command_carries_authenticated_identity(tmp_path):
    from cooperagents.eval.cooperbench import run_eval

    (tmp_path / "dataset").mkdir()
    command = run_eval(
        "run", logs_dir=tmp_path, cooperbench_dir=tmp_path, backend="apptainer", dry_run=True, shared_image=("/image.sif", "a" * 64)
    )
    assert command[command.index("--shared-image-path") + 1] == "/image.sif"
    assert command[command.index("--shared-image-sha256") + 1] == "a" * 64


def test_shared_evaluator_requires_capability_and_identity_together(tmp_path):
    with pytest.raises(ValueError, match="together"):
        ApptainerEvalBackend({}, str(tmp_path), image_base=Mock())
    with pytest.raises(ValueError, match="together"):
        ApptainerEvalBackend({}, str(tmp_path), shared_image=("/image.sif", "a" * 64))


def test_failed_clean_image_probe_releases_holder_and_keeps_borrowed_base(tmp_path, monkeypatch):
    from cooperagents.env.apptainer import ApptainerEnv
    from cooperagents.env.published_holder import MountHolder

    holder = Mock(root=tmp_path / "holder")
    holder.root.mkdir()
    monkeypatch.setattr(MountHolder, "for_image", Mock(return_value=holder))
    env = Mock()
    env.execute.side_effect = RuntimeError("shell unavailable")
    env.cleanup.side_effect = holder.close
    monkeypatch.setattr(ApptainerEnv, "_configure_filesystem", Mock(return_value=env))
    base = Mock()
    with pytest.raises(RuntimeError, match="shell unavailable"):
        ApptainerEnv.from_image_base(Path("/image.sif"), "a" * 64, scratch=tmp_path, base=base)
    holder.close.assert_called_once()
    base.close.assert_not_called()


def test_invalid_workdir_cannot_create_holder(tmp_path, monkeypatch):
    from cooperagents.env.apptainer import ApptainerEnv
    from cooperagents.env.published_holder import MountHolder

    factory = Mock()
    monkeypatch.setattr(MountHolder, "for_image", factory)
    with pytest.raises(ValueError, match="working directory"):
        ApptainerEnv.from_image_base(Path("/image.sif"), "a" * 64, scratch=tmp_path, base=Mock(), workdir="/workspace/../tmp")
    factory.assert_not_called()


@pytest.mark.parametrize("code,output", [(127, "bash unavailable"), (0, ""), (1, "EVALUATOR_BASE_OK")])
def test_shared_command_failure_of_base_is_infrastructure_error(code, output):
    from cooperagents.eval.apptainer import EvalSandbox

    env = Mock()
    env.execute.side_effect = [Mock(exit_code=1, stdout="test failed"), Mock(exit_code=code, stdout=output)]
    sandbox = EvalSandbox(env, 30, check_base=True)
    with pytest.raises(RuntimeError, match="image base"):
        sandbox.exec("pytest")


def test_shared_valid_test_failure_remains_test_failure():
    from cooperagents.eval.apptainer import EvalSandbox

    env = Mock()
    env.execute.side_effect = [Mock(exit_code=1, stdout="assertion"), Mock(exit_code=0, stdout="EVALUATOR_BASE_OK")]
    result = EvalSandbox(env, 30, check_base=True).exec("pytest")
    assert result.returncode == 1 and result.output == "assertion"
