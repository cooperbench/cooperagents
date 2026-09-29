"""BrowseComp-Plus smoke tests — offline, no API keys (fake retriever + judge)."""

from __future__ import annotations

import json

import pytest

from cooperagents.agent import Agent
from cooperagents.benchmarks.browsecomp import (
    BM25Retriever,
    BrowseCompBenchmark,
    BrowseCompInstance,
    InMemoryRetriever,
    LLMJudge,
    SubstringJudge,
    browsecomp_from_env,
    load_browsecomp,
)
from cooperagents.benchmarks.runner import run_config, success_rate
from cooperagents.bus.memory import InMemoryBus
from cooperagents.llm import Action, ScriptedLLM

_DOCS = {
    "d1": "Paris is the capital and most populous city of France, on the Seine.",
    "d2": "Berlin is the capital of Germany.",
}
_INST = BrowseCompInstance("q1", "What is the capital of France?", "Paris", ["d1"])


def _bench() -> BrowseCompBenchmark:
    return BrowseCompBenchmark(retriever=InMemoryRetriever(_DOCS), judge=SubstringJudge(), instances_data=[_INST])


def test_search_open_submit_flow_scores_correct():
    bench = _bench()
    env = bench.make_env(_INST)
    agent = Agent(
        agent_id="a1",
        role="lead",
        task=bench.task_prompt(_INST),
        env=env,
        llm=ScriptedLLM(
            {
                "*": [
                    Action(tool="search", args={"query": "capital of France"}),
                    Action(tool="open", args={"doc_id": "d1"}),
                    Action(tool="submit_answer", args={"answer": "The capital of France is Paris."}),
                ]
            }
        ),
        bus=InMemoryBus("t"),
        toolset=bench.toolset(),
        step_limit=8,
    )
    res = agent.run()
    assert res.artifact.answer == "The capital of France is Paris."
    assert "d1" in res.artifact.trajectory  # visited doc recorded
    assert bench.scorer().score(_INST, res.artifact.text()).passed


def test_runner_end_to_end_offline():
    bench = _bench()

    def factory(_agent_id, _role):
        return ScriptedLLM({"*": [Action(tool="submit_answer", args={"answer": "Paris"})]})

    scores = run_config(bench, [_INST], llm_factory=factory, team_size=1, step_limit=5)
    assert success_rate(scores) == 1.0


def test_wrong_answer_fails():
    assert not _bench().scorer().score(_INST, "The capital is Berlin.").passed


def test_unconfigured_services_raise_with_guidance():
    bench = BrowseCompBenchmark()
    with pytest.raises(NotImplementedError):
        bench.instances()
    with pytest.raises(NotImplementedError):
        bench.toolset().dispatch(bench.make_env(_INST), Action(tool="search", args={"query": "x"}))


# --- real services (offline-verifiable via seams) ---------------------------


def test_bm25_ranks_relevant_document_first():
    r = BM25Retriever({"d1": "paris is the capital of france", "d2": "berlin is the capital of germany"})
    hits = r.search("capital of france", k=2)
    assert hits and hits[0][0] == "d1"
    assert r.open("d1").startswith("paris")


def test_llm_judge_uses_injected_complete():
    yes = LLMJudge("m", complete=lambda _p: "YES, they match")
    no = LLMJudge("m", complete=lambda _p: "NO")
    assert yes.judge("q", "a", "gold") is True
    assert no.judge("q", "a", "gold") is False


def test_load_browsecomp_from_jsonl(tmp_path):
    p = tmp_path / "q.jsonl"
    p.write_text(json.dumps({"id": "x1", "query": "capital of france?", "answer": "Paris", "gold_docs": ["d1"]}) + "\n")
    insts = load_browsecomp(p)
    assert len(insts) == 1 and insts[0].instance_id == "x1" and insts[0].answer == "Paris"


def test_real_stack_offline_with_bm25_and_seam_judge():
    inst = BrowseCompInstance("q1", "capital of france", "Paris", ["d1"])
    bench = BrowseCompBenchmark(
        retriever=BM25Retriever({"d1": "paris is the capital of france"}),
        judge=LLMJudge("m", complete=lambda _p: "YES"),
        instances_data=[inst],
    )

    def factory(_a, _r):
        return ScriptedLLM(
            {"*": [Action(tool="search", args={"query": "capital france"}), Action(tool="submit_answer", args={"answer": "Paris"})]}
        )

    assert success_rate(run_config(bench, [inst], llm_factory=factory, team_size=1, step_limit=5)) == 1.0


def test_from_env_unconfigured_returns_shells(monkeypatch):
    for var in ("BROWSECOMP_CORPUS", "BROWSECOMP_QUERIES", "BROWSECOMP_JUDGE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    bench = browsecomp_from_env()
    with pytest.raises(NotImplementedError):
        bench.instances()
