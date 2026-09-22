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
