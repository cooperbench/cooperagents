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


def test_select_by_picks_highest_key():
    from cooperagents.env.artifact import StateArtifact
    from cooperagents.reducers import select_by
    from cooperagents.types import AgentResult

    a = AgentResult("a", "member", "submitted", artifact=StateArtifact(answer="a"))
    b = AgentResult("b", "member", "submitted", artifact=StateArtifact(answer="b"))
    reducer = select_by(lambda r: 1 if r.agent_id == "b" else 0)
    assert reducer([a, b]).agent_id == "b"
    assert reducer([b, a]).agent_id == "b"  # order-independent


def test_box_sweep_emits_solo_iso_and_team():
    from cooperagents.benchmarks.runner import box_sweep

    bench = get_benchmark("plancraft")

    def make_llm(_m):
        return ScriptedLLM({"*": [Action(tool="impossible", args={"reason": "x"})]})

    results = box_sweep(bench, models=["m1"], make_llm=make_llm, split="val.small", limit=2, team_size=3, step_limit=5)
    assert {r.mode for r in results} == {"solo", "solo-iso", "team"}
    assert all(r.n == 2 for r in results)


def test_box_sweep_no_iso_when_disabled():
    from cooperagents.benchmarks.runner import box_sweep

    bench = get_benchmark("plancraft")

    def make_llm(_m):
        return ScriptedLLM({"*": [Action(tool="impossible", args={"reason": "x"})]})

    results = box_sweep(bench, models=["m1"], make_llm=make_llm, split="val.small", limit=1, team_size=3, step_limit=5, iso_compute=False)
    assert {r.mode for r in results} == {"solo", "team"}
