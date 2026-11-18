"""Shared request settings for mini-SWE workers and the coordinator client."""

from __future__ import annotations

import math
import os
from typing import Any


def sampling_kwargs(temperature: float | None = None, *, worker: bool = False) -> dict[str, Any]:
    """Read explicit settings; FORCE keeps precedence over per-call temperature.

    Nonstandard OpenAI fields go in extra_body so both OpenAI SDK and LiteLLM's
    OpenAI-compatible transport send them unchanged. COOPER_SEND_EXTRA_BODY
    preserves legacy behavior when unset or true, and rejects those fields when
    explicitly false. Workers may override token and thinking settings.
    """
    result: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    extra_sources: set[str] = set()
    for name, lower, upper in (
        ("temperature", 0, 2), ("top_p", 0, 1), ("presence_penalty", -2, 2),
        ("top_k", 1, math.inf), ("min_p", 0, 1), ("repetition_penalty", 0, 2),
        ("max_tokens", 1, math.inf), ("max_completion_tokens", 1, math.inf),
    ):
        key = f"COOPER_{name.upper()}"
        if worker and name in ("max_tokens", "max_completion_tokens") and os.getenv(f"COOPER_WORKER_{name.upper()}"):
            key = f"COOPER_WORKER_{name.upper()}"
        raw = os.getenv(key)
        if name == "temperature":
            raw = os.getenv("COOPER_TEMPERATURE_FORCE") or (
                str(temperature) if temperature is not None else raw
            )
        if not raw:
            continue
        try:
            value = int(raw) if name in ("top_k", "max_tokens", "max_completion_tokens") else float(raw)
        except ValueError:
            raise ValueError(f"{key} must be a valid number") from None
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{key} must be finite and between {lower} and {upper}")
        target = extra if name in ("top_k", "min_p", "repetition_penalty") else result
        target[name] = value
        if target is extra:
            extra_sources.add(key)

    if "max_tokens" in result and "max_completion_tokens" in result:
        raise ValueError("max_tokens and max_completion_tokens are mutually exclusive")
    effort = os.getenv("COOPER_REASONING_EFFORT")
    if effort:
        try:
            number = float(effort)
        except ValueError:
            result["reasoning_effort"] = effort
        else:
            if not math.isfinite(number):
                raise ValueError("COOPER_REASONING_EFFORT must be finite")
            result["reasoning_effort"] = number

    for key, field in (("COOPER_REASONING_ENABLED", "reasoning"),
                       ("COOPER_REQUIRE_PARAMETERS", "provider")):
        raw = os.getenv(key)
        if raw is not None and raw != "":
            if raw.lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"{key} must be true/false or 1/0")
            enabled = raw.lower() in ("true", "1")
            extra[field] = {"enabled" if field == "reasoning" else "require_parameters": enabled}
            extra_sources.add(key)
    thinking_key = "COOPER_CHAT_TEMPLATE_ENABLE_THINKING"
    if worker and os.getenv("COOPER_WORKER_CHAT_TEMPLATE_ENABLE_THINKING") is not None:
        thinking_key = "COOPER_WORKER_CHAT_TEMPLATE_ENABLE_THINKING"
    thinking = os.getenv(thinking_key)
    if thinking is not None:
        if thinking.lower() not in ("true", "false", "1", "0"):
            raise ValueError(f"{thinking_key} must be true/false or 1/0")
        extra["chat_template_kwargs"] = {"enable_thinking": thinking.lower() in ("true", "1")}
        extra_sources.add(thinking_key)
    provider = os.getenv("COOPER_PROVIDER_ONLY")
    if provider is not None:
        if not provider.strip() or any(c.isspace() for c in provider.strip()):
            raise ValueError("COOPER_PROVIDER_ONLY must be a single provider slug")
        extra_sources.add("COOPER_PROVIDER_ONLY")
        extra.setdefault("provider", {}).update(
            only=[provider.strip()], allow_fallbacks=False,
        )
    raw_send_extra = os.getenv("COOPER_SEND_EXTRA_BODY")
    send_extra = raw_send_extra.lower() if raw_send_extra is not None else None
    if send_extra not in (None, "true", "false", "1", "0"):
        raise ValueError("COOPER_SEND_EXTRA_BODY must be true/false or 1/0")
    if extra:
        if send_extra in ("false", "0"):
            raise ValueError("COOPER_SEND_EXTRA_BODY=false rejects extra fields from: " + ", ".join(sorted(extra_sources)))
        result["extra_body"] = extra
    return result
