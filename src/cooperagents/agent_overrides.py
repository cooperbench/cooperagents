"""Strict loading and application of agent-overrides/1 experiment settings."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from cooperagents.agent_coordination_types import AgentCoordinatorConfig, GateConfig, _finite_float, _reject_constant

OVERRIDES_SCHEMA = "agent-overrides/1"

_PROMPTS = {"opening-plan", "wake", "spec-check", "gate"}
_CRITERIA = {"question", "wake", "skip"}
_GATE_COUNTS = {"wake_budget", "per_worker_wake_cap", "min_steps_between_wakes", "heartbeat_steps"}
_GATE_NUMBERS = {"min_seconds_between_wakes", "wake_threshold", "timeout_s"}
_SYSTEM2_COUNTS = {"opening_max_tool_calls", "opening_reserve", "wake_max_tool_calls", "wake_reserve", "spec_gate_max_rejections"}
_SYSTEM2_NUMBERS = {"opening_time_limit_s", "wake_time_limit_s", "spec_check_time_limit_s"}
_SIGNAL_COUNTS = {"history_steps", "no_progress_window", "no_progress_min_failures", "churn_min_steps", "churn_window", "cooldown_steps"}


@dataclass(frozen=True)
class AgentOverrides:
    prompts: dict[str, str] | None = None
    gate_criteria: dict[str, str] | None = None
    playbook: str | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    system2: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = value.keys() - keys
    if unknown:
        raise ValueError(f"unknown {name} key: {min(unknown)}")
    return value


def _text(value: Any, name: str, limit: int | None = None) -> str:
    if not isinstance(value, str) or not value or (limit is not None and len(value) > limit):
        maximum = f" of at most {limit} characters" if limit is not None else ""
        raise ValueError(f"{name} must be a non-empty string{maximum}")
    value.encode("utf-8")
    return value


def _texts(value: Any, name: str, keys: set[str]) -> dict[str, str]:
    return {key: _text(text, f"{name}.{key}", 16_000) for key, text in _object(value, name, keys).items()}


def _numbers(section: dict[str, Any], name: str, counts: set[str]) -> None:
    for key, value in section.items():
        if key in counts:
            minimum = 1 if key.endswith("_max_tool_calls") else 0
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name}.{key} must be an integer >= {minimum}")
            continue
        if type(value) not in (int, float) or not 0 <= value < math.inf:
            raise ValueError(f"{name}.{key} must be finite and nonnegative")
        if key == "wake_threshold" and value > 1:
            raise ValueError("gate.wake_threshold must be between 0 and 1")
        if key == "timeout_s" and not 0 < value <= 10:
            raise ValueError("gate.timeout_s must be in (0, 10]")
        if key in _SYSTEM2_NUMBERS | {"probe_timeout_s"} and value == 0:
            raise ValueError(f"{name}.{key} must be positive")


def _check_reserves(system2: Mapping[str, Any]) -> None:
    for phase in ("opening", "wake"):
        reserve, max_calls = f"{phase}_reserve", f"{phase}_max_tool_calls"
        if reserve in system2 and max_calls in system2 and system2[reserve] >= system2[max_calls]:
            raise ValueError(f"{reserve} must be less than {max_calls}")


def load_overrides(path: str | Path) -> tuple[AgentOverrides, str]:
    raw = Path(path).read_bytes()
    obj = _object(
        json.loads(raw, object_pairs_hook=_unique_object, parse_float=_finite_float, parse_constant=_reject_constant),
        "overrides",
        {"schema", "prompts", "gate_criteria", "playbook", "gate", "system2", "signals"},
    )
    if obj.get("schema") != OVERRIDES_SCHEMA:
        raise ValueError(f"overrides schema must be {OVERRIDES_SCHEMA}")

    gate = _object(obj.get("gate", {}), "gate", _GATE_COUNTS | _GATE_NUMBERS)
    system2 = _object(obj.get("system2", {}), "system2", _SYSTEM2_COUNTS | _SYSTEM2_NUMBERS)
    signals = _object(obj.get("signals", {}), "signals", _SIGNAL_COUNTS | {"probe_timeout_s", "test_command_prefixes"})
    _numbers(gate, "gate", _GATE_COUNTS)
    _numbers(system2, "system2", _SYSTEM2_COUNTS)
    _check_reserves(system2)
    signals = dict(signals)
    if "test_command_prefixes" in signals:
        prefixes = signals.pop("test_command_prefixes")
        if not isinstance(prefixes, list) or not prefixes:
            raise ValueError("signals.test_command_prefixes must be a non-empty list")
        prefixes = tuple(_text(prefix, "signals.test_command_prefixes") for prefix in prefixes)
    else:
        prefixes = ()
    _numbers(signals, "signals", _SIGNAL_COUNTS)
    if prefixes:
        signals["test_command_prefixes"] = prefixes

    return AgentOverrides(
        prompts=_texts(obj["prompts"], "prompts", _PROMPTS) if "prompts" in obj else None,
        gate_criteria=_texts(obj["gate_criteria"], "gate_criteria", _CRITERIA) if "gate_criteria" in obj else None,
        playbook=_text(obj["playbook"], "playbook", 8_000) if "playbook" in obj else None,
        gate=gate,
        system2=system2,
        signals=signals,
    ), sha256(raw).hexdigest()


def apply_overrides(
    gate: GateConfig,
    config: AgentCoordinatorConfig,
    overrides: AgentOverrides,
    cli_explicit: Mapping[str, str],
) -> AgentCoordinatorConfig:
    """Merge settings. Map fields with non-default CLI values to their flag names."""
    for key in sorted((overrides.gate.keys() | overrides.system2.keys()) & cli_explicit.keys()):
        raise ValueError(f"overrides file and {cli_explicit[key]} both set {key}")

    texts = {name: value for name in ("prompts", "gate_criteria", "playbook") if (value := getattr(overrides, name)) is not None}
    merged = replace(
        config,
        gate=replace(gate, **overrides.gate),
        signals=replace(config.signals, **overrides.signals),
        **overrides.system2,
        **texts,
    )
    _check_reserves({name: getattr(merged, name) for name in _SYSTEM2_COUNTS})
    merged.validate()
    merged.gate.validate()
    return merged
