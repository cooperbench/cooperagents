"""StateEnv + ToolSet: the builtin agent on a non-git substrate."""

from __future__ import annotations

from cooperagents.agent import Agent
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.artifact import StateArtifact
from cooperagents.env.base import Environment
from cooperagents.env.state import StateEnv
from cooperagents.llm import Action, ScriptedLLM
from cooperagents.tools import ToolSet


class _QAToolSet(ToolSet):
    """Toy answer-style tool set: a fake search plus submit_answer."""

    def specs(self) -> list[dict[str, str]]:
        return [
            {"name": "search", "description": "Search the corpus. args: {query}"},
            {"name": "submit_answer", "description": "Submit the final answer. args: {answer}"},
        ]

    def dispatch(self, env: Environment, action: Action):
        if action.tool == "search":
            env.record(f"search: {action.args.get('query', '')}")
            return "results: [doc1, doc2]", False
        if action.tool == "submit_answer":
            env.answer = str(action.args.get("answer", ""))
            env.record(f"answer: {env.answer}")
            return "answer recorded", True
        return None


def _agent(actions, **kw):
    return Agent(
        agent_id="agent1",
        role="member",
        task="answer the question",
        env=StateEnv(),
        llm=ScriptedLLM({"*": actions}),
        bus=InMemoryBus("t"),
        toolset=_QAToolSet(),
        **kw,
    )


def test_toolset_replaces_code_tools_but_keeps_coordination():
    a = _agent([Action(tool="finish")])
    names = {t["name"] for t in a.tools}
    assert "search" in names and "submit_answer" in names
    assert not ({"bash", "read_file", "write_file"} & names)  # code tools gone
    assert {"send_message", "task_list", "finish"} <= names  # coordination kept


def test_state_answer_becomes_state_artifact():
    a = _agent(
        [
            Action(tool="search", args={"query": "capital of France"}),
            Action(tool="submit_answer", args={"answer": "Paris"}),
        ]
    )
    res = a.run()
    assert res.status == "submitted"
    assert isinstance(res.artifact, StateArtifact)
    assert res.artifact.text() == "Paris"
    assert res.patch == ""  # non-code contribution submits no diff
    assert any("results" in m["content"] for m in res.messages)


def test_empty_answer_finish_is_vetoed():
    # Finishes immediately without submitting; the empty-contribution veto nudges.
    a = _agent([Action(tool="finish")] * 5, finish_nudges=2)
    res = a.run()
    assert any("submitted an answer" in m["content"] for m in res.messages)


def test_unknown_tool_still_reported():
    a = _agent([Action(tool="nonsense"), Action(tool="submit_answer", args={"answer": "x"})])
    res = a.run()
    assert any("unknown or disallowed" in m["content"] for m in res.messages)
