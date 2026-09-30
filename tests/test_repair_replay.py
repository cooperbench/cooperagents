"""Real filesystem/Git replay with an in-process container and fake model transport."""

import io
import json
import os
import tarfile
import threading
from dataclasses import asdict
from functools import partial
from pathlib import Path

import pytest
from litellm import ModelResponse

from cooperagents.checkpoint import extract_tree, save_checkpoint, sha256, verify_checkpoint
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.base import ExecResult
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.repair import (
    RepairInput,
    capture_repair_input,
    describe_gate,
    import_legacy_repair_input,
    load_repair_input,
    run_repair_checkpoint,
)
from cooperagents.types import Assignment, TeamSpec
from cooperagents.verification import validate
from cooperagents.workers import mini_swe_worker as worker


def make_checkpoint(monkeypatch, tmp_path, *, gate=None, attempts=2):
    """Host bash replaces only Apptainer execution; restore uses the public API."""

    def execute(env, command, *, timeout=60):
        repo = env.root / "fs" / env.repo_path.lstrip("/")
        return LocalEnv(str(repo), base_commit=getattr(env, "_base_commit", "HEAD")).execute(command, timeout=timeout)

    monkeypatch.setattr(ApptainerEnv, "execute", execute)
    env = ApptainerEnv.__new__(ApptainerEnv)
    env.root = tmp_path / "source"
    env.repo_path = "/workspace/repo"
    env._lock, env._closed = threading.RLock(), False
    repo = env.root / "fs/workspace/repo"
    repo.mkdir(parents=True)
    local = LocalEnv.fresh(workdir=str(tmp_path))
    import shutil

    shutil.copytree(local.repo_path, repo, dirs_exist_ok=True)
    local.cleanup()
    env._base_commit = env.execute("git rev-parse HEAD").stdout.strip()
    env.image = tmp_path / "source.sif"
    env.image.write_bytes(b"fixture-image")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "branch").write_text("worker head")
    env._checkpoint_mounts = {"/cbshared": shared}
    env.write_file(".gitignore", "ignored\n")
    env.write_file("a.py", "broken(\n")
    env.write_file("worker.py", "feature = 1\n")
    env.execute("git add .gitignore worker.py")
    env.write_file("worker.py", "feature = 2\n")
    env.write_file("ignored", "cache")
    (repo / "binary").write_bytes(bytes(range(256)))
    env.execute("ln -s worker.py link; chmod +x worker.py")
    (env.root / "fs/bin").symlink_to("/usr/bin")
    spec = TeamSpec(
        run_id="original",
        repo="demo",
        task_id=1,
        features=[1, 2],
        assignments=[Assignment("agent1", "feature one", feature_id=1), Assignment("agent2", "feature two", feature_id=2)],
        worker="mini_swe",
        model="fixture",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        repair_integrator=True,
        repair_attempts=attempts,
        focused_repair=True,
        spec_fidelity=True,
        tdd_preamble=True,
        mine_conventions=True,
        completion_gate=gate,
    )
    from cooperagents.harness import repair_brief
    from cooperagents.repair import prefix_task
    from cooperagents.vendor.mini_swe.exceptions import LimitsExceeded

    task = repair_brief(env, spec, spec.assignments)
    checkpoint = tmp_path / "checkpoints/before-integrator1"
    checkpoint.parent.mkdir()
    run = dict(spec=asdict(spec), assignments=[asdict(a) for a in spec.assignments], step_limit=40, cost_limit=5.0, command_timeout=300)
    run["spec"]["completion_gate"] = "lambda" if gate else None
    (checkpoint.parent / "run.json").write_text(json.dumps(run))

    def capture(agent):
        inputs = capture_repair_input(
            agent, task=task, spec=spec, assignments=spec.assignments, command_timeout=300, guard_git=False, time_limit_s=None
        )
        save_checkpoint(env, checkpoint, metadata=dict(boundary="repair_agent_start", task=task, attempt=1), repair_input=inputs)

    def stop(agent):
        raise LimitsExceeded({"role": "exit", "content": "limit", "extra": {"exit_status": "LimitsExceeded"}})

    with monkeypatch.context() as startup:
        startup.setattr(worker.DefaultAgent, "step", stop)
        worker.run_mini_swe_agent(
            env,
            task=prefix_task(spec, task),
            agent_id="integrator1",
            role="integrator",
            model_name="fixture",
            step_limit=spec.repair_step_limit,
            cost_limit=5,
            command_timeout=300,
            comm=worker.BusComm(__import__("cooperagents.bus.memory", fromlist=["InMemoryBus"]).InMemoryBus("original"), "integrator1"),
            on_start=capture,
        )
    env.cleanup()
    return checkpoint


def completion(command, *, content=None):
    return ModelResponse(
        choices=[
            dict(
                index=0,
                finish_reason="tool_calls",
                message=dict(
                    role="assistant",
                    content=content,
                    tool_calls=[
                        dict(id="call", type="function", function=dict(name="bash", arguments=json.dumps(dict(command=command)))),
                    ],
                ),
            )
        ],
        usage=dict(prompt_tokens=100, completion_tokens=10, total_tokens=110),
    )


def test_two_restores_are_independent_and_preserve_full_state(monkeypatch, tmp_path):
    checkpoint = make_checkpoint(monkeypatch, tmp_path)
    manifest = (checkpoint / "manifest.json").read_bytes()
    left = ApptainerEnv.from_checkpoint(checkpoint, scratch=tmp_path / "scratch")
    right = ApptainerEnv.from_checkpoint(checkpoint, scratch=tmp_path / "scratch")
    try:
        assert left.root != right.root
        assert left._base_commit == right._base_commit
        for env in (left, right):
            assert env.read_file("worker.py") == "feature = 2\n"
            assert env.execute("git show :worker.py").stdout == "feature = 1\n"
            assert env.read_file("ignored") == "cache"
            repo = env.root / "fs/workspace/repo"
            assert (repo / "binary").read_bytes() == bytes(range(256))
            assert (repo / "link").is_symlink()
            assert (repo / "worker.py").stat().st_mode & 0o111
            assert os.readlink(env.root / "fs/bin") == "/usr/bin"
        left.write_file("worker.py", "changed")
        (left._checkpoint_mounts["/cbshared"] / "branch").write_text("changed")
        assert right.read_file("worker.py") == "feature = 2\n"
        assert (right._checkpoint_mounts["/cbshared"] / "branch").read_text() == "worker head"
        assert (checkpoint / "manifest.json").read_bytes() == manifest
        verify_checkpoint(checkpoint)
    finally:
        left.cleanup()
        right.cleanup()


def test_only_repair_and_second_attempt_uses_current_tree_and_saved_prompt(monkeypatch, tmp_path):
    checkpoint = make_checkpoint(monkeypatch, tmp_path)
    saved = load_repair_input(checkpoint)
    requests, roots = [], []
    original_run = worker.run_mini_swe_agent

    def run(env, **kwargs):
        assert kwargs["role"] == "integrator"
        roots.append(env.root)
        if kwargs["agent_id"] == "integrator2":
            assert env.read_file("marker.py") == "first = 1\n"
            assert "a.py:1" in kwargs["task"]
        return original_run(env, **kwargs)

    def query(model, messages, **kwargs):
        requests.append(messages)
        if len(requests) == 1:
            return completion(
                "printf 'new_broken(\\n' > a.py; printf 'first = 1\\n' > marker.py; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
            )
        return completion("printf 'fixed = 1\\n' > a.py; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")

    monkeypatch.setattr(worker, "run_mini_swe_agent", run)
    monkeypatch.setattr(worker.LitellmModel, "_query_inner", query)
    monkeypatch.setattr(UnifiedHarness, "run", lambda *a, **k: pytest.fail("full run must not execute"))
    monkeypatch.setenv("COOPER_TEMPERATURE_FORCE", "2")
    monkeypatch.setattr(worker, "_solo_config", lambda: {"agent": {"system_template": "changed", "instance_template": "changed"}})
    result = run_repair_checkpoint(checkpoint, scratch=tmp_path / "scratch", run_id="new")
    assert set(result.seeds) == {"integrator1", "integrator2"}
    assert roots[0] == roots[1] and not roots[0].exists()
    assert requests[0] == [m.model_dump() for m in saved.initial_messages]
    assert requests[1][0] == requests[0][0]
    assert "new_broken" not in result.integrated.patch
    assert all(name in result.integrated.patch for name in ("worker.py", "marker.py", "a.py", "GIT binary patch"))
    assert result.metrics["repair_attempts"][0]["healthy"] is False
    assert result.metrics["repair_attempts"][1]["healthy"] is True


def test_first_repair_runs_even_if_initial_tree_is_healthy(monkeypatch, tmp_path):
    checkpoint = make_checkpoint(monkeypatch, tmp_path, attempts=1)
    # Health is not queried before the first model request.
    from cooperagents import harness

    called = []
    monkeypatch.setattr(harness, "_tree_health", lambda env: (called.append("health"), True)[1])
    monkeypatch.setattr(
        worker.LitellmModel,
        "_query_inner",
        lambda *a, **k: (called.append("model"), completion("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"))[1],
    )
    run_repair_checkpoint(checkpoint, scratch=tmp_path / "scratch", run_id="new")
    assert called == ["model", "health"]


def test_standard_gate_rejects_then_accepts_and_nonzero_tool_is_observation(monkeypatch, tmp_path):
    checkpoint = make_checkpoint(monkeypatch, tmp_path, gate=partial(validate, merged=False, build_artifact=None), attempts=1)
    replies = iter(
        [
            completion("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
            completion("false"),
            completion("printf 'fixed = 1\\n' > a.py; touch pyproject.toml; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        ]
    )
    requests = []

    def query(model, messages, **kwargs):
        requests.append(messages.copy())
        return next(replies)

    monkeypatch.setattr(worker.LitellmModel, "_query_inner", query)
    result = run_repair_checkpoint(checkpoint, scratch=tmp_path / "scratch", run_id="new")
    assert len(requests) == 3
    assert "SUBMISSION REJECTED" in requests[1][-1]["content"]
    assert '"returncode": 1' in requests[2][-1]["content"]
    assert result.seeds["integrator1"].status == "submitted"
    assert load_repair_input(checkpoint).gate.max_rejections == 3
    with pytest.raises(ValueError, match="standard"):
        describe_gate(lambda env: None)


@pytest.mark.parametrize("failure", ["corrupt", "base", "cancel", "truncated"])
def test_restore_failure_cleans_owned_tree(monkeypatch, tmp_path, failure):
    checkpoint = make_checkpoint(monkeypatch, tmp_path)
    execute = ApptainerEnv.execute
    if failure == "corrupt":
        (checkpoint / "rootfs.tar.gz").write_bytes(b"corrupt")
    elif failure == "base":

        def fail(env, command, **kwargs):
            if "cat-file" in command:
                return ExecResult("missing", 1)
            return execute(env, command, **kwargs)

        monkeypatch.setattr(ApptainerEnv, "execute", fail)
    elif failure == "cancel":
        monkeypatch.setattr(ApptainerEnv, "execute", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    else:
        monkeypatch.setattr(ApptainerEnv, "execute", lambda *a, **k: ExecResult("[output truncated after 20 bytes]", 0))
    with pytest.raises((ValueError, RuntimeError, KeyboardInterrupt)):
        ApptainerEnv.from_checkpoint(checkpoint, scratch=tmp_path / "scratch")
    assert not list((tmp_path / "scratch").glob("ca-repair-*")) if (tmp_path / "scratch").exists() else True


@pytest.mark.parametrize("field,value", [("image", None), ("image_sha256", None)])
def test_malformed_image_metadata_allocates_no_sandbox(monkeypatch, tmp_path, field, value):
    from cooperagents.checkpoint import sha256

    checkpoint = make_checkpoint(monkeypatch, tmp_path)
    state = json.loads((checkpoint / "state.json").read_text())
    state["runtime"][field] = value
    (checkpoint / "state.json").write_text(json.dumps(state))
    manifest = json.loads((checkpoint / "manifest.json").read_text())
    manifest["files"]["state.json"] = {"bytes": (checkpoint / "state.json").stat().st_size, "sha256": sha256(checkpoint / "state.json")}
    (checkpoint / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="image provenance"):
        ApptainerEnv.from_checkpoint(checkpoint, scratch=tmp_path / "scratch")
    assert not (tmp_path / "scratch").exists()


@pytest.mark.parametrize("name,target", [("../escape", None), ("/escape", None), ("link/escape", "/tmp")])
def test_archives_cannot_write_outside_owned_tree(tmp_path, name, target):
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        if target:
            link = tarfile.TarInfo("link")
            link.type, link.linkname = tarfile.SYMTYPE, target
            archive.addfile(link)
        member = tarfile.TarInfo(name)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        extract_tree(path, tmp_path / "restore")


def test_invalid_inputs_fail_before_restore_and_credentials_not_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "private-credential")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://private-endpoint.invalid/v1")
    checkpoint = make_checkpoint(monkeypatch, tmp_path)
    inputs = load_repair_input(checkpoint)
    text = (checkpoint / "repair-input.json").read_text()
    assert "private-credential" not in text and "private-endpoint" not in text
    broken = inputs.model_dump()
    broken["model_config_data"]["model_kwargs"]["api_key"] = "secret"
    with pytest.raises(ValueError, match="Unsupported"):
        RepairInput.model_validate(broken)
    broken = inputs.model_dump()
    broken["version"] = 99
    with pytest.raises(ValueError):
        RepairInput.model_validate(broken)
    with pytest.raises(ValueError, match="unsaved host state"):
        UnifiedHarness(checkpoint_dir=tmp_path / "unused").run(
            TeamSpec(
                "bad", "demo", 1, [1, 2], worker="mini_swe", shared_workspace=True, seed_prior=False, coop_tools=True, task_board=True
            ),
            env_factory=lambda aid: pytest.fail("must fail before environment"),
        )


def make_legacy_evidence(monkeypatch, tmp_path):
    checkpoint = make_checkpoint(monkeypatch, tmp_path, gate=partial(validate, merged=False))
    inputs = load_repair_input(checkpoint)
    manifest = json.loads((checkpoint / "manifest.json").read_text())
    del manifest["files"]["repair-input.json"]
    (checkpoint / "repair-input.json").unlink()
    (checkpoint / "manifest.json").write_text(json.dumps(manifest))
    source = tmp_path / "source-code"
    package = Path(worker.__file__).parents[1]
    files = {
        name: (package / name).read_bytes()
        for name in (
            "harness.py",
            "workers/mini_swe_worker.py",
            "vendor/mini_swe/config/solo.yaml",
            "vendor/mini_swe/agents/default.py",
            "vendor/mini_swe/models/litellm_model.py",
            "vendor/mini_swe/models/utils/actions_toolcall.py",
        )
    }
    for name, content in files.items():
        path = source / "src/cooperagents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for name in ("bench_compare.py", "nlp_cluster/job.sh", "nlp_cluster/submit.py"):
        path = source / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pinned-source-fixture")

    def git_read(argv, **kwargs):
        if argv[-1] == "HEAD":
            return "514ed98a59c611ee5a38027c8459d6fcbcec92b8\n"
        return (source / argv[-1].split(":", 1)[1]).read_bytes()

    monkeypatch.setattr("subprocess.check_output", git_read)
    variant = tmp_path / "variant.toml"
    variant.write_text("completion_gate = true\npresub_merge = false\nrepair = true\nrepair_attempts = 2\n")
    args = tmp_path / "training-args.txt"
    args.write_text(
        "--completion-gate\n--repair-integrator\n--repair-attempts\n2\n--step-limit\n40\n"
        "--no-seed\n--coop-tools\n--spec-fidelity\n--tdd-preamble\n--mine-conventions\n--focused-repair\n"
    )
    state = verify_checkpoint(checkpoint)
    request = dict(
        model=inputs.model_config_data["model_name"],
        messages=[m.model_dump() for m in inputs.initial_messages],
        tools=inputs.tools,
        **inputs.model_config_data["model_kwargs"],
    )
    records = [
        (
            "checkpoint_end",
            dict(
                path="/old/before-integrator1", boundary="repair_agent_start", files=manifest["files"], patch_source=state["patch_source"]
            ),
        ),
        ("agent_start", dict(role="integrator", task=inputs.effective_task, step_limit=25, time_limit_s=None, model="fixture")),
        ("context", dict(reason="start", messages=[])),
        ("messages", dict(messages=request["messages"])),
        ("request", dict(request=request)),
        ("response", dict(call_id=5, response={})),
    ]
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        "".join(
            json.dumps(dict(version=1, seq=i, time="2026-09-29T00:00:00+00:00", actor="integrator1", event=event, data=data)) + "\n"
            for i, (event, data) in enumerate(records, 1)
        )
    )
    return checkpoint, inputs, journal, source, variant, args


def test_legacy_import_verifies_evidence_and_does_not_rewrite_original(monkeypatch, tmp_path):
    checkpoint, inputs, journal, source, variant, args = make_legacy_evidence(monkeypatch, tmp_path)
    before = {p.name: sha256(p) for p in checkpoint.iterdir() if p.is_file()}
    destination = tmp_path / "new-run/data/repair-input.json"
    imported = import_legacy_repair_input(
        checkpoint, journal=journal, source=source, variant=variant, launch_args=args, destination=destination
    )
    assert imported.initial_messages == inputs.initial_messages
    assert imported.agent_config == inputs.agent_config
    assert imported.tools == inputs.tools
    assert imported.gate.max_rejections == 3
    assert load_repair_input(checkpoint, destination) == imported
    assert {p.name: sha256(p) for p in checkpoint.iterdir() if p.is_file()} == before
    with pytest.raises(FileExistsError):
        import_legacy_repair_input(checkpoint, journal=journal, source=source, variant=variant, launch_args=args, destination=destination)


@pytest.mark.parametrize("damage", ["receipt", "task", "messages", "tools", "sequence", "gate", "args"])
def test_legacy_missing_or_mismatched_evidence_fails_before_inference(monkeypatch, tmp_path, damage):
    checkpoint, _, journal, source, variant, args = make_legacy_evidence(monkeypatch, tmp_path)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    if damage == "receipt":
        rows[0]["data"]["files"]["raw.patch"]["sha256"] = "0" * 64
    elif damage == "task":
        rows[1]["data"]["task"] = "another task"
    elif damage == "messages":
        rows[4]["data"]["request"]["messages"][1]["content"] = "changed"
    elif damage == "tools":
        rows[4]["data"]["request"]["tools"] = []
    elif damage == "sequence":
        rows[2]["seq"] = 99
    elif damage == "gate":
        variant.write_text("completion_gate = true\npresub_merge = true\n")
    else:
        args.write_text(args.read_text() + "--repair-steps\n99\n")
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    destination = tmp_path / "new-run/repair-input.json"
    with pytest.raises(ValueError):
        import_legacy_repair_input(checkpoint, journal=journal, source=source, variant=variant, launch_args=args, destination=destination)
    assert not destination.exists()
    with pytest.raises(ValueError, match="Legacy checkpoint"):
        load_repair_input(checkpoint)


@pytest.mark.parametrize("failure", ["truncated", "failed", "cancel"])
def test_patch_export_or_cancellation_invalidates_episode_and_cleans(monkeypatch, tmp_path, failure):
    checkpoint = make_checkpoint(monkeypatch, tmp_path, attempts=1)
    roots = []
    real_restore = ApptainerEnv.from_checkpoint

    def restore(path, *, scratch):
        env = real_restore(path, scratch=scratch)
        roots.append(env.root)
        if failure == "truncated":
            monkeypatch.setattr(env, "git_diff", lambda: "diff --git a/a.py b/a.py\n[output truncated after 100 bytes]")
        elif failure == "failed":
            execute = env.execute
            monkeypatch.setattr(
                env, "execute", lambda command, **kw: ExecResult("stage failed", 1) if command == "git add -A" else execute(command, **kw)
            )
        return env

    monkeypatch.setattr(ApptainerEnv, "from_checkpoint", restore)
    if failure == "cancel":
        monkeypatch.setattr(worker.LitellmModel, "_query_inner", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    else:
        monkeypatch.setattr(worker.LitellmModel, "_query_inner", lambda *a, **k: completion("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"))
    with pytest.raises((RuntimeError, KeyboardInterrupt)):
        run_repair_checkpoint(checkpoint, scratch=tmp_path / "scratch", run_id="new")
    assert roots and not roots[0].exists()


def test_capture_failure_propagates_without_model_call(monkeypatch, tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        monkeypatch.setattr(worker.LitellmModel, "_query_inner", lambda *a, **k: pytest.fail("capture failure must stop inference"))

        def fail(agent):
            raise OSError("capture failed")

        with pytest.raises(OSError, match="capture failed"):
            worker.run_mini_swe_agent(
                env,
                task="repair",
                agent_id="integrator1",
                role="integrator",
                model_name="fixture",
                step_limit=1,
                cost_limit=5,
                on_start=fail,
            )
    finally:
        env.cleanup()


def test_rootfs_readonly_directory_permissions_are_applied_after_file_extraction(tmp_path):
    source = tmp_path / "source"
    (source / "readonly").mkdir(parents=True)
    (source / "readonly/cache").write_text("preserved")
    (source / "readonly").chmod(0o555)
    path = tmp_path / "tree.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        archive.add(source, arcname=".")
    destination = tmp_path / "restored"
    try:
        extract_tree(path, destination)
        assert (destination / "readonly/cache").read_text() == "preserved"
        assert (destination / "readonly").stat().st_mode & 0o777 == 0o555
    finally:
        (source / "readonly").chmod(0o755)
        if (destination / "readonly").exists():
            (destination / "readonly").chmod(0o755)


def test_cleanup_removes_readonly_rootfs_without_touching_external_symlink(monkeypatch, tmp_path):
    checkpoint = make_checkpoint(monkeypatch, tmp_path)
    env = ApptainerEnv.from_checkpoint(checkpoint, scratch=tmp_path / "scratch")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o555)
    readonly = env.root / "fs/readonly"
    readonly.mkdir()
    (readonly / "cache").write_text("owned")
    (readonly / "external").symlink_to(outside, target_is_directory=True)
    readonly.chmod(0)
    try:
        env.cleanup()
        assert not env.root.exists()
        assert outside.stat().st_mode & 0o777 == 0o555
        env.cleanup()  # Idempotent.
    finally:
        outside.chmod(0o755)
