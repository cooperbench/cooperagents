"""Shared request settings for mini-SWE workers and the coordinator client."""

from __future__ import annotations

import math
import os
from typing import Any


def sampling_kwargs(temperature: float | None = None) -> dict[str, Any]:
    """Read explicit settings; FORCE keeps precedence over per-call temperature.

    Nonstandard OpenAI fields go in extra_body so both OpenAI SDK and LiteLLM's
    OpenAI-compatible transport send them unchanged to OpenRouter.
    """
    result: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for name, lower, upper in (
        ("temperature", 0, 2), ("top_p", 0, 1), ("presence_penalty", -2, 2),
        ("top_k", 1, math.inf), ("min_p", 0, 1), ("repetition_penalty", 0, 2),
        ("max_tokens", 1, math.inf),
    ):
        key = f"COOPER_{name.upper()}"
        raw = os.getenv(key)
        if name == "temperature":
            raw = os.getenv("COOPER_TEMPERATURE_FORCE") or (
                str(temperature) if temperature is not None else raw
            )
        if not raw:
            continue
        value = int(raw) if name in ("top_k", "max_tokens") else float(raw)
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{key} must be finite and between {lower} and {upper}")
        target = extra if name in ("top_k", "min_p", "repetition_penalty") else result
        target[name] = value

    for key, field in (("COOPER_REASONING_ENABLED", "reasoning"),
                       ("COOPER_REQUIRE_PARAMETERS", "provider")):
        raw = os.getenv(key)
        if raw is not None and raw != "":
            if raw.lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"{key} must be true/false or 1/0")
            enabled = raw.lower() in ("true", "1")
            extra[field] = {"enabled" if field == "reasoning" else "require_parameters": enabled}
    provider = os.getenv("COOPER_PROVIDER_ONLY")
    if provider is not None:
        if not provider.strip() or any(c.isspace() for c in provider.strip()):
            raise ValueError("COOPER_PROVIDER_ONLY must be a single provider slug")
        extra.setdefault("provider", {}).update(
            only=[provider.strip()], allow_fallbacks=False,
        )
    if extra:
        result["extra_body"] = extra
    return result
