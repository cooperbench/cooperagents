"""PlanCraft benchmark wiring against the real simulator."""

from __future__ import annotations

import pytest

from cooperagents.agent import Agent
from cooperagents.bus.memory import InMemoryBus
from cooperagents.llm import Action, ScriptedLLM

pytest.importorskip("plancraft")

from cooperagents.benchmarks.plancraft import PlanCraftBenchmark  # noqa: E402


def _run(instance, actions):
    bench = PlanCraftBenchmark(max_steps=30)
    env = bench.make_env(instance)
    agent = Agent(
        agent_id="a1",
        role="lead",
        task=bench.task_prompt(instance),
        env=env,
        llm=ScriptedLLM({"*": actions}),
        bus=InMemoryBus("t"),
        toolset=bench.toolset(),
        step_limit=8,
    )
    res = agent.run()
    return bench, res


def test_toolset_exposes_actions_not_code_tools():
    bench = PlanCraftBenchmark()
    inst = bench.instances("val.small")[0]
    env = bench.make_env(inst)
    agent = Agent(
        agent_id="a1",
        role="lead",
        task="t",
        env=env,
        llm=ScriptedLLM({"*": [Action(tool="think", args={"thought": "x"})]}),
        bus=InMemoryBus("t"),
        toolset=bench.toolset(),
    )
    names = {t["name"] for t in agent.tools}
    assert {"move", "smelt", "think", "search", "impossible"} <= names
    assert not ({"bash", "read_file", "write_file"} & names)


def test_correct_impossible_declaration_scores_success():
    bench = PlanCraftBenchmark()
    imp = next(e for e in bench.instances("val.small") if e.impossible)
    _b, res = _run(imp, [Action(tool="impossible", args={"reason": "no valid recipe"})])
    score = bench.scorer().score(imp, res.artifact.text())
    assert score.passed  # correct impossible-declaration == success


def test_wrong_impossible_on_solvable_scores_zero():
    bench = PlanCraftBenchmark()
    solvable = next(e for e in bench.instances("val.small") if not e.impossible)
    _b, res = _run(solvable, [Action(tool="impossible", args={"reason": "giving up"})])
    score = bench.scorer().score(solvable, res.artifact.text())
    assert not score.passed  # incorrect stop on a solvable task == failure
