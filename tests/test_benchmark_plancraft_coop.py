"""Cooperative-notebook PlanCraft: auto-captured single solution, no oracle."""

from __future__ import annotations

import pytest

from cooperagents.agent import Agent
from cooperagents.bus.memory import InMemoryBus
from cooperagents.llm import Action, ScriptedLLM

pytest.importorskip("plancraft")

from cooperagents.benchmarks.plancraft_coop import PlanCraftCoopBenchmark  # noqa: E402


def _agent(bench, inst, actions, agent_id="a1"):
    env = bench.make_env(inst)
    env.agent_id = agent_id  # type: ignore[attr-defined]
    agent = Agent(
        agent_id=agent_id, role="lead", task=bench.task_prompt(inst), env=env,
        llm=ScriptedLLM({"*": actions}), bus=InMemoryBus("t"),
        toolset=bench.toolset(), step_limit=6,
    )
    return env, agent.run()


def test_toolset_exposes_notebook_tools_not_commit_not_code():
    bench = PlanCraftCoopBenchmark()
    inst = bench.instances("val.small")[0]
    env = bench.make_env(inst)
    agent = Agent(
        agent_id="a1", role="lead", task="t", env=env,
        llm=ScriptedLLM({"*": [Action(tool="think", args={"thought": "x"})]}),
        bus=InMemoryBus("t"), toolset=bench.toolset(),
    )
    names = {t["name"] for t in agent.tools}
    assert {"note_write", "note_read", "solution_read"} <= names
    assert "solution_commit" not in names  # auto-capture: no commit ritual
    assert not ({"bash", "read_file", "write_file"} & names)


def test_autocapture_scores_without_commit():
    bench = PlanCraftCoopBenchmark()
    imp = next(e for e in bench.instances("val.small") if e.impossible)
    # Just acting (impossible) auto-captures the plan into the shared solution.
    _env, res = _agent(bench, imp, [Action(tool="impossible", args={"reason": "no recipe"})])
    submission = res.artifact.text()
    assert submission  # auto-captured, no explicit commit
    assert bench.scorer().score(imp, submission).passed


def test_no_action_means_no_solution():
    bench = PlanCraftCoopBenchmark()
    imp = next(e for e in bench.instances("val.small") if e.impossible)
    # Only thinking — no state-changing action — captures nothing, so it fails.
    _env, res = _agent(bench, imp, [Action(tool="think", args={"thought": "hmm"})])
    assert res.artifact.text() == ""
    assert not bench.scorer().score(imp, res.artifact.text()).passed


def test_no_oracle_last_writer_wins_over_a_success():
    """A correct episode is NOT oracle-selected: with auto-capture the last
    writer's solution is graded, even if an earlier agent had produced a correct
    one. (The oracle select_by variant would have kept the success.)"""
    bench = PlanCraftCoopBenchmark()
    imp = next(e for e in bench.instances("val.small") if e.impossible)
    nb = bench._notebook(imp)
    # A commits a correct impossible; then B overwrites with a non-solving move.
    _agent(bench, imp, [Action(tool="impossible", args={"reason": "correct"})], agent_id="A")
    _agent(bench, imp, [Action(tool="move", args={"from": "[I1]", "to": "[A1]", "quantity": "1"})], agent_id="B")
    assert not bench.scorer().score(imp, nb.read_solution()).passed  # B's wrong plan is what gets graded
    part = nb.participation()
    assert part["A"]["solution"] >= 1 and part["B"]["solution"] >= 1  # both contributed; last wins
