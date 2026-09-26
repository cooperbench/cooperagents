"""WorkBench benchmark wiring against the real (current) WorkBench repo."""

from __future__ import annotations

import ast
import json

import pytest

from cooperagents.agent import Agent
from cooperagents.bus.memory import InMemoryBus
from cooperagents.llm import Action, ScriptedLLM

try:
    from cooperagents.benchmarks.workbench import WorkBenchBenchmark, find_workbench

    find_workbench()
    _HAVE_WB = True
except Exception:
    _HAVE_WB = False

pytestmark = pytest.mark.skipif(not _HAVE_WB, reason="WorkBench checkout not available")


def _parse_action(action_str: str) -> tuple[str, dict]:
    """'email.delete_email.func(email_id="00000479")' -> ('email.delete_email', {...})."""
    name = action_str.split(".func(")[0]
    call = ast.parse(action_str, mode="eval").body
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    return name, kwargs


def test_scorer_matches_ground_truth():
    bench = WorkBenchBenchmark()
    inst = bench.instances("email")[0]
    scorer = bench.scorer()
    # Ground truth scored against itself is correct with no side effects.
    good = scorer.score(inst, json.dumps(inst.outcome))
    assert good.passed
    assert good.detail["side_effects"] is False
    # An empty submission fails a task that requires an action.
    assert not scorer.score(inst, json.dumps([])).passed


def test_agent_tool_call_recorded_and_scored():
    bench = WorkBenchBenchmark()
    inst = bench.instances("email")[0]
    tool_name, args = _parse_action(inst.outcome[0])
    env = bench.make_env(inst)
    agent = Agent(
        agent_id="a1",
        role="lead",
        task=bench.task_prompt(inst),
        env=env,
        llm=ScriptedLLM({"*": [Action(tool=tool_name, args=args), Action(tool="finish")]}),
        bus=InMemoryBus("t"),
        toolset=bench.toolset(),
        step_limit=6,
    )
    res = agent.run()
    recorded = json.loads(res.artifact.text())
    assert recorded == inst.outcome  # the side-effect call was captured in WB's format
    assert bench.scorer().score(inst, res.artifact.text()).passed


def test_toolset_swaps_code_tools_for_workbench_tools():
    bench = WorkBenchBenchmark()
    ts = bench.toolset()
    names = {s["name"] for s in ts.specs()}
    assert "email.send_email" in names and "calendar.create_event" in names
    assert len(names) == 27
