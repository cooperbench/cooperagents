import importlib.util
from pathlib import Path
from types import SimpleNamespace


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
