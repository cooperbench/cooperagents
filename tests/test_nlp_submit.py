import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest


def test_qualified_image_pins_resources_and_node(monkeypatch, tmp_path):
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
        ],
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    calls = []

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
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    module.main()
    submission = next(c[-1] for c in calls if c[0] == "ssh" and "sbatch --parsable" in c[-1])
    assert "--nodelist=john11" in submission
    assert "--cpus-per-task=2" in submission and "--mem=8G" in submission
    assert "--time=02:00:00" in submission
    report.write_text(json.dumps(dict(passed=False, task=dict(repo="go_chi_task", task_id=26, pairs=[[1, 2]]))))
    with pytest.raises(SystemExit):
        module.main()


def test_multi_task_qualification_requires_one_passing_node(monkeypatch, tmp_path):
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
            "--collect-trajectories",
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
    assert "COOPER_STEP_LIMIT=30" in submission and "COOPER_AGENT_TIME_LIMIT=300" in submission
    assert "COOPER_COLLECT_TRAJECTORIES=1" in submission
    assert "COOPER_REPAIR=1" in submission
    assert "COOPER_REPAIR_ATTEMPTS=2" in submission
    with tarfile.open(fileobj=io.BytesIO(uploaded_archive)) as snapshot:
        variant = snapshot.extractfile("_run/variant.toml").read()
        assert b"coordinator = false" in variant and b"coordinator_notebook = false" in variant
        metadata = json.loads(snapshot.extractfile("_run/metadata.json").read())
        assert metadata["step_limit"] == 30 and metadata["agent_time_limit"] == 300
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
