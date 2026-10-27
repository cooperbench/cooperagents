import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


@pytest.mark.parametrize("implementation_enabled", [True, False])
@pytest.mark.parametrize("interval", [None, 0, 7])
@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
@pytest.mark.parametrize("mode", ["direct", "group-chat", "group-chat-planning"])
def test_qualified_image_pins_resources_and_node(monkeypatch, tmp_path, variant, mode, interval, implementation_enabled):
    spec = importlib.util.spec_from_file_location("nlp_submit", Path(__file__).parents[1] / "scripts/nlp_cluster/submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            dict(
                passed=True,
                task=dict(repo="go_chi_task", task_id=26, pairs=[[1, 2]]),
                image="task-image",
                image_path="/scr/user/task.sif",
                node="john11.stanford.edu",
                sha256="abc",
            )
        )
    )
    monkeypatch.setattr(
        module.sys if hasattr(module, "sys") else __import__("sys"),
        "argv",
        [
            "submit.py",
            "--mode",
            "real",
            "--env-file",
            "/private/profile",
            "--cpus",
            "2",
            "--memory",
            "8G",
            "--qualification-report",
            str(report),
            "--pairs",
            "go_chi_task:26:1,2",
            "--langfuse",
            "--coordination-variant",
            variant,
            "--coordination-mode",
            mode,
            "--planning-step-limit",
            "200",
            "--planning-time-limit",
            "600",
        ] + ([] if interval is None else ["--worker-reminder-interval", str(interval)])
        + ([] if implementation_enabled else ["--no-implementation-coordinator"]),
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    calls = []
    uploaded_archive = None

    def output(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return "abcdef012345\n"
        if argv[:2] == ["git", "status"]:
            return b""
        if argv[:2] == ["git", "archive"]:
            return archive.getvalue()
        return "12345\n"

    monkeypatch.setattr(module.subprocess, "check_output", output)

    def run(argv, **kwargs):
        nonlocal uploaded_archive
        calls.append(argv)
        if argv[0] == "ssh" and "tar -xf -" in argv[-1]:
            uploaded_archive = kwargs["input"]

    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    submission = next(c[-1] for c in calls if c[0] == "ssh" and "sbatch --parsable" in c[-1])
    assert "--nodelist=john11" in submission
    assert "--cpus-per-task=2" in submission and "--mem=8G" in submission
    assert "--time=02:00:00" in submission
    assert "COOPER_LANGFUSE=1" in submission
    assert f"COOPER_COORDINATOR_IMPLEMENTATION_ENABLED={int(implementation_enabled)}" in submission
    assert f"COOPER_COORDINATION_VARIANT={variant}" in submission
    assert f"COOPER_WORKER_REMINDER_INTERVAL={10 if interval is None else interval}" in submission
    with tarfile.open(fileobj=io.BytesIO(uploaded_archive)) as snapshot:
        metadata = json.loads(snapshot.extractfile("_run/metadata.json").read())
        assert metadata["coordination_variant"] == variant
        assert metadata["coordination_mode"] == mode
        assert metadata["coordinator_implementation_enabled"] is implementation_enabled
        assert metadata["planning_step_limit"] == 200 and metadata["planning_time_limit"] == 600
        assert metadata["worker_reminder_interval"] == (10 if interval is None else interval)
        variant_data = snapshot.extractfile("_run/variant.toml").read()
        assert f"coordinator_implementation_enabled = {str(implementation_enabled).lower()}".encode() in variant_data
        assert f"worker_reminder_interval = {10 if interval is None else interval}".encode() in variant_data
        assert f"{variant}-{mode}" in metadata["run_id"]
        assert f"coordination_variant = '{variant}'".encode() in snapshot.extractfile("_run/variant.toml").read()
    report.write_text(json.dumps(dict(passed=False, task=dict(repo="go_chi_task", task_id=26, pairs=[[1, 2]]))))
    with pytest.raises(SystemExit):
        module.main()


@pytest.mark.parametrize("stop_after_capture", [False, True])
def test_multi_task_qualification_requires_one_passing_node(monkeypatch, tmp_path, stop_after_capture):
    spec = importlib.util.spec_from_file_location("nlp_submit", Path(__file__).parents[1] / "scripts/nlp_cluster/submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reports = tmp_path / "reports"
    reports.mkdir()
    for task_id in (26, 27):
        (reports / f"{task_id}.json").write_text(
            json.dumps(
                dict(
                    passed=True,
                    task=dict(repo="go_chi_task", task_id=task_id, pairs=[[1, 2]]),
                    image=f"image-{task_id}",
                    image_path=f"/scr/user/{task_id}.sif",
                    node="visionlab-dgx1.stanford.edu",
                    sha256=f"hash-{task_id}",
                )
            )
        )
    monkeypatch.setattr(
        __import__("sys"),
        "argv",
        [
            "submit.py",
            "--mode",
            "real",
            "--env-file",
            "/private/profile",
            "--qualification-dir",
            str(reports),
            "--partition",
            "sc-freegpu",
            "--cpus",
            "20",
            "--memory",
            "128G",
            "--concurrency",
            "10",
            "--wall-time",
            "08:00:00",
            "--round",
            "1",
            "--no-coordinator",
            "--no-coordinator-notebook",
            "--step-limit",
            "30",
            "--agent-time-limit",
            "300",
            "--stop-after-repair-checkpoint" if stop_after_capture else "--collect-trajectories",
            "--checkpoint-repair",
            "--repair-integrator",
            "--repair-attempts",
            "2",
            "--pairs",
            "go_chi_task:26:1,2",
            "go_chi_task:27:1,2",
        ],
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    calls = []
    uploaded_archive = None

    def output(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return "abcdef012345\n"
        if argv[:2] == ["git", "status"]:
            return b""
        if argv[:2] == ["git", "archive"]:
            return archive.getvalue()
        return "12345\n"

    monkeypatch.setattr(module.subprocess, "check_output", output)

    def run(argv, **kwargs):
        nonlocal uploaded_archive
        calls.append(argv)
        if argv[0] == "ssh" and "tar -xf -" in argv[-1]:
            uploaded_archive = kwargs["input"]

    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    submission = next(c[-1] for c in calls if c[0] == "ssh" and "sbatch --parsable" in c[-1])
    assert "--nodelist=visionlab-dgx1" in submission
    assert "--cpus-per-task=20" in submission and "--mem=128G" in submission
    assert "--time=08:00:00" in submission and "--no-requeue" in submission
    assert "COOPER_CONCURRENCY=10" in submission
    assert "COOPER_COORDINATOR=0" in submission
    assert "COOPER_COORDINATOR_NOTEBOOK=0" in submission
    assert "COOPER_COORDINATION_VARIANT=auto-prompt" in submission
    assert "COOPER_STEP_LIMIT=30" in submission and "COOPER_AGENT_TIME_LIMIT=300" in submission
    assert "COOPER_COLLECT_TRAJECTORIES=1" in submission
    assert "COOPER_CHECKPOINT_REPAIR=1" in submission and "COOPER_RECORD_TRAJECTORIES=1" in submission
    assert f"COOPER_STOP_AFTER_REPAIR_CHECKPOINT={int(stop_after_capture)}" in submission
    assert "COOPER_REPAIR=1" in submission
    assert "COOPER_REPAIR_ATTEMPTS=2" in submission
    with tarfile.open(fileobj=io.BytesIO(uploaded_archive)) as snapshot:
        variant = snapshot.extractfile("_run/variant.toml").read()
        assert b"coordinator = false" in variant and b"coordinator_notebook = false" in variant
        metadata = json.loads(snapshot.extractfile("_run/metadata.json").read())
        assert metadata["step_limit"] == 30 and metadata["agent_time_limit"] == 300
        assert metadata["checkpoint_repair"] and metadata["record_trajectories"]
        assert metadata["stop_after_repair_checkpoint"] is stop_after_capture
        assert not metadata["official_evaluation"]
        assert f"stop_after_repair_checkpoint = {str(stop_after_capture).lower()}".encode() in variant
        checkpoint = json.loads(snapshot.extractfile("_run/checkpoint.json").read())
        assert checkpoint["format"] == "checkpoint-tar-delta-v1"
        assert not metadata["coordinator_notebook"]
        assert b"repair = true\nrepair_attempts = 2" in variant
    (reports / "27.json").write_text(
        json.dumps(
            dict(
                passed=True,
                task=dict(repo="go_chi_task", task_id=27, pairs=[[1, 2]]),
                image="image-27",
                image_path="/scr/user/27.sif",
                node="other-node",
                sha256="hash-27",
            )
        )
    )
    with pytest.raises(SystemExit):
        module.main()


@pytest.mark.parametrize("script", ["scripts/bench_compare.py", "scripts/nlp_cluster/submit.py"])
def test_coordination_cli_names(script):
    path = Path(__file__).parents[1] / script
    help_result = subprocess.run([sys.executable, str(path), "--help"], capture_output=True, text=True, check=True)
    assert "{auto-prompt,human-prompt}" in help_result.stdout
    for old_name in ("current", "human_in_loop"):
        result = subprocess.run(
            [sys.executable, str(path), "--coordination-variant", old_name],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "invalid choice" in result.stderr
        assert "auto-prompt" in result.stderr and "human-prompt" in result.stderr


def test_existing_snapshot_aborts_before_copy_or_submission(monkeypatch, tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("nlp_submit_collision", Path(__file__).parents[1] / "scripts/nlp_cluster/submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda _: datetime(2026, 10, 4, tzinfo=UTC)))
    monkeypatch.setattr(sys, "argv", ["submit.py", "--root", str(tmp_path)])
    run_id = "20261004T000000Z-cooperbench-auto-prompt-direct-abcdef01"
    code = tmp_path / "code" / f"cooperagents-{run_id}"
    run = tmp_path / "runs" / run_id
    code.mkdir(parents=True)
    run.mkdir(parents=True)
    sentinel = run / "metadata.json"
    sentinel.write_text("original record")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass

    def output(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return "abcdef012345\n"
        if argv[:2] == ["git", "status"]:
            return b""
        if argv[:2] == ["git", "archive"]:
            return archive.getvalue()
        raise AssertionError("No submission after collision")

    execute = subprocess.run
    calls = []

    def local_ssh(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "ssh" and "tar -xf -" in argv[-1]
        assert "; cp " not in argv[-1]
        return execute(["bash", "-c", argv[-1]], **kwargs)

    monkeypatch.setattr(module.subprocess, "check_output", output)
    monkeypatch.setattr(module.subprocess, "run", local_ssh)
    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    assert sentinel.read_text() == "original record" and len(calls) == 1


@pytest.mark.parametrize("missing", ["--checkpoint-repair", "--repair-integrator"])
def test_stop_option_requires_capture_and_repair(monkeypatch, missing):
    spec = importlib.util.spec_from_file_location("submit_stop", Path(__file__).parents[1] / "scripts/nlp_cluster/submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [
        "submit.py", "--mode", "real", "--stop-after-repair-checkpoint",
        *[flag for flag in ["--checkpoint-repair", "--repair-integrator"] if flag != missing],
    ])
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **k: pytest.fail("invalid options reached submission"))
    with pytest.raises(SystemExit):
        module.main()
