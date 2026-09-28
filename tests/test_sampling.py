"""Offline request parity: real SDK serialization, no paid/network calls."""

import httpx
import openai
import pytest

from cooperagents.planner import _default_planner_complete
from cooperagents.sampling import sampling_kwargs
from cooperagents.workers.mini_swe_worker import build_model


def test_worker_coordinator_profile_parity(monkeypatch):
    import json
    import os

    for key in os.environ:
        if key.startswith("COOPER_"):
            monkeypatch.delenv(key)
    profile = {
        "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
        "COOPER_TEMPERATURE_FORCE": "1.0",
        "COOPER_TOP_P": "0.95",
        "COOPER_TOP_K": "20",
        "COOPER_PRESENCE_PENALTY": "1.5",
        "COOPER_REASONING_ENABLED": "false",
        "COOPER_REQUIRE_PARAMETERS": "true",
        "COOPER_PROVIDER_ONLY": "venice",
    }
    for key, value in profile.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("COOPER_MAX_TOKENS", "4096")
    worker = build_model("qwen/qwen3.5-9b", temperature=0.0)
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Try another command."}}]})

    client = openai.OpenAI(
        api_key="test-only", base_url="https://example.test/v1", http_client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr(openai, "OpenAI", lambda **_: client)
    coordinator = _default_planner_complete("qwen/qwen3.5-9b", None, None)
    assert coordinator is not None
    assert coordinator("stalled") == "Try another command."

    def worker_completion(**kwargs):
        # Exercise the vendored worker's final LiteLLM boundary, not just a helper.
        kwargs.pop("tools", None)
        kwargs.pop("api_base", None)
        kwargs.pop("api_key", None)
        kwargs.pop("drop_params", None)
        kwargs.pop("timeout", None)
        kwargs["model"] = kwargs["model"].removeprefix("openai/")
        return client.chat.completions.create(**kwargs)

    monkeypatch.setattr("litellm.completion", worker_completion)
    worker._query_inner([{"role": "user", "content": "work"}])
    for payload in captured:
        assert "min_p" not in payload and "repetition_penalty" not in payload
        assert {
            key: payload[key]
            for key in (
                "model",
                "temperature",
                "top_p",
                "top_k",
                "presence_penalty",
                "reasoning",
                "provider",
                "max_tokens",
            )
        } == {
            "model": "qwen/qwen3.5-9b",
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "presence_penalty": 1.5,
            "reasoning": {"enabled": False},
            "provider": {"require_parameters": True, "only": ["venice"], "allow_fallbacks": False},
            "max_tokens": 4096,
        }
    monkeypatch.setenv("COOPER_CHAT_TEMPLATE_ENABLE_THINKING", "false")
    monkeypatch.setenv("COOPER_WORKER_CHAT_TEMPLATE_ENABLE_THINKING", "true")
    build_model("qwen/qwen3.5-9b")._query_inner([{"role": "user", "content": "work"}])
    coordinator("stalled")
    assert captured[-2]["chat_template_kwargs"] == {"enable_thinking": True}
    assert captured[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    client.close()


def test_invalid_sampling_is_rejected(monkeypatch):
    monkeypatch.setenv("COOPER_TOP_P", "nan")
    with pytest.raises(ValueError, match="COOPER_TOP_P"):
        sampling_kwargs()


@pytest.mark.parametrize("value", ["", "  ", "venice other"])
def test_invalid_provider_is_rejected(monkeypatch, value):
    monkeypatch.setenv("COOPER_PROVIDER_ONLY", value)
    with pytest.raises(ValueError, match="COOPER_PROVIDER_ONLY"):
        sampling_kwargs()


def test_sglang_nonthinking_profile(monkeypatch):
    monkeypatch.delenv("COOPER_REASONING_ENABLED", raising=False)
    monkeypatch.delenv("COOPER_REQUIRE_PARAMETERS", raising=False)
    monkeypatch.delenv("COOPER_PROVIDER_ONLY", raising=False)
    monkeypatch.setenv("COOPER_CHAT_TEMPLATE_ENABLE_THINKING", "false")
    extra = sampling_kwargs()["extra_body"]
    assert extra["chat_template_kwargs"] == {"enable_thinking": False}
    assert "provider" not in extra and "reasoning" not in extra


def test_coordinator_ignores_unparsed_tool_markup(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "cooperagents.vendor.mini_swe.models.litellm_model", None)
    from cooperagents.harness import _SEND_MESSAGE_TOOL

    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "<tool_call>raw</tool_call>"}}]})

    client = openai.OpenAI(
        api_key="test-only", base_url="https://example.test/v1", http_client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr(openai, "OpenAI", lambda **_: client)
    complete = _default_planner_complete("test-model", None, None, tools=[_SEND_MESSAGE_TOOL])
    assert complete is not None
    assert complete("coordinate") == []
    client.close()


def test_injected_coordinator_uses_separate_endpoint_and_records_once(monkeypatch, tmp_path):
    import json
    from functools import partial
    from types import SimpleNamespace

    from cooperagents.bus.memory import InMemoryBus
    from cooperagents.harness import _SEND_MESSAGE_TOOL, _Coordinator
    from cooperagents.trajectory import Trajectory, record_call, replay
    from cooperagents.types import Assignment

    for key in ("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY", "COOPER_PROVIDER_ONLY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://workers.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "worker-key")
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), payload))
        message = (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "send_message", "arguments": json.dumps({"recipient": "agent1", "content": "nudge"})},
                }],
            }
            if payload.get("tools")
            else {"role": "assistant", "content": '{"tool":"finish"}'}
        )
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": message,
                    }
                ],
            },
        )

    with (
        openai.OpenAI(
            api_key="training-session",
            base_url="https://training.test/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as training,
        openai.OpenAI(
            api_key="worker-key",
            base_url="https://workers.test/v1",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as workers,
    ):
        journal = Trajectory(tmp_path / "trajectory.jsonl")
        trace = partial(journal.emit, "coordinator")

        def complete(prompt):
            response = record_call(
                trace, training.chat.completions.create, model="Qwen/Qwen3-1.7B", messages=[{"role": "user", "content": prompt}],
                tools=[_SEND_MESSAGE_TOOL], tool_choice="auto",
            )
            return [
                {"name": call.function.name, "arguments": call.function.arguments}
                for call in response.choices[0].message.tool_calls or []
            ]

        coordinator = _Coordinator(
            {},
            "fixed-worker-model",
            assignments=[Assignment(agent_id="agent1", role="lead", task="feature")],
            bus=InMemoryBus("test"),
            complete=complete,
            trace=trace,
        )
        coordinator.register("agent1", SimpleNamespace(messages=[]))
        coordinator.decide(initial=True)
        assert coordinator.error is None
        assert coordinator.drain("agent1") == ["[coordinator] nudge"]

        def worker_completion(**kwargs):
            assert kwargs.pop("api_base") == "https://workers.test/v1"
            assert kwargs.pop("api_key") == "worker-key"
            kwargs.pop("tools", None)
            kwargs.pop("drop_params", None)
            kwargs.pop("timeout", None)
            return workers.chat.completions.create(**kwargs)

        monkeypatch.setattr("litellm.completion", worker_completion)
        # Workers and repair share this model builder; summaries use the same client's
        # query boundary without tool definitions.
        model = build_model("fixed-worker-model")
        model._query_inner([{"role": "user", "content": "worker"}])
        model._query_inner([{"role": "user", "content": "repair"}])
        model._query_inner([{"role": "user", "content": "summary"}], _tools=None)
        journal.close()

    assert [url for url, _ in seen] == ["https://training.test/v1/chat/completions"] + ["https://workers.test/v1/chat/completions"] * 3
    assert seen[0][1]["model"] == "Qwen/Qwen3-1.7B"
    assert seen[0][1]["tools"][0]["function"]["name"] == "send_message"
    assert all(payload["model"] == "openai/fixed-worker-model" for _, payload in seen[1:])
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert sum(row["event"] == "request" for row in rows) == 1
    assert sum(row["event"] == "response" for row in rows) == 1
    assert {"decision", "nudge", "delivery"} <= {row["event"] for row in rows}
    assert not replay(journal.path)["pending_calls"]
