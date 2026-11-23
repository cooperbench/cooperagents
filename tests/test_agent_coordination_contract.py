import json
from dataclasses import replace
from pathlib import Path

import pytest

from cooperagents import agent_coordination_types as c
from cooperagents import agent_coordinator_prompts as p
from cooperagents.harness import UnifiedHarness
from cooperagents.types import TeamSpec

FIXTURES = Path(__file__).parent / "fixtures/agent_coordination/contract"

SAMPLES = {
    "diff_probe": c.DiffProbe("abc", {"x": [(1, 2)]}, 2, 1, 5.0),
    "step_record": c.StepRecord("agent1", 1, "pytest", 1, "hash", True),
    "signal": c.Signal("agent1:NO_PROGRESS_FAILING:1", "NO_PROGRESS_FAILING", "hard", {}),
    "gate_state": c.GateState("agent1", "signal", [c.Signal("s", "HUNK_CHURN", "warn", {})], [], {}, {}, {}, []),
    "decision_question": c.DecisionQuestion("decision", "choose", {"WAKE": "wake", "SKIP": "skip"}),
    "decision_request": c.DecisionRequest("state", (c.DecisionQuestion("decision", "choose", {"WAKE": "wake"}),), "gate-v1:abc"),
    "decision_result": c.DecisionResult({"decision": {"WAKE": 1.0}}, {"decision": "WAKE"}, 1.0, 3, 0, 0.1, "model", "provider", "id"),
    "worker_plan": c.WorkerPlan(["a"], [], [], "note"),
    "opening_plan": c.OpeningPlan(1, {"agent1": c.WorkerPlan(["a"], [], [], "note")}, [], [], []),
    "spec_finding": c.SpecFinding("api_mismatch", "1", "quote", "a.py"),
    "spec_verdict": c.SpecVerdict("REJECT", [c.SpecFinding("empty_diff", "1", "")]),
    "signal_config": c.SignalConfig(),
    "gate_config": c.GateConfig(),
    "agent_config": c.AgentCoordinatorConfig(),
}
base = c.GateDecision(
    "c-1",
    "signal",
    "WAKE",
    "NO_PROGRESS",
    "agent1",
    ["s"],
    "model",
    {"WAKE": 0.8, "SKIP": 0.2},
    0.5,
    "liquid/d1.2-ultra",
    1.0,
    3,
    0,
    "gate-v1:abc",
    7,
)
SAMPLES.update(
    {
        "gate_model": base,
        "gate_fallback": replace(base, source="fallback", probabilities=None, error="timeout"),
        **{
            f"gate_{source}": replace(
                base, source=source, probabilities=None, threshold=None, model=None, input_tokens=None, completion_tokens=None
            )
            for source in ("rule", "random", "budget")
        },
    }
)


@pytest.mark.parametrize("name", SAMPLES)
def test_golden_roundtrip(name):
    obj = SAMPLES[name]
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    assert json.loads(c.to_json(obj)) == data
    assert c.from_json(type(obj), json.dumps(data)) == obj
    data["unknown"] = 1
    with pytest.raises(ValueError):
        c.from_json(type(obj), json.dumps(data))


@pytest.mark.parametrize("name", ["gate_state", "gate_model", "opening_plan"])
def test_schema_version(name):
    obj = SAMPLES[name]
    data = json.loads(c.to_json(obj))
    data["schema"] = "wrong/2"
    with pytest.raises(ValueError):
        c.from_json(type(obj), json.dumps(data))


def test_model_requires_probabilities():
    with pytest.raises(ValueError):
        replace(base, probabilities=None)


def test_config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    config = c.GateConfig()
    assert (config.model, config.base_url, config.api_key_env, config.wake_threshold, config.timeout_s) == (
        "liquid/d1.2-ultra",
        "https://openrouter.ai/api/alpha",
        "OPENROUTER_API_KEY",
        0.5,
        3.0,
    )
    config.validate()
    for kw in (
        {"model": ""},
        {"base_url": "http://example.com"},
        {"base_url": "http://localhost.evil"},
        {"base_url": "https://openrouter.ai/api/alpha/"},
        {"api_key_env": "MISSING_KEY"},
        {"wake_threshold": 1.1},
        {"wake_threshold": -0.1},
        {"timeout_s": 0},
        {"timeout_s": 10.1},
    ):
        with pytest.raises(ValueError):
            replace(config, **kw).validate()
    for policy in ("rule", "random"):
        x = c.GateConfig(policy=policy)
        assert all(getattr(x, name) is None for name in ("model", "base_url", "api_key_env", "wake_threshold", "timeout_s"))
        x.validate()
        with pytest.raises(ValueError):
            replace(x, model="x").validate()


def test_prompts():
    import hashlib

    assert p.prompt_id("wake", p.WAKE_PROMPT) == "wake-v1:" + hashlib.sha256(p.WAKE_PROMPT.encode()).hexdigest()[:12]
    texts = [p.GATE_INSTRUCTIONS, p.DECISION_QUESTION_INSTRUCTIONS, p.WAKE_CRITERION, p.SKIP_CRITERION]
    for i in range(4):
        changed = texts.copy()
        changed[i] += " changed"
        assert p.gate_prompt_id(*changed) != p.GATE_PROMPT_ID


@pytest.mark.parametrize("other", ["coordinator", "deterministic_coordinator"])
def test_mutually_exclusive(other):
    with pytest.raises(ValueError, match="Coordinator families are mutually exclusive"):
        TeamSpec("r", "repo", 1, [], agent_coordinator=True, **{other: True})


def test_guard_before_environment():
    def unexpected(_):
        pytest.fail("environment created")

    with pytest.raises(NotImplementedError, match="agent coordinator is not wired"):
        UnifiedHarness().run(TeamSpec("r", "repo", 1, [], agent_coordinator=True), env_factory=unexpected)


@pytest.mark.parametrize("source", ["model", "fallback"])
@pytest.mark.parametrize("missing", ["threshold", "model"])
def test_model_and_fallback_require_model_fields(source, missing):
    decision = base if source == "model" else replace(base, source="fallback", probabilities=None, error="timeout")
    with pytest.raises(ValueError):
        replace(decision, **{missing: None})


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_numbers_rejected_even_in_any(number):
    data = c.to_json(SAMPLES["signal"]).replace('"payload":{}', f'"payload":{{"x":{number}}}')
    with pytest.raises(ValueError):
        c.from_json(c.Signal, data)


@pytest.mark.parametrize("region", [[1], [1, 2, 3], [1, "2"]])
def test_fixed_tuple_length_and_type(region):
    data = json.loads(c.to_json(SAMPLES["diff_probe"]))
    data["hunks"]["x"] = [region]
    with pytest.raises(ValueError):
        c.from_json(c.DiffProbe, json.dumps(data))
