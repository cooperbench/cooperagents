"""SWE-bench Verified adapter: offline wiring (image template, task, submission)."""

from __future__ import annotations

import json

from cooperagents.adapters import get_adapter
from cooperagents.adapters.swebench import SWEBenchAdapter, SWEBenchInstance

_INST = SWEBenchInstance(
    instance_id="astropy__astropy-13236",
    repo="astropy/astropy",
    base_commit="abc123",
    problem_statement="Deprecate automatic ndarray-to-Column conversion.",
)


def test_registry_resolves_swebench():
    assert isinstance(get_adapter("swebench"), SWEBenchAdapter)


def test_declarative_facts_are_none():
    a = SWEBenchAdapter()
    assert a.build_artifact is None and a.reference_binary is None


def test_image_template_default(monkeypatch):
    # Hermetic: ignore any SWEBENCH_IMAGE_TEMPLATE leaked into the environment
    # (e.g. from .env) so this asserts the hardcoded default. Published Docker Hub
    # tags rewrite ``__`` to ``_1776_`` (the ``{key}`` form).
    monkeypatch.delenv("SWEBENCH_IMAGE_TEMPLATE", raising=False)
    assert SWEBenchAdapter().image(_INST) == "swebench/sweb.eval.x86_64.astropy_1776_astropy-13236:latest"


def test_image_template_arm64_key_override(monkeypatch):
    monkeypatch.setenv("SWEBENCH_IMAGE_TEMPLATE", "swebench/sweb.eval.arm64.{key}:latest")
    assert SWEBenchAdapter().image(_INST) == "swebench/sweb.eval.arm64.astropy_1776_astropy-13236:latest"


def test_image_template_env_override(monkeypatch):
    monkeypatch.setenv("SWEBENCH_IMAGE_TEMPLATE", "ghcr.io/epoch-research/swe-bench.eval.x86_64.{id}:latest")
    assert SWEBenchAdapter().image(_INST) == "ghcr.io/epoch-research/swe-bench.eval.x86_64.astropy__astropy-13236:latest"


def test_task_for_includes_issue_and_workspace():
    task = SWEBenchAdapter().task_for(_INST)
    assert "Deprecate automatic ndarray" in task
    assert "/testbed" in task


def test_task_for_bare_id_recovers_issue_text(tmp_path, monkeypatch):
    # bench_programbench.py passes a bare id string; task_for must resolve the
    # full instance from SWEBENCH_DATA so the issue text reaches the agent
    # (regression: the ## Issue section used to degrade to the bare id).
    p = tmp_path / "verified.jsonl"
    p.write_text(
        json.dumps(
            {
                "instance_id": "pylint-dev__pylint-4551",
                "repo": "pylint-dev/pylint",
                "base_commit": "deadbeef",
                "problem_statement": "Use Python type hints for UML generation in pyreverse.",
            }
        )
        + "\n"
    )
    monkeypatch.setenv("SWEBENCH_DATA", str(p))
    adapter = SWEBenchAdapter()
    adapter.__class__._index = None  # reset the lazy cache for the test
    task = adapter.task_for("pylint-dev__pylint-4551")
    assert "type hints for UML generation" in task
    assert "pyreverse" in task
    adapter.__class__._index = None


def test_env_kwargs_points_at_testbed():
    assert SWEBenchAdapter().env_kwargs()["repo_path"] == "/testbed"


def test_submit_writes_predictions_jsonl(tmp_path):
    SWEBenchAdapter().submit(_INST, "diff --git a/x b/x\n+patch\n", tmp_path)
    rec = json.loads((tmp_path / "predictions.jsonl").read_text().strip())
    assert rec["instance_id"] == "astropy__astropy-13236"
    assert rec["model_patch"] == "diff --git a/x b/x\n+patch\n"
    assert "model_name_or_path" in rec


def test_instances_from_local_jsonl(tmp_path, monkeypatch):
    p = tmp_path / "verified.jsonl"
    p.write_text(
        json.dumps(
            {
                "instance_id": "django__django-11razor",
                "repo": "django/django",
                "base_commit": "deadbeef",
                "problem_statement": "Fix the thing.",
            }
        )
        + "\n"
    )
    monkeypatch.setenv("SWEBENCH_DATA", str(p))
    insts = SWEBenchAdapter().instances()
    assert len(insts) == 1
    assert insts[0].instance_id == "django__django-11razor"
    assert insts[0].base_commit == "deadbeef"
