import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_budget_reaches_solo_and_team(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("bench_compare", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    captured = []

    def run(self, team, **kwargs):
        captured.append((self.step_limit, team.agent_time_limit))
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={})

    monkeypatch.setattr(bench.UnifiedHarness, "run", run)
    monkeypatch.setattr(bench, "read_feature", lambda *a: "feature")
    monkeypatch.setattr(bench, "write_run_outputs", lambda *a, **k: None)
    item = bench.WorkItem("go_chi_task", 26, [1, 2])
    kwargs = dict(run_name="budget", logs_dir=tmp_path, step_limit=1000, agent_time_limit=3600)
    bench.run_solo(item, **kwargs)
    bench.run_team(item, max_agents=2, **kwargs)
    assert captured == [(1000, 3600), (1000, 3600)]


def test_runners_isolate_notebooks_and_mount_workers_only(monkeypatch, tmp_path):
    from cooperagents.harness import _Coordinator

    paths, mounts = [], []

    def run(self, team, *, env_factory, **kwargs):
        path = self.coordinator_notebook_path
        paths.append(path)
        coordinator = _Coordinator({}, assignments=team.assignments, bus=self.bus, notebook_path=path, complete=lambda _: [])
        for worker in [a.agent_id for a in team.assignments] + ["merge"]:
            env_factory(worker)
        assert bool(path) == team.coordinator_notebook
        if path is not None:
            assert path.is_file()
            assert len(coordinator._queues) == len(team.assignments)
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={}, integrated=SimpleNamespace(patch=""))

    for script in ("bench_compare", "bench_programbench"):
        module_spec = importlib.util.spec_from_file_location(script, Path(__file__).parents[1] / f"scripts/{script}.py")
        bench = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(bench)
        monkeypatch.setattr(bench.UnifiedHarness, "run", run)
        if script == "bench_compare":
            monkeypatch.setattr(bench, "read_feature", lambda *a: "feature")
            monkeypatch.setattr(bench, "write_run_outputs", lambda *a, **k: None)
            monkeypatch.setattr(bench, "task_environment", lambda *a, **kw: mounts.append(kw["coordinator_dir"]))

            def invoke(enabled, bench=bench):
                return bench.run_team(
                    bench.WorkItem("go_chi_task", 26, [1, 2]),
                    run_name="mount",
                    logs_dir=tmp_path,
                    step_limit=10,
                    max_agents=2,
                    coordinator=True,
                    coordinator_notebook=enabled,
                    coop_tools=True,
                    seed_prior=False,
                )

            size = 2
        else:
            monkeypatch.setattr(bench, "DockerEnv", lambda *a, **kw: mounts.append(kw))
            monkeypatch.setattr(
                bench,
                "ADAPTER",
                SimpleNamespace(
                    name="test",
                    build_artifact="artifact",
                    reference_binary="reference",
                    image=lambda _: "image",
                    task_for=lambda *a: "feature",
                    setup_env=lambda *a: None,
                    env_kwargs=lambda: {"user": "1000", "network": "none"},
                ),
            )

            def invoke(enabled, bench=bench):
                return bench.run_team_once(
                    "coopgitc2",
                    "test",
                    step_limit=10,
                    agent_time_limit=30,
                    team_size=3,
                    coordinator_notebook=enabled,
                    artifact_dir=tmp_path,
                )

            before = set(tmp_path.rglob("*"))
            dry, _ = bench.run_team_once(
                "coopgitc2",
                "test",
                step_limit=10,
                agent_time_limit=30,
                team_size=3,
                coordinator_notebook=False,
                artifact_dir=tmp_path,
                dry_run=True,
            )
            assert dry["agents"] == ["agent1", "agent2", "agent3"] and not dry["spec"]["coordinator_notebook"]
            assert set(tmp_path.rglob("*")) == before
            size = 3
        for enabled in (True, True, False):
            mounts.clear()
            invoke(enabled)
            path = paths[-1]
            if script == "bench_compare":
                assert mounts == [path.parent if path else None] * size + [None]
            else:
                for index, options in enumerate(mounts):
                    assert options["user"] == "1000" and options["network"] == "none"
                    notes = [v for v in options["volumes"] if v.endswith(":/coordination:ro")]
                    assert notes == ([f"{path.parent}:/coordination:ro"] if path and index < size else [])
        assert paths[-3] != paths[-2] and paths[-1] is None


@pytest.mark.parametrize("only_first", [False, True])
@pytest.mark.parametrize("collect_conflicts", [False, True])
def test_benchmark_checkpoint_options_reach_valid_harness(monkeypatch, tmp_path, only_first, collect_conflicts):
    from cooperagents.repair import validate_capture_spec

    module_spec = importlib.util.spec_from_file_location("bench_capture", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bench)

    def run(self, team, **kwargs):
        validate_capture_spec(team, self.bus)
        assert self.stop_after_repair_checkpoint
        assert self.only_first_repair_checkpoint is only_first
        assert self.collect_source_conflicts is collect_conflicts
        assert team.repair_integrator
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={}, seeds={})

    monkeypatch.setattr(bench.UnifiedHarness, "run", run)
    monkeypatch.setattr(bench, "read_feature", lambda *a: "feature")
    monkeypatch.setattr(bench, "write_run_outputs", lambda *a, **k: None)
    bench.run_team(
        bench.WorkItem("go_chi_task", 26, [1, 2]), run_name="capture", logs_dir=tmp_path,
        step_limit=10, max_agents=2, seed_prior=False, coop_tools=True, repair_integrator=True,
        completion_gate=True, checkpoint_repair=True, stop_after_repair_checkpoint=True,
        only_first_repair_checkpoint=only_first,
        collect_source_conflicts=collect_conflicts,
    )


@pytest.mark.parametrize("only_first", [False, True])
@pytest.mark.parametrize("collect_conflicts", [False, True])
def test_stop_cli_skips_official_evaluation(monkeypatch, tmp_path, only_first, collect_conflicts):
    import sys

    module_spec = importlib.util.spec_from_file_location("bench_stop", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bench)
    calls = []
    monkeypatch.setattr(bench, "_load_env", lambda: None)
    monkeypatch.setattr(bench, "run_eval", lambda *a, **k: pytest.fail("capture mode must not score"))
    monkeypatch.setattr(bench, "run_team", lambda *a, **k: calls.append(k) or {"duration": 0, "steps": 0})
    monkeypatch.setattr(sys, "argv", [
        "bench_compare.py", "--pairs", "go_chi_task:26:1,2", "--team-only", "--checkpoint-repair",
        "--repair-integrator", "--stop-after-repair-checkpoint", "--log-dir", str(tmp_path),
        *(["--only-first-repair-checkpoint"] if only_first else []),
        *(["--collect-source-conflicts"] if collect_conflicts else []),
    ])
    bench.main()
    assert len(calls) == 1 and calls[0]["stop_after_repair_checkpoint"]
    assert calls[0]["only_first_repair_checkpoint"] is only_first
    assert calls[0]["collect_source_conflicts"] is collect_conflicts


def test_standard_completion_gate_partial_preserves_defaults():
    import inspect

    from cooperagents.verification import validate

    defaults = inspect.signature(validate).parameters
    assert defaults["merged"].default is False and defaults["build_artifact"].default is None


@pytest.mark.parametrize("missing", ["--checkpoint-repair", "--repair-integrator", "--team-only"])
def test_stop_cli_requires_capture_repair_and_team(monkeypatch, missing):
    import sys

    module_spec = importlib.util.spec_from_file_location("bench_invalid_stop", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bench)
    monkeypatch.setattr(bench, "_load_env", lambda: None)
    monkeypatch.setattr(bench, "run_team", lambda *a, **k: pytest.fail("invalid options reached task execution"))
    flags = ["--checkpoint-repair", "--repair-integrator", "--team-only"]
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", "--stop-after-repair-checkpoint", *[f for f in flags if f != missing]])
    with pytest.raises(SystemExit):
        bench.main()


@pytest.mark.parametrize("script", ["bench_compare.py", "nlp_cluster/submit.py"])
@pytest.mark.parametrize("flag", ["--only-first-repair-checkpoint", "--collect-source-conflicts"])
def test_collection_cli_requires_stop_mode(monkeypatch, capsys, script, flag):
    import sys

    spec = importlib.util.spec_from_file_location("only_first_invalid", Path(__file__).parents[1] / "scripts" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "_load_env"):
        monkeypatch.setattr(module, "_load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", [script, flag])
    with pytest.raises(SystemExit):
        module.main()
    assert "requires --stop-after-repair-checkpoint" in capsys.readouterr().err
