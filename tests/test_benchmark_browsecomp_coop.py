"""Cooperative-notebook BrowseComp: shared answer via auto-capturing submit."""

from __future__ import annotations

from cooperagents.agent import Agent
from cooperagents.benchmarks.browsecomp import BrowseCompInstance, InMemoryRetriever, SubstringJudge
from cooperagents.benchmarks.browsecomp_coop import BrowseCompCoopBenchmark
from cooperagents.bus.memory import InMemoryBus
from cooperagents.llm import Action, ScriptedLLM

_DOCS = {"d1": "the capital of france is paris", "d2": "berlin is the capital of germany"}
_INST = [BrowseCompInstance(instance_id="q1", query="capital of france?", answer="paris", gold_docs=["d1"])]


def _bench():
    return BrowseCompCoopBenchmark(retriever=InMemoryRetriever(_DOCS), judge=SubstringJudge(), instances_data=_INST)


def _agent(bench, inst, actions, agent_id="a1"):
    env = bench.make_env(inst)
    env.agent_id = agent_id  # type: ignore[attr-defined]
    agent = Agent(
        agent_id=agent_id, role="lead", task=bench.task_prompt(inst), env=env,
        llm=ScriptedLLM({"*": actions}), bus=InMemoryBus("t"),
        toolset=bench.toolset(), step_limit=8,
    )
    return env, agent.run()


def test_toolset_has_notebook_and_submit():
    bench = _bench()
    env = bench.make_env(_INST[0])
    agent = Agent(
        agent_id="a1", role="lead", task="t", env=env,
        llm=ScriptedLLM({"*": [Action(tool="note_read", args={})]}),
        bus=InMemoryBus("t"), toolset=bench.toolset(),
    )
    names = {t["name"] for t in agent.tools}
    assert {"search", "open", "note_write", "note_read", "solution_read", "submit_answer"} <= names
    assert not ({"bash", "read_file", "write_file"} & names)


def test_submit_answer_autocaptures_shared_solution_and_scores():
    bench = _bench()
    inst = _INST[0]
    _env, res = _agent(bench, inst, [
        Action(tool="search", args={"query": "capital france"}),
        Action(tool="note_write", args={"text": "d1 says paris"}),
        Action(tool="submit_answer", args={"answer": "Paris"}),
    ])
    assert res.artifact.text() == "Paris"  # auto-captured into the shared solution
    assert bench.scorer().score(inst, res.artifact.text()).passed  # judged against gold


def test_shared_answer_across_two_agents():
    bench = _bench()
    inst = _INST[0]
    nb = bench._notebook(inst)
    _agent(bench, inst, [Action(tool="note_write", args={"text": "found: paris in d1"})], agent_id="A")
    _agent(bench, inst, [Action(tool="note_read", args={}), Action(tool="submit_answer", args={"answer": "paris"})], agent_id="B")
    assert nb.read_solution() == "paris"
    assert bench.scorer().score(inst, nb.read_solution()).passed
    part = nb.participation()
    assert part["A"]["scratch"] >= 1 and part["B"]["solution"] >= 1
