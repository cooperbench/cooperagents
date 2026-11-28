import json
from dataclasses import asdict, replace
from hashlib import sha256

import pytest

from cooperagents.agent_coordination_types import AgentCoordinatorConfig, GateConfig, from_json, to_json
from cooperagents.agent_overrides import OVERRIDES_SCHEMA, apply_overrides, load_overrides


def test_all_overrides_round_trip_and_hash_raw_bytes(tmp_path, monkeypatch):
    path = tmp_path / "overrides.json"
    fields = dict(
        prompts={"opening-plan": "Opening fixture", "wake": "Wake fixture", "spec-check": "Check fixture", "gate": "Gate fixture"},
        gate_criteria={"question": "Question fixture", "wake": "Wake criterion fixture", "skip": "Skip criterion fixture"},
        playbook="Playbook fixture π",
        gate=dict(
            wake_budget=3,
            per_worker_wake_cap=2,
            min_steps_between_wakes=1,
            min_seconds_between_wakes=2.5,
            heartbeat_steps=12,
            wake_threshold=0.7,
            timeout_s=1,
        ),
        system2=dict(
            opening_max_tool_calls=5,
            opening_time_limit_s=10.5,
            opening_reserve=1,
            wake_max_tool_calls=3,
            wake_time_limit_s=7.5,
            wake_reserve=0,
            spec_check_time_limit_s=8,
            spec_gate_max_rejections=2,
        ),
        signals=dict(
            history_steps=9,
            no_progress_window=4,
            no_progress_min_failures=1,
            churn_min_steps=2,
            churn_window=8,
            cooldown_steps=0,
            probe_timeout_s=5.5,
            test_command_prefixes=["pytest", "go test"],
        ),
    )
    raw = (json.dumps(dict(schema=OVERRIDES_SCHEMA, **fields), ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(raw)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-fixture")
    config = AgentCoordinatorConfig(reasoning_effort="low")
    gate = replace(config.gate, model="test-model")

    overrides, digest = load_overrides(path)
    assert digest == sha256(raw).hexdigest()
    fields["signals"]["test_command_prefixes"] = ("pytest", "go test")
    assert asdict(overrides) == fields
    effective = apply_overrides(gate, config, overrides, {})
    assert from_json(AgentCoordinatorConfig, to_json(effective)) == effective
    assert asdict(effective) == asdict(config) | {
        "gate": asdict(gate) | fields["gate"],
        "signals": fields["signals"],
        **fields["system2"],
        "prompts": fields["prompts"],
        "gate_criteria": fields["gate_criteria"],
        "playbook": fields["playbook"],
    }


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('"extra":true', "unknown overrides key"),
        ('"gate":{"extra":1}', "unknown gate key"),
        ('"gate":{"wake_budget":1,"wake_budget":2}', "duplicate JSON key"),
        ('"gate":{"wake_budget":true}', "must be an integer"),
        ('"gate":{"heartbeat_steps":-1}', "must be an integer"),
        ('"gate":{"min_seconds_between_wakes":NaN}', "invalid JSON constant"),
        ('"gate":{"min_seconds_between_wakes":Infinity}', "invalid JSON constant"),
        ('"gate":{"min_seconds_between_wakes":1e309}', "expected finite number"),
        ('"gate":{"wake_threshold":1.1}', "between 0 and 1"),
        ('"gate":{"timeout_s":10.01}', "timeout_s"),
        ('"gate":{"timeout_s":0}', "timeout_s"),
        ('"gate":{"timeout_s":true}', "must be finite"),
        ('"system2":{"opening_max_tool_calls":1,"opening_reserve":1}', "opening_reserve must be less"),
        ('"system2":{"wake_max_tool_calls":0}', "must be an integer"),
        ('"system2":{"wake_time_limit_s":0}', "must be positive"),
        ('"system2":{"max_retries":2}', "unknown system2 key"),
        ('"signals":{"probe_timeout_s":0}', "must be positive"),
        ('"signals":{"test_command_prefixes":[]}', "must be a non-empty list"),
        ('"signals":{"test_command_prefixes":[""]}', "must be a non-empty string"),
        ('"signals":{"test_command_prefixes":null}', "must be a non-empty list"),
        ('"signals":{"no_progress_window":1.5}', "must be an integer"),
        ('"prompts":{"wake":""}', "must be a non-empty string"),
        ('"gate_criteria":{"other":"fixture"}', "unknown gate_criteria key"),
        (f'"gate_criteria":{{"wake":"{"x" * 16001}"}}', "at most 16000 characters"),
        (f'"prompts":{{"spec-check":"{"x" * 16001}"}}', "at most 16000 characters"),
        (f'"playbook":"{"x" * 8001}"', "at most 8000 characters"),
    ],
)
def test_load_rejects_invalid_schema_fields(tmp_path, section, message):
    path = tmp_path / "overrides.json"
    path.write_text(f'{{"schema":"agent-overrides/1",{section}}}')
    with pytest.raises(ValueError, match=message):
        load_overrides(path)


@pytest.mark.parametrize("raw", ["[]", "{}", '{"schema":"agent-overrides/2"}', '{"schema":null}'])
def test_load_requires_schema_one(tmp_path, raw):
    path = tmp_path / "overrides.json"
    path.write_text(raw)
    with pytest.raises(ValueError, match="overrides"):
        load_overrides(path)


def test_reserve_is_checked_against_effective_budget(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text('{"schema":"agent-overrides/1","system2":{"wake_reserve":3}}')
    overrides, _ = load_overrides(path)
    gate = GateConfig(policy="rule")
    config = AgentCoordinatorConfig(gate=gate, wake_max_tool_calls=3)
    with pytest.raises(ValueError, match="wake_reserve must be less than wake_max_tool_calls"):
        apply_overrides(gate, config, overrides, {"wake_max_tool_calls": "--agent-wake-max-tool-calls"})
    config = replace(config, wake_max_tool_calls=4)
    assert apply_overrides(gate, config, overrides, {"wake_max_tool_calls": "--agent-wake-max-tool-calls"}).wake_reserve == 3
