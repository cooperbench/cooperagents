"""Planning violations recover without accepting submission or fabricating agreement."""

import json
import socket
from types import SimpleNamespace

import litellm
import pytest

from cooperagents.env.base import ExecResult
from cooperagents.vendor.mini_swe.exceptions import FormatError, PlanningComplete, Submitted
from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModel
from cooperagents.workers.mini_swe_worker import MiniSweEnvAdapter
from test_coordinator import tool
from test_planning import planning, publish_plan

MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network is forbidden")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(litellm, "completion", forbidden)


@pytest.mark.parametrize("variant", ["human-prompt", "auto-prompt"])
def test_submit_rejection_recovers_and_implementation_still_submits(tmp_path, variant):
    c = planning(tmp_path, coordination_variant=variant)
    ran = []
    events = []
    c.trace = lambda event, **data: events.append((event, data))

    def execute(command, **kwargs):
        ran.append(command)
        return ExecResult(MARKER + "\noriginal output", 0)

    gate = []
    adapter = MiniSweEnvAdapter(
        SimpleNamespace(execute=execute),
        submission_guard=lambda executed: c.submission_error("agent1", executed=executed),
        completion_gate=lambda env: gate.append(True),
    )
    for _ in range(5):
        result = adapter.execute({"command": "echo " + MARKER})
        assert result["returncode"] == 1 and "not executed" in result["output"]
    assert not ran and not gate and not c._finished and c.phase == "planning"
    result = adapter.execute({"command": "printf '%s\\n' " + MARKER})
    assert result["returncode"] == 1 and "command ran" in result["output"]
    assert "original output" in result["output"] and len(ran) == 1 and not gate
    rejected = [data for event, data in events if event == "submission_rejected"]
    assert len(rejected) == 6 and rejected[-1]["command_executed"]
    publish_plan(c)
    for aid in c._assignments:
        assert c.confirm_plan(aid, {"version": c._version}, sole=True)["returncode"] == 0
    c._observed_confirmations = dict(c._confirmations)
    c.finish_planning(c._version)
    with c._condition:
        for aid, agent in c._agents.items():
            c._restore_plan_locked(aid, agent)
    with pytest.raises(Submitted):
        adapter.execute({"command": "echo " + MARKER})
    assert gate == [True]


def test_finalizing_and_planning_only_completion_do_not_accept_submit(tmp_path):
    c = planning(tmp_path, planning_only=True)
    c.phase = "finalizing"
    error = c.submission_error("agent1", executed=False)
    assert "Current phase: finalizing" in error and "call confirm_plan" not in error
    c._final_plan = {"version": 1}
    c.phase = "complete"
    with pytest.raises(PlanningComplete):
        c.submission_error("agent1", executed=False)


@pytest.mark.parametrize("binding", [False, True])
@pytest.mark.parametrize("name,args,expected", [
    ("dispatch", {}, "Unknown or unavailable tool: dispatch. Available tools: bash."),
    ("bash", {}, "bash failed: Missing required tool arguments"),
    ("bash", {"command": 7}, "bash failed: Expected tool argument type string"),
])
def test_tool_errors_are_factual_on_both_transports(binding, name, args, expected):
    model = LitellmModel(model_name="openai/dummy", cost_tracking="ignore_errors", format_error_template="old coaching {{error}}")
    if binding:
        model.completion = object()
    response = litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    ]}}])
    with pytest.raises(FormatError) as error:
        model._parse_actions(response)
    text = error.value.messages[0]["content"]
    assert expected == text
    assert "example" not in text and "old coaching" not in text and MARKER not in text


def test_confirmation_errors_preserve_state_and_report_versions(tmp_path):
    c = planning(tmp_path)
    c.before_query("agent1", c._agents["agent1"])
    assert "no candidate notebook" in c.confirm_plan("agent1", {"version": 1}, sole=True)["output"]
    publish_plan(c)
    out = c.confirm_plan("agent1", {"version": c._version - 1}, sole=True)
    assert "confirm_plan failed: version mismatch" in out["output"]
    assert f"Current candidate: {c._version}" in out["output"]
    assert not c._confirmations
    out = c.confirm_plan("agent1", {"version": c._version}, sole=False)
    assert "Other actions may have executed" in out["output"]
    assert not c._confirmations


def test_coordinator_error_then_valid_finish(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    c._complete = lambda _: [tool("finish_planning", version=c._version)]
    c.decide(initial=True)
    assert "finish_planning failed: missing valid confirmations" in c._last_error
    assert c.phase == "planning"
    for aid in c._assignments:
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    c.decide(initial=True)
    assert c.phase == "implementation"


@pytest.mark.parametrize("binding", [False, True])
def test_unknown_xml_tool_lists_active_tools(binding):
    model = LitellmModel(model_name="openai/dummy", cost_tracking="ignore_errors")
    if binding:
        model.completion = object()
    response = litellm.ModelResponse(choices=[{"message": {
        "role": "assistant", "content": "<tool_call><function=dispatch></function></tool_call>"
    }}])
    with pytest.raises(FormatError) as error:
        model._parse_actions(response)
    assert error.value.messages[0]["content"] == "Unknown or unavailable tool: dispatch. Available tools: bash."


def test_worker_rejection_is_a_correlated_nonterminal_tool_result(tmp_path):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent

    c = planning(tmp_path)
    ran = []
    env = SimpleNamespace(repo_path="/workspace/repo", execute=lambda **kwargs: ran.append(kwargs) or ExecResult("source", 0))
    model = LitellmModel(model_name="openai/dummy", cost_tracking="ignore_errors")
    adapter = MiniSweEnvAdapter(env, submission_guard=lambda executed: c.submission_error("agent1", executed=executed))
    worker = DefaultAgent(model, adapter, agent_id="agent1", system_template="system", instance_template="{{task}}")
    worker.coordinator = c
    for call_id, command in [("submit-1", "echo " + MARKER), ("inspect-2", "cat source.py")]:
        action = {"tool_name": "bash", "tool_call_id": call_id, "command": command}
        result = worker.execute_actions({"extra": {"actions": [action]}})
        assert result[-1]["role"] == "tool" and result[-1]["tool_call_id"] == call_id
    assert len(ran) == 1 and c.phase == "planning" and not c._finished
    assert all(message["role"] != "exit" for message in worker.messages)
    assert "SUBMISSION_NOT_ALLOWED_IN_PLANNING" in worker.messages[0]["content"]
