"""Finance-Agent smoke tests — offline, no API keys (fake backend + grader)."""

from __future__ import annotations

import pytest

from cooperagents.agent import Agent
from cooperagents.benchmarks.finance import (
    FakeFinanceBackend,
    FinanceAgentBenchmark,
    FinanceInstance,
    KeywordGrader,
    LiveFinanceBackend,
    LLMRubricGrader,
    finance_from_env,
    load_finance,
)
from cooperagents.benchmarks.runner import run_config, success_rate
from cooperagents.bus.memory import InMemoryBus
from cooperagents.llm import Action, ScriptedLLM

_INST = FinanceInstance("f1", "What was Salesforce quarterly revenue for the quarter ended Dec 31, 2024?", "$9.29 billion")


def _bench() -> FinanceAgentBenchmark:
    backend = FakeFinanceBackend(web={"salesforce revenue": "Salesforce reported quarterly revenue of $9.29 billion."})
    return FinanceAgentBenchmark(backend=backend, grader=KeywordGrader(), instances_data=[_INST])


def test_search_then_submit_scores_correct():
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
                    Action(tool="google_search", args={"query": "salesforce revenue Q4 2024"}),
                    Action(tool="submit_answer", args={"answer": "Revenue was $9.29 billion.", "sources": ["sec.gov"]}),
                ]
            }
        ),
        bus=InMemoryBus("t"),
        toolset=bench.toolset(),
        step_limit=8,
    )
    res = agent.run()
    assert res.artifact.answer == "Revenue was $9.29 billion."
    assert bench.scorer().score(_INST, res.artifact.text()).passed


def test_runner_end_to_end_offline():
    bench = _bench()

    def factory(_agent_id, _role):
        return ScriptedLLM({"*": [Action(tool="submit_answer", args={"answer": "$9.29 billion"})]})

    scores = run_config(bench, [_INST], llm_factory=factory, team_size=1, step_limit=5)
    assert success_rate(scores) == 1.0


def test_wrong_answer_fails():
    assert not _bench().scorer().score(_INST, "About $12 billion.").passed


def test_unconfigured_services_raise_with_guidance():
    bench = FinanceAgentBenchmark()
    with pytest.raises(NotImplementedError):
        bench.instances()
    with pytest.raises(NotImplementedError):
        bench.toolset().dispatch(bench.make_env(_INST), Action(tool="google_search", args={"query": "x"}))


# --- real services (offline-verifiable via seams) ---------------------------


def test_live_backend_parse_and_retrieve_offline():
    b = LiveFinanceBackend(http_get=lambda _url: "<html><body><p>Revenue was $9.29B.</p></body></html>")
    text = b.parse_html("https://example.com/10q")
    assert text == "Revenue was $9.29B."
    assert "9.29" in b.retrieve_information("revenue")


def test_live_backend_google_search_injected():
    b = LiveFinanceBackend(web_search_fn=lambda q: f"results for {q}")
    assert b.google_search("salesforce") == "results for salesforce"


def test_live_backend_requires_key_without_seam(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    b = LiveFinanceBackend()
    with pytest.raises(NotImplementedError):
        b.google_search("x")


def test_llm_rubric_grader_injected():
    assert LLMRubricGrader("m", complete=lambda _p: "CORRECT").grade("q", "a", "r", "ref") == 1.0
    assert LLMRubricGrader("m", complete=lambda _p: "INCORRECT").grade("q", "a", "r", "ref") == 0.0


def test_load_finance_csv(tmp_path):
    p = tmp_path / "f.csv"
    p.write_text("question,answer,rubric\nWhat is X?,42,exact\n")
    insts = load_finance(p)
    assert len(insts) == 1 and insts[0].question == "What is X?" and insts[0].reference == "42"


def test_finance_from_env_unconfigured_returns_shells(monkeypatch):
    for var in ("FINANCE_DATA", "FINANCE_GRADER_MODEL", "TAVILY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(NotImplementedError):
        finance_from_env().instances()
