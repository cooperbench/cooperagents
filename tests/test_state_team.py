"""State-backend team run through the harness: solo and team + reducer."""

from __future__ import annotations

from cooperagents.env.base import Environment
from cooperagents.env.state import StateEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.llm import Action, ScriptedLLM
from cooperagents.reducers import best_of_first_nonempty
from cooperagents.tools import ToolSet
from cooperagents.types import Assignment, TeamSpec


class _SubmitToolSet(ToolSet):
    def specs(self) -> list[dict[str, str]]:
        return [{"name": "submit_answer", "description": "Submit. args: {answer}"}]

    def dispatch(self, env: Environment, action: Action):
        if action.tool == "submit_answer":
            env.answer = str(action.args.get("answer", ""))
            return "recorded", True
        return None


def _run(assignments, actions_by_agent):
    spec = TeamSpec(
        run_id="r1",
        repo="none",
        task_id=0,
        features=[],
        assignments=assignments,
        artifact_backend="state",
        toolset_factory=_SubmitToolSet,
        reducer=best_of_first_nonempty,
    )
    harness = UnifiedHarness(step_limit=5)
    llm = ScriptedLLM(actions_by_agent)
    return harness.run(spec, env_factory=lambda _id: StateEnv(), llm=llm)


def test_solo_state_run_submits_single_artifact():
    res = _run(
        [Assignment(agent_id="agent1", role="lead", task="answer")],
        {"*": [Action(tool="submit_answer", args={"answer": "Paris"})]},
    )
    assert res.integrated is not None
    assert res.integrated.artifact.text() == "Paris"
    assert res.integrated.patch == ""


def test_team_state_run_reduces_to_one():
    res = _run(
        [
            Assignment(agent_id="agent1", role="lead", task="answer"),
            Assignment(agent_id="agent2", role="member", task="answer"),
        ],
        {
            "agent1": [Action(tool="submit_answer", args={"answer": "Paris"})],
            "agent2": [Action(tool="submit_answer", args={"answer": "Lyon"})],
        },
    )
    # best_of_first_nonempty selects the first agent's answer.
    assert res.integrated is not None
    assert res.integrated.artifact.text() == "Paris"
    assert set(res.seeds) == {"agent1", "agent2"}
