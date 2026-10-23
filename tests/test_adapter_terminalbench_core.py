"""Terminal-Bench-core adapter: offline wiring (format parsing, filter, scoring)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cooperagents.adapters import get_adapter
from cooperagents.adapters import terminalbench_core as tbc
from cooperagents.adapters.terminalbench_core import TerminalBenchCoreAdapter


def _make_task(root: Path, name: str, *, services: list[str], instruction: str = "do the thing") -> None:
    d = root / name
    (d / "tests").mkdir(parents=True)
    (d / "task.yaml").write_text(
        f"instruction: |-\n  {instruction}\nmax_agent_timeout_sec: 120.0\nmax_test_timeout_sec: 30.0\n"
    )
    svc = "\n".join(f"  {s}:\n    build: .\n" for s in services)
    (d / "docker-compose.yaml").write_text("services:\n" + svc)
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "run-tests.sh").write_text("bash $TEST_DIR/run-uv-pytest.sh\n")
    (d / "tests" / "test_outputs.py").write_text("def test_ok():\n    assert True\n")


def test_registry_resolves_terminalbench_core():
    assert isinstance(get_adapter("terminalbench-core"), TerminalBenchCoreAdapter)


def test_declarative_facts_are_none():
    a = TerminalBenchCoreAdapter()
    assert a.build_artifact is None and a.reference_binary is None


def test_instances_filters_single_container(tmp_path, monkeypatch):
    monkeypatch.setattr(tbc, "TBC_ROOT", tmp_path)
    _make_task(tmp_path, "single", services=["client"])
    _make_task(tmp_path, "multi", services=["client", "db"])
    insts = TerminalBenchCoreAdapter().instances()
    assert insts == ["single"]  # multi-container excluded


def test_task_for_includes_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(tbc, "TBC_ROOT", tmp_path)
    _make_task(tmp_path, "t1", services=["client"], instruction="Create hello.txt")
    task = TerminalBenchCoreAdapter().task_for("t1")
    assert "Create hello.txt" in task
    assert "/app" in task


def test_agent_timeout_from_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(tbc, "TBC_ROOT", tmp_path)
    _make_task(tmp_path, "t1", services=["client"])
    assert TerminalBenchCoreAdapter().agent_timeout_s("t1") == 120


@pytest.mark.parametrize(
    "output,expected",
    [
        ("=== 2 passed in 0.5s ===", 1.0),
        ("=== 1 failed, 1 passed in 0.5s ===", 0.0),
        ("=== 1 passed, 1 error in 0.5s ===", 0.0),
        ("no tests ran in 0.01s", 0.0),
        ("", 0.0),
    ],
)
def test_reward_from_pytest(output, expected):
    assert TerminalBenchCoreAdapter()._reward_from_pytest(output) == expected


def test_submit_writes_integrated_patch(tmp_path):
    out = tmp_path / "out"
    TerminalBenchCoreAdapter().submit("hello-world", "diff --git a/x b/x\n", out)
    assert (out / "integrated.patch").read_text() == "diff --git a/x b/x\n"
