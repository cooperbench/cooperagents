"""Full SDK-shaped requests use the existing parser and isolate fixed summaries."""

import pytest
from litellm import ModelResponse
from tests.test_repair_replay import completion, make_checkpoint

from cooperagents.completion import CompletionBinding, CompletionSettings
from cooperagents.repair import run_repair_checkpoint
from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModel


def settings(model):
    return CompletionSettings(
        model=model, revision="fixed-sha", generation={"max_tokens": 128, "temperature": 1.0, "top_p": 1.0}, timeout=30.0
    )


def test_replay_full_history_and_training_override(monkeypatch, tmp_path):
    checkpoint = make_checkpoint(monkeypatch, tmp_path, attempts=1)
    requests = []
    monkeypatch.setenv("COOPER_TEMPERATURE_FORCE", "0")

    def action(request):
        requests.append(request)
        if len(requests) == 1:
            return completion("printf 'fixed = 1\\n' > a.py; printf observed")
        return completion("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")

    def forbidden(*args, **kwargs):
        pytest.fail("historical service or summary unexpectedly called")

    monkeypatch.setattr("litellm.completion", forbidden)
    result = run_repair_checkpoint(
        checkpoint,
        scratch=tmp_path / "scratch",
        run_id="new",
        completion=CompletionBinding(action, settings("policy"), forbidden, settings("summary")),
    )
    assert len(requests) == 2
    assert requests[0].actor_id == "integrator1" and requests[1].call_id == 2
    assert requests[0].settings.model == "policy" and requests[0].settings.generation["temperature"] == 1
    assert requests[0].tools == requests[1].tools
    assert any(m["role"] == "tool" and "observed" in str(m) for m in requests[1].messages)
    assert requests[1].messages[2]["content"] is None
    assert "fixed = 1" in result.integrated.patch


def test_summary_and_malformed_output_use_existing_paths():
    actions, summaries = [], []

    def action(request):
        actions.append(request)
        return ModelResponse(choices=[{"message": {"role": "assistant", "content": "missing tool"}}])

    def summary(request):
        summaries.append(request)
        return ModelResponse(choices=[{"message": {"role": "assistant", "content": "<think>hidden</think>saved summary"}}])

    model = LitellmModel(
        model_name="old",
        cost_tracking="ignore_errors",
        completion=CompletionBinding(action, settings("policy"), summary, settings("summary")),
        actor_id="integrator2",
    )
    from cooperagents.vendor.mini_swe.exceptions import FormatError

    with pytest.raises(FormatError):
        model.query([{"role": "user", "content": "repair"}])
    output = model.summarize_context([{"role": "tool", "tool_call_id": "x", "content": "observation"}], "summarize")
    assert output["content"] == "saved summary"
    assert len(actions) == len(summaries) == 1
    assert actions[0].purpose == "action" and summaries[0].purpose == "summary"
    assert summaries[0].tools is None and summaries[0].settings.model == "summary"
    assert summaries[0].call_id == 2


def test_injected_failure_never_retries_or_falls_back(monkeypatch):
    calls = []

    def fail(request):
        calls.append(request)
        raise TimeoutError("failed")

    model = LitellmModel(
        model_name="old", completion=CompletionBinding(fail, settings("policy"), fail, settings("summary")), actor_id="integrator1"
    )
    monkeypatch.setattr("litellm.completion", lambda **kwargs: pytest.fail("fallback"))
    from cooperagents.repair import RepairInfrastructureError

    with pytest.raises(RepairInfrastructureError):
        model.query([{"role": "user", "content": "repair"}])
    assert len(calls) == 1


def test_credentials_and_invalid_sdk_history_rejected():
    with pytest.raises(ValueError):
        CompletionSettings(model="policy", revision="sha", generation={"api_key": "secret", "max_tokens": 128}, timeout=30.0)
    from cooperagents.completion import MiniSweCompletionRequest

    with pytest.raises(ValueError):
        MiniSweCompletionRequest.build(
            messages=[{"role": "tool", "content": "missing id"}],
            tools=None,
            settings=settings("policy"),
            actor_id="integrator1",
            call_id=1,
            purpose="summary",
        )


def test_worker_propagates_injected_failure_without_replay_wrapper(monkeypatch, tmp_path):
    from cooperagents.env.local import LocalEnv
    from cooperagents.repair import RepairInfrastructureError
    from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

    def fail(request):
        raise TimeoutError("transport unavailable")

    env = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        with pytest.raises(RepairInfrastructureError):
            run_mini_swe_agent(
                env,
                task="repair",
                agent_id="integrator1",
                role="integrator",
                model_name="fixture",
                step_limit=5,
                cost_limit=5.0,
                completion=CompletionBinding(fail, settings("policy"), fail, settings("summary")),
            )
    finally:
        env.cleanup()


def test_summary_context_failure_does_not_trigger_emergency_truncation(monkeypatch):
    from cooperagents.repair import RepairInfrastructureError
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent

    calls = []

    def fail(request):
        calls.append(request)
        raise ValueError("ContextWindowExceededError: context window too long")

    model = LitellmModel(
        model_name="old", completion=CompletionBinding(fail, settings("policy"), fail, settings("summary")), actor_id="integrator1"
    )
    agent = DefaultAgent(
        model, object(), system_template="system", instance_template="{{task}}", compaction_token_trigger=1, compaction_keep_recent_turns=0
    )
    agent.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old turn"},
    ]
    agent._last_prompt_tokens = 2
    monkeypatch.setattr(agent, "_emergency_truncate", lambda: pytest.fail("injected failure truncated context"))
    with pytest.raises(RepairInfrastructureError):
        agent.query()
    assert len(calls) == 1 and calls[0].purpose == "summary"
    assert len(agent.messages) == 3 and agent.n_calls == 0
