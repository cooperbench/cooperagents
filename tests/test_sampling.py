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
        "COOPER_TEMPERATURE_FORCE": "1.0", "COOPER_TOP_P": "0.95",
        "COOPER_TOP_K": "20",
        "COOPER_PRESENCE_PENALTY": "1.5",
        "COOPER_REASONING_ENABLED": "false", "COOPER_REQUIRE_PARAMETERS": "true",
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

    client = openai.OpenAI(api_key="test-only", base_url="https://example.test/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(respond)))
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
        assert {key: payload[key] for key in (
            "model", "temperature", "top_p", "top_k", "presence_penalty",
            "reasoning", "provider", "max_tokens",
        )} == {
            "model": "qwen/qwen3.5-9b", "temperature": 1.0, "top_p": 0.95,
            "top_k": 20, "presence_penalty": 1.5,
            "reasoning": {"enabled": False},
            "provider": {"require_parameters": True, "only": ["venice"], "allow_fallbacks": False}, "max_tokens": 4096,
        }
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
