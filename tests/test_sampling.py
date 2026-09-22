"""Offline request parity: real SDK serialization, no paid/network calls."""

from pathlib import Path

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
    profile = Path(__file__).resolve().parents[1] / "configs/qwen35-9b-openrouter.env.example"
    for line in profile.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
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
        assert {key: payload[key] for key in (
            "model", "temperature", "top_p", "top_k", "min_p", "presence_penalty",
            "repetition_penalty", "reasoning", "provider", "max_tokens",
        )} == {
            "model": "qwen/qwen3.5-9b", "temperature": 1.0, "top_p": 0.95,
            "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5,
            "repetition_penalty": 1.0, "reasoning": {"enabled": False},
            "provider": {"require_parameters": True}, "max_tokens": 4096,
        }
    client.close()


def test_invalid_sampling_is_rejected(monkeypatch):
    monkeypatch.setenv("COOPER_TOP_P", "nan")
    with pytest.raises(ValueError, match="COOPER_TOP_P"):
        sampling_kwargs()
