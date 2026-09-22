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
