import json
import subprocess
import sys
from statistics import mean, quantiles

import pytest

from cooperagents.eval.cooperbench import write_run_outputs
from cooperagents.metrics import completion_time_summary
from cooperagents.types import AgentResult, RunResult
from test_bench_budget import compare as compare


def test_completion_time_percentiles():
    durations = [7.0, 1.0, 3.0, 19.0]
    cuts = quantiles(durations, n=100, method="inclusive")
    assert completion_time_summary(durations) == {
        "n": 4, "mean": mean(durations), "p50": cuts[49], "p90": cuts[89], "p95": cuts[94], "p99": cuts[98],
    }


@pytest.mark.parametrize(("durations", "value"), [([], None), ([5.5], 5.5)])
def test_completion_time_small_samples(durations, value):
    assert completion_time_summary(durations) == dict(n=len(durations), mean=value, p50=value, p90=value, p95=value, p99=value)


@pytest.mark.parametrize(("agent", "resume", "skip_eval"), [
    (False, False, False), (True, False, False), (True, True, False), (True, False, True),
])
def test_bench_summary(compare, monkeypatch, tmp_path, capsys, agent, resume, skip_eval):
    name = "agent_main" if agent else "det_main"
    durations = {1: 10.0, 2: 30.0}
    metrics = {
        "gate": {"wake": 2, "skip": 3},
        "usage": {
            "coordinator": dict(calls=4, prompt_tokens=70, cached_tokens=20, completion_tokens=10),
            "coordinator_gate": dict(calls=3, prompt_tokens=12, cached_tokens=0, completion_tokens=0),
        },
    }

    def generate(item, **opts):
        result = RunResult(
            "fixture", item.repo, item.task_id, item.features, duration_seconds=durations[item.task_id],
            seeds={"agent1": AgentResult("agent1", "member", "submitted", cost=0.25, feature_id=1)},
            metrics={"agent_coordinator": metrics} if agent else {},
        )
        pair_dir = write_run_outputs(result, run_name=opts["run_name"], logs_dir=opts["logs_dir"], setting="team")
        (pair_dir / "eval.json").write_text(json.dumps({
            "both_passed": item.task_id == 1,
            "feature1": {"passed": True},
            "feature2": {"passed": item.task_id == 1},
        }))
        return {"duration": result.duration_seconds}

    if resume:
        for task in durations:
            generate(compare.WorkItem("fixture", task, [1, 2]), run_name=name, logs_dir=tmp_path)
    monkeypatch.setattr(compare, "_load_env", lambda: None)
    monkeypatch.setattr(compare, "run_team", generate if not resume else lambda *a, **k: pytest.fail("resumed pair regenerated"))
    monkeypatch.setattr(
        compare, "run_eval",
        (lambda *a, **k: pytest.fail("generation-only run evaluated")) if skip_eval else lambda *a, **k: subprocess.CompletedProcess([], 0),
    )
    family_flags = ["--agent-coordinator", "--agent-gate", "rule", "--git-share"] if agent else ["--deterministic-coordinator"]
    monkeypatch.setattr(sys, "argv", [
        "bench_compare.py", "--pairs", "fixture:1:1,2", "fixture:2:2,1", "--team-name", name,
        "--log-dir", str(tmp_path), "--team-only", "--no-seed", "--coop-tools", *family_flags,
        *(["--resume"] if resume else []), *(["--skip-eval"] if skip_eval else []),
    ])
    compare.main()

    output = capsys.readouterr().out
    if not skip_eval:
        first_line = output.index("  pass-rate:")
        legacy = (
            "  pass-rate:  solo 0/2   team 1/2\n"
            "  features :  solo 0/4   team 3/4\n"
            f"  avg time :  solo 0s   team {0 if resume else 20}s\n"
        )
        assert output[first_line:].startswith(
            legacy
            + "  team completion time (n=2): p50=20.00s p90=28.00s p95=29.00s p99=29.80s\n"
            + "  team total worker cost: $0.500000\n"
        )
        if agent:
            assert "team agent coordinator: wakes=4 skips=6 coordinator calls=8 tokens=160 gate calls=6 tokens=24" in output

    summary = json.loads((tmp_path / name / "summary.json").read_text())
    assert summary.keys() == {"team"}
    arm = summary["team"]
    assert arm["pairs_passed"] == (None if skip_eval else 1)
    assert arm["features_passed"] == (None if skip_eval else 3)
    assert arm["total_worker_cost"] == 0.5
    assert arm["completion_time"] == completion_time_summary(list(durations.values()))
    assert ("agent_coordinator" in arm) == agent
    if agent:
        assert arm["agent_coordinator"] == {
            "wakes": 4,
            "skips": 6,
            "usage": {
                actor: {key: 2 * value for key, value in payload.items()}
                for actor, payload in metrics["usage"].items()
            },
        }
