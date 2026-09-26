"""Model-sweep runner + benchmark registry."""

from __future__ import annotations

import pytest

from cooperagents.benchmarks import get_benchmark
from cooperagents.benchmarks.runner import ConfigResult, run_config, success_rate
from cooperagents.eval.scoring import TaskScore
from cooperagents.llm import Action, ScriptedLLM

pytest.importorskip("plancraft")


def test_registry_resolves_and_rejects():
    from cooperagents.benchmarks.plancraft import PlanCraftBenchmark

    assert isinstance(get_benchmark("plancraft"), PlanCraftBenchmark)
    with pytest.raises(KeyError):
        get_benchmark("nope")


def test_success_rate_helper():
    assert success_rate([TaskScore("a", 1.0), TaskScore("b", 0.0)]) == 0.5
    assert success_rate([]) == 0.0


def test_run_config_solo_on_plancraft_impossible():
    bench = get_benchmark("plancraft")
    impossible = [e for e in bench.instances("val.small") if e.impossible][:2]

    # A scripted policy that always declares "impossible" — correct on these.
    def factory(_agent_id, _role):
        return ScriptedLLM({"*": [Action(tool="impossible", args={"reason": "no recipe"})]})

    scores = run_config(bench, impossible, llm_factory=factory, team_size=1, step_limit=5)
    assert len(scores) == 2
    assert success_rate(scores) == 1.0  # declaring impossible is correct here


def test_run_config_returns_config_result_shape():
    # ConfigResult is a simple carrier; verify its fields round-trip.
    r = ConfigResult(model="m", mode="solo", success_rate=0.5, n=2, scores=[])
    assert r.model == "m" and r.mode == "solo" and r.n == 2
