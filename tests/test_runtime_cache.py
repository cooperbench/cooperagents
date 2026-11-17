"""Offline cache admission/lifecycle tests; fake FUSE/overlay and Apptainer.

These verify the mount plan and isolated episode paths, not Linux overlayfs or
112-slot throughput. The deployment smoke must qualify those separately.
"""

import fcntl
import json
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cooperagents.checkpoint import sha256
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.runtime_cache import (
    TARGET,
    RuntimeCache,
    RuntimeCacheArtifact,
    RuntimeCacheIdentity,
    validate_transport,
)
from cooperagents.env.runtime_cache_mount import prepare_cache
from test_restored_environment import state


@pytest.fixture
def identity(tmp_path):
    for name in ["image.json", "pyproject.toml", "uv.lock"]:
        (tmp_path / name).write_text(name)
    return RuntimeCacheIdentity.from_files(
        repo="demo", task_id=1, image_sha256="b" * 64, image_manifest=tmp_path / "image.json",
        pyproject=tmp_path / "pyproject.toml", uv_lock=tmp_path / "uv.lock",
    )


@pytest.fixture
def fake_seals(monkeypatch):
    """Emulate only Linux sealed-fd syscalls so tests also run on macOS."""
    seals = {}
    real_fcntl = fcntl.fcntl
    for name, value in [("F_ADD_SEALS", 1033), ("F_GET_SEALS", 1034), ("F_SEAL_SEAL", 1),
                        ("F_SEAL_SHRINK", 2), ("F_SEAL_GROW", 4), ("F_SEAL_WRITE", 8)]:
        monkeypatch.setattr(fcntl, name, value, raising=False)
    monkeypatch.setattr(os, "MFD_ALLOW_SEALING", 2, raising=False)

    def create(*args):
        fd, name = tempfile.mkstemp()
        os.unlink(name)
        seals[fd] = 0
        return fd

    def control(fd, operation, *args):
        if operation == fcntl.F_ADD_SEALS:
            seals[fd] |= args[0]
            return 0
        if operation == fcntl.F_GET_SEALS:
            return seals.get(fd, 0)
        return real_fcntl(fd, operation, *args)

    def checksum(path):
        if str(path).startswith("/proc/self/fd/"):
            import hashlib

            fd = int(path.name)
            return hashlib.sha256(os.pread(fd, os.fstat(fd).st_size, 0)).hexdigest()
        return sha256(path)

    monkeypatch.setattr(os, "memfd_create", create, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", control)
    monkeypatch.setattr("cooperagents.env.runtime_cache.sha256", checksum)
    return seals


@pytest.fixture
def fake_mount_host(monkeypatch):
    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.squashfuse_helper", lambda: "/host/squashfuse_ll")
    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.namespace_identity",
                        lambda: {"namespace_uid": 1000, "uid_map": "1000 1000 1"})


@pytest.fixture
def artifact(tmp_path, monkeypatch, identity):
    def pack(args, **kwargs):
        # A deterministic fake SQFS stands in for an offline mksquashfs binary.
        Path(args[2]).write_bytes((Path(args[1]) / "wheel").read_bytes())
        return SimpleNamespace(returncode=0)

    with monkeypatch.context() as build:
        build.setattr("cooperagents.env.runtime_cache.subprocess.run", pack)
        return RuntimeCacheArtifact.build(tmp_path / "store", identity, lambda path: (path / "wheel").write_bytes(b"warm wheel"))


def test_key_binds_every_environment_and_input(identity):
    for name in ["repo", "task_id", "image_sha256", "image_manifest_sha256", "pyproject_sha256", "uv_lock_sha256"]:
        values = identity.model_dump()
        values[name] = 2 if name == "task_id" else "other" if name == "repo" else "a" * 64
        assert RuntimeCacheIdentity(**values).key != identity.key
    assert RuntimeCacheIdentity(**identity.model_dump()).key == identity.key


def test_default_off_argv_and_environment_match_base_bytes(tmp_path, monkeypatch):
    source = (Path(__file__).parent / "fixtures/runtime-cache/base_configure.py.txt").read_text()
    # Execute the exact method frozen from the specified base commit.
    import cooperagents.env.apptainer as module

    scope = dict(vars(module))
    exec("class BaseApptainer(ApptainerEnv):\n" + source, scope)
    monkeypatch.setenv("UV_CACHE_DIR", "/do-not-inherit")
    monkeypatch.setenv("APPTAINERENV_UV_CACHE_DIR", "/do-not-inherit")
    monkeypatch.setenv("SINGULARITYENV_UV_CACHE_DIR", "/do-not-inherit")
    root = tmp_path / "episode"
    root.mkdir()
    with_mount = state()
    with_mount["runtime"]["mounts"] = [dict(target="/cbshared", readonly=False), dict(target="/coordination", readonly=True)]
    baseline = scope["BaseApptainer"]._configure_filesystem(root, with_mount, ("nsenter", "--mount=test"), None)
    baseline_bytes = json.dumps([baseline._argv, baseline._host_env], sort_keys=True).encode()
    (root / "host-home").rmdir()
    current = ApptainerEnv._configure_filesystem(root, with_mount, ("nsenter", "--mount=test"), None)
    assert json.dumps([current._argv, current._host_env], sort_keys=True).encode() == baseline_bytes
    assert not (root / "receipts.json").exists()
    current.cleanup()


def test_cache_argv_adds_only_private_bind_and_explicit_env(tmp_path):
    root = tmp_path / "episode"
    root.mkdir()
    env = ApptainerEnv._configure_filesystem(root, state(), (), None)
    original, host_env = list(env._argv), dict(env._host_env)
    env._enable_runtime_cache()
    assert env._argv == original[:-3] + ["--bind", f"{root}/runtime-cache/fs:{TARGET}", "--env", f"UV_CACHE_DIR={TARGET}"] + original[-3:]
    assert env._host_env == host_env
    env.cleanup()


def test_concurrent_builders_publish_one_immutable_key(tmp_path, identity, monkeypatch):
    warmed, packed = [], []

    def warm(path):
        warmed.append(path)
        (path / "wheel").write_bytes(b"warm")

    def pack(args, **kwargs):
        packed.append(args)
        Path(args[2]).write_bytes(b"sqfs")

    monkeypatch.setattr("cooperagents.env.runtime_cache.subprocess.run", pack)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: RuntimeCacheArtifact.build(tmp_path / "store", identity, warm), range(8)))
    assert len(warmed) == len(packed) == 1
    assert len({r.receipt_sha256 for r in results}) == 1
    assert [p.name for p in (tmp_path / "store").iterdir() if p.is_dir()] == [identity.key]
    assert all(not p.stat().st_mode & 0o222 for p in results[0].directory.iterdir())


def test_failed_builder_never_publishes(tmp_path, identity):
    def fail(path):
        (path / "partial").touch()
        raise RuntimeError("warm failed")

    with pytest.raises(RuntimeError, match="warm failed"):
        RuntimeCacheArtifact.build(tmp_path / "store", identity, fail)
    assert not (tmp_path / "store" / identity.key).exists()
    assert not list((tmp_path / "store").glob(".building-*"))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "receipt", "mismatch"])
def test_bad_artifact_falls_back(artifact, fake_seals, damage, caplog):
    if damage == "missing":
        artifact = artifact.model_copy(update={"directory": artifact.directory / "missing"})
    elif damage == "mismatch":
        artifact = artifact.model_copy(update={"identity": artifact.identity.model_copy(update={"task_id": 999})})
    else:
        path = artifact.directory / ("uv.sqfs" if damage == "corrupt" else "receipt.json")
        path.chmod(0o644)
        path.write_bytes(b"corrupt")
        path.chmod(0o444)
    cache = artifact.open()
    assert cache.descriptor == -1
    assert cache.transport()["descriptor"] == -1
    assert "fallback" in caplog.text


def test_sealed_admission_and_closed_descriptor_fallback(artifact, fake_seals):
    cache = artifact.open()
    assert cache.descriptor >= 0
    assert fake_seals[cache.descriptor] == 15
    cache.close()
    cache.close()
    assert cache.transport()["descriptor"] == -1


def episode(root, identity):
    repo = root / "fs/workspace/repo"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("pyproject.toml")
    (repo / "uv.lock").write_text("uv.lock")
    return SimpleNamespace(checkpoint=root / "checkpoint", reference=SimpleNamespace(image_sha256=identity.image_sha256), state=state())


def patch_inputs(monkeypatch, identity):
    monkeypatch.setattr("cooperagents.repair.load_repair_input", lambda _: SimpleNamespace(
        settings=SimpleNamespace(repo=identity.repo, task_id=identity.task_id)))


@pytest.mark.parametrize("damage", ["missing", "task", "repo", "image", "pyproject", "uv.lock", "descriptor", "seals", "mount"])
def test_holder_fallback_leaves_original_argv(artifact, fake_seals, fake_mount_host, tmp_path, monkeypatch, damage, caplog):
    with artifact.open() as cache:
        root = tmp_path / "episode"
        publication = episode(root, artifact.identity)
        patch_inputs(monkeypatch, artifact.identity)
        config = cache.transport()
        if damage == "missing":
            config = RuntimeCache(-1, {}).transport()
        elif damage in {"task", "repo"}:
            patch_inputs(monkeypatch, artifact.identity.model_copy(update={"task_id": 2} if damage == "task" else {"repo": "other"}))
        elif damage == "image":
            publication.reference.image_sha256 = "a" * 64
        elif damage in {"pyproject", "uv.lock"}:
            (root / "fs/workspace/repo" / ("pyproject.toml" if damage == "pyproject" else damage)).write_text("changed")
        elif damage == "descriptor":
            config["proof"] = {**config["proof"], "fd_identity": [0, 0, 0]}
        elif damage == "seals":
            fake_seals[cache.descriptor] = 0
        else:
            monkeypatch.setattr("cooperagents.env.runtime_cache_mount.subprocess.Popen", Mock(side_effect=OSError("no FUSE")))
        record, helper = prepare_cache(config, root, publication)
        assert record["status"] == "fallback" and helper is None
        assert "fallback" in caplog.text
        env = ApptainerEnv._configure_filesystem(root, state(), (), None)
        assert not any("runtime-cache" in arg or "UV_CACHE_DIR" in arg for arg in env._argv)
        env.cleanup()


def test_overlay_plan_and_episode_writes_are_private(artifact, fake_seals, fake_mount_host, tmp_path, monkeypatch):
    patch_inputs(monkeypatch, artifact.identity)
    commands, helpers = [], []

    def start(args, **kwargs):
        assert args[args.index("-o") + 1] == "ro"
        assert kwargs["pass_fds"]
        # Simulate the read-only mounted SQFS tree.
        (Path(args[-1]) / "wheel").write_bytes(b"warm wheel")
        helper = Mock()
        helpers.append(helper)
        return helper

    def mount(args, **kwargs):
        commands.append(args)
        assert args[:4] == ["mount", "-t", "overlay", "overlay"]
        assert "userxattr,index=off,metacopy=off" in args[-2]
        directory = Path(args[-1]).parent
        # Fake overlay materialization: writes below must stay episode local.
        shutil.copytree(directory / "lower", directory / "fs", dirs_exist_ok=True)

    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.subprocess.Popen", start)
    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.subprocess.run", mount)
    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.os.path.ismount", lambda _: True)
    before = sha256(artifact.directory / "uv.sqfs")
    with artifact.open() as cache:
        roots = [tmp_path / "episode-1", tmp_path / "episode-2"]
        for root in roots:
            publication = episode(root, artifact.identity)
            record, helper = prepare_cache(cache.transport(), root, publication)
            assert record["status"] == "hit" and helper in helpers
            assert record["identity"] == artifact.identity.model_dump()
            assert record["size_bytes"] == len(b"warm wheel")
            assert record["warmup_seconds"] >= 0 and record["created_at"]
            for name in ["upper", "work"]:
                assert f"{name}dir={root}/runtime-cache/{name}" in commands[-1][-2]
        (roots[0] / "runtime-cache/fs/wheel").write_text("edited")
        (roots[0] / "runtime-cache/fs/miss").write_text("download")
        for name in ["pyproject.toml", "uv.lock"]:
            (roots[0] / "fs/workspace/repo" / name).write_text("edited")
        assert (roots[1] / "runtime-cache/fs/wheel").read_bytes() == b"warm wheel"
        assert not (roots[1] / "runtime-cache/fs/miss").exists()
        validate_transport(cache.transport(), repo="demo", task_id=1, image="b" * 64,
                           filesystem=roots[1] / "fs", repo_path="/workspace/repo")
        assert sha256(artifact.directory / "uv.sqfs") == before
        for root in roots:
            env = ApptainerEnv._configure_filesystem(root, state(), (), lambda: None)
            env.cleanup()
            assert not root.exists()
        assert cache.descriptor >= 0  # Episodes borrow; only the pool closes it.


def test_fake_apptainer_observes_explicit_cache_env(tmp_path, monkeypatch):
    executable = tmp_path / "apptainer"
    log = tmp_path / "argv.json"
    # Fake binary consumes precisely the transport's stdin; no real SIF or network.
    executable.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "os.execv('/bin/bash', ['bash', '-s'])\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    root = tmp_path / "episode"
    root.mkdir()
    env = ApptainerEnv._configure_filesystem(root, state(), (), None)
    env.repo_path = str(tmp_path)
    env._enable_runtime_cache()
    assert env.execute("printf ready").stdout == "ready"
    argv = json.loads(log.read_text())
    assert argv.count("--env") == 1 and argv[argv.index("--env") + 1] == f"UV_CACHE_DIR={TARGET}"
    assert argv[-2:] == ["bash", "-s"]
    env.cleanup()


@pytest.mark.parametrize("status", ["hit", "fallback"])
def test_restore_receipts_metrics_and_cleanup(artifact, fake_seals, tmp_path, monkeypatch, status):
    import time

    from cooperagents.published_restore import _restore_with_base

    with artifact.open() as cache:
        root = tmp_path / "restored"
        root.mkdir()
        record = (dict(status="hit", key=artifact.identity.key, **cache.proof["receipt"]) if status == "hit"
                  else dict(status="fallback", reason="cache missing"))
        close = Mock()
        holder = SimpleNamespace(root=root, ready=dict(state=state(), risks={}, runtime_cache=record),
                                 exec_prefix=lambda: (), close=close)
        constructor = Mock(return_value=holder)
        monkeypatch.setattr("cooperagents.published_restore.MountHolder", constructor)
        monkeypatch.setattr(ApptainerEnv, "_validate_restored", lambda *a: None)
        monkeypatch.setattr("cooperagents.published_restore._probe", lambda env: {"returncode": 0})
        base = SimpleNamespace(proof={"preparation_seconds": 0}, close=Mock())
        reference = SimpleNamespace(version="v3", format="repair-overlay-v3", publication=SimpleNamespace(sha256="c" * 64))
        env = _restore_with_base(reference, tmp_path / "checkpoint", tmp_path, base, time.monotonic(), False,
                                 "training", runtime_cache=cache)
        assert constructor.call_args.kwargs["runtime_cache"] is cache
        assert json.loads((root / "receipts.json").read_text()) == {"runtime_cache": record}
        assert env.restore_metrics["runtime_cache"] == record
        assert any("UV_CACHE_DIR" in arg for arg in env._argv) == (status == "hit")
        env.cleanup()
        env.cleanup()
        close.assert_called_once()
        base.close.assert_not_called()
        assert not root.exists()
        assert cache.descriptor >= 0


def test_build_accepts_uv_internal_links_and_rejects_escape(tmp_path, identity, monkeypatch):
    def pack(args, **kwargs):
        assert (Path(args[1]) / "index").is_symlink()
        Path(args[2]).write_bytes(b"sqfs")

    def warm(path):
        (path / "wheel").write_bytes(b"wheel")
        (path / "index").symlink_to("wheel")

    monkeypatch.setattr("cooperagents.env.runtime_cache.subprocess.run", pack)
    RuntimeCacheArtifact.build(tmp_path / "good", identity, warm)

    def escape(path):
        (path / "venv").symlink_to("/another/task/.venv")

    with pytest.raises(ValueError, match="link escapes"):
        RuntimeCacheArtifact.build(tmp_path / "bad", identity, escape)
    assert not (tmp_path / "bad" / identity.key).exists()


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="real sealed descriptors require Linux")
def test_real_linux_consumer_cannot_write_sealed_layer(artifact):
    with artifact.open() as cache:
        assert cache.descriptor >= 0
        with pytest.raises(OSError):
            os.write(cache.descriptor, b"poison")
        with pytest.raises(OSError):
            os.ftruncate(cache.descriptor, 0)


def test_eval_cache_and_repair_use_same_mount_plan(artifact, fake_seals, fake_mount_host, tmp_path, monkeypatch):
    patch_inputs(monkeypatch, artifact.identity)
    commands = []
    helper = Mock()
    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.subprocess.Popen", Mock(return_value=helper))
    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.os.path.ismount", lambda _: True)
    monkeypatch.setattr("cooperagents.env.runtime_cache_mount.subprocess.run", lambda args, **kw: commands.append(args))
    with artifact.open() as cache:
        training_root, eval_root = tmp_path / "training", tmp_path / "eval"
        publication = episode(training_root, artifact.identity)
        episode(eval_root, artifact.identity)
        training, _ = prepare_cache(cache.transport(), training_root, publication)
        evaluation, _ = prepare_cache(cache.transport(), eval_root, task=("demo", 1), image="b" * 64)
        assert training == evaluation
        assert str(training_root) not in " ".join(commands[1])
        assert str(eval_root) not in " ".join(commands[0])
        assert commands[0][-2].replace(str(training_root), "ROOT") == commands[1][-2].replace(str(eval_root), "ROOT")


@pytest.mark.parametrize("shared", [True, False])
def test_eval_backend_forwards_capability_and_reuses_admitted_base(artifact, fake_seals, tmp_path, monkeypatch, shared):
    from cooperagents.eval.apptainer import ApptainerEvalBackend
    from cooperagents.eval.dataset import image_name

    base = Mock()
    env = Mock(runtime_cache_receipt={"status": "hit"})
    shared_factory = Mock(return_value=env)
    prepare = Mock(return_value=base)
    cold = Mock(side_effect=AssertionError("cold constructor forbidden for cache hit"))
    monkeypatch.setattr("cooperagents.eval.apptainer.ApptainerEnv", cold)
    monkeypatch.setattr(cold, "from_image_base", shared_factory, raising=False)
    monkeypatch.setattr("cooperagents.eval.apptainer.ImageBase.prepare", prepare)
    monkeypatch.setattr("cooperagents.eval.apptainer.sha256", lambda _: pytest.fail("per-sandbox SIF hash forbidden"))
    with artifact.open() as cache:
        image = image_name("demo", 1)
        backend = ApptainerEvalBackend({image: "/image.sif"}, str(tmp_path), runtime_caches={image: cache},
                                      **({"image_base": base, "shared_image": ("/image.sif", "b" * 64)} if shared else {}))
        sandbox = backend.create_sandbox(image)
        assert shared_factory.call_args.kwargs["runtime_cache"] is cache
        assert shared_factory.call_args.kwargs["cache_task"] == ("demo", 1)
        assert prepare.call_count == (0 if shared else 1)
        assert sandbox.check_base
        sandbox.terminate()
        env.cleanup.assert_called_once()
        base.close.assert_not_called()
        assert cache.descriptor >= 0
        # A second sandbox borrows the already-admitted base without rehashing.
        second = backend.create_sandbox(image)
        assert prepare.call_count == (0 if shared else 1)
        second.terminate()
        backend.close()
        backend.close()
        assert base.close.call_count == (0 if shared else 1)


@pytest.mark.parametrize("reason", ["missing", "task", "image", "manifest", "mount"])
def test_eval_cache_failure_returns_original_constructor(artifact, fake_seals, tmp_path, monkeypatch, reason):
    from cooperagents.eval.apptainer import ApptainerEvalBackend
    from cooperagents.eval.dataset import image_name

    with artifact.open() as cache:
        image = image_name("demo", 1)
        cold_env = Mock()
        native = Mock(return_value=cold_env)
        warm_env = Mock(runtime_cache_receipt={"status": "fallback", "reason": "mount failed"})
        shared_factory = Mock(return_value=warm_env)
        base = Mock()
        monkeypatch.setattr("cooperagents.eval.apptainer.ApptainerEnv", native)
        monkeypatch.setattr(native, "from_image_base", shared_factory, raising=False)
        prepare = Mock(side_effect=ValueError("SIF identity mismatch")) if reason == "image" else Mock(return_value=base)
        monkeypatch.setattr("cooperagents.eval.apptainer.ImageBase.prepare", prepare)
        if reason == "missing":
            cache.close()
        if reason == "task":
            image = image_name("demo", 999)
        backend = ApptainerEvalBackend({image: "/image.sif"}, str(tmp_path), runtime_caches={image: cache},
                                      image_manifest_sha256="a" * 64 if reason == "manifest" else None)
        sandbox = backend.create_sandbox(image)
        native.assert_called_once_with("/image.sif", scratch=str(tmp_path))
        assert cold_env.repo_path == "/workspace"
        record = cold_env._record_runtime_cache.call_args.args[0]
        assert record["status"] == "fallback" and record["reason"]
        if reason == "mount":
            warm_env.cleanup.assert_called_once()
        else:
            shared_factory.assert_not_called()
        sandbox.terminate()


def test_eval_holder_records_receipt_and_keeps_borrowed_base(tmp_path, monkeypatch):
    from cooperagents.env.base import ExecResult
    from cooperagents.env.published_holder import MountHolder

    root = tmp_path / "eval"
    root.mkdir()
    holder = SimpleNamespace(root=root, ready={"runtime_cache": {"status": "hit", "key": "a" * 64}},
                             exec_prefix=lambda: (), close=Mock())
    factory = Mock(return_value=holder)
    monkeypatch.setattr(MountHolder, "for_image", factory)
    monkeypatch.setattr(ApptainerEnv, "execute", lambda _, cmd: ExecResult("a" * 40 if cmd.startswith("git ") else "EVALUATOR_READY", 0))
    base = Mock()
    cache = RuntimeCache(-1, {})
    env = ApptainerEnv.from_image_base(Path("/image.sif"), "b" * 64, scratch=tmp_path, base=base,
                                      runtime_cache=cache, cache_task=("demo", 1))
    assert factory.call_args.kwargs == {"runtime_cache": cache, "cache_task": ("demo", 1)}
    assert json.loads((root / "receipts.json").read_text())["runtime_cache"]["status"] == "hit"
    assert env._argv.count("--env") == 1
    env.cleanup()
    holder.close.assert_called_once()
    base.close.assert_not_called()
    assert not root.exists()


@pytest.mark.parametrize("failure", [None, "uid", "helper_exists", "squashfuse_mount", "overlay_mount",
                                     "private-write", "leak", "fallback"])
def test_preflight_requires_real_mount_checks_and_private_upper(artifact, fake_seals, tmp_path, monkeypatch, failure):
    import re

    from cooperagents.env.base import ExecResult
    from cooperagents.env.runtime_cache_preflight import preflight

    base = Mock()
    base.__enter__ = Mock(return_value=base)
    base.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("cooperagents.env.runtime_cache_preflight.ImageBase.prepare", Mock(return_value=base))
    opened = []

    def sandbox(*args, **kwargs):
        root = tmp_path / f"preflight-{len(opened)}"
        (root / "runtime-cache/upper").mkdir(parents=True)
        checks = dict(helper_exists=True, squashfuse_mount=True, overlay_mount=True)
        if failure in checks:
            checks[failure] = False
        record = dict(status="hit", mount_checks=checks)
        if failure == "fallback":
            record = dict(status="fallback", reason="overlay unavailable")

        def execute(command):
            marker = re.search(r"\.cooperagents-preflight-[a-f0-9]+", command)[0]
            if failure != "private-write":
                (root / "runtime-cache/upper" / marker).write_text("private")
            return ExecResult("", 0)

        env = SimpleNamespace(root=root, runtime_cache_receipt=record, execute=execute,
                              cleanup=lambda: None if failure == "leak" else shutil.rmtree(root))
        opened.append(env)
        return env

    monkeypatch.setattr("cooperagents.env.runtime_cache_preflight.ApptainerEnv.from_image_base", sandbox)
    if failure is not None:
        with pytest.raises((ValueError, OSError)):
            preflight(artifact, tmp_path / "image.sif", tmp_path, expected_uid=os.geteuid() + (failure == "uid"))
        assert len(opened) <= 1
    else:
        result = preflight(artifact, tmp_path / "image.sif", tmp_path, expected_uid=os.geteuid())
        assert result["status"] == "hit" and result["private_write_isolated"] and result["cleanup_verified"]
        assert len(result["sandboxes"]) == len(opened) == 2
    assert all(not env.root.exists() for env in opened) or failure == "leak"


def test_preflight_cli_fails_closed_and_writes_failure_receipt(artifact, tmp_path, monkeypatch):
    from cooperagents.env import runtime_cache_preflight as module

    locator, receipt = tmp_path / "locator.json", tmp_path / "preflight.json"
    locator.write_text(artifact.model_dump_json())
    monkeypatch.setattr("sys.argv", ["preflight", "--artifact", str(locator), "--image", "/image.sif", "--scratch", str(tmp_path),
                                     "--expected-uid", str(os.geteuid()), "--receipt", str(receipt)])
    monkeypatch.setattr(module, "preflight", Mock(side_effect=ValueError("mount denied under run identity")))
    assert module.main() == 1
    result = json.loads(receipt.read_text())
    assert result["status"] == "failed" and "mount denied" in result["reason"]


def test_helper_resolution_fails_when_no_executable_exists(monkeypatch):
    from cooperagents.env import runtime_cache_mount as module

    path = Mock()
    path.is_file.return_value = False
    monkeypatch.setattr(module, "Path", Mock(return_value=path))
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="helper is unavailable"):
        module.squashfuse_helper()
    monkeypatch.setattr(module.shutil, "which", lambda _: "/custom/squashfuse_ll")
    assert module.squashfuse_helper() == "/custom/squashfuse_ll"
