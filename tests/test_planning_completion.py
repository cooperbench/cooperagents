"""Public planning transport stops the real worker lifecycle at the frozen notebook."""

import hashlib
import json
import socket
import threading
from pathlib import Path

import litellm
import pytest

from cooperagents.completion import CompletionBinding, CompletionSettings, parse_completion_tool_calls
from cooperagents.coordination_prompts import COORDINATOR_IMPLEMENTATION_SYSTEM, COORDINATOR_PLANNING_SYSTEM
from cooperagents.env.base import ExecResult
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.planning import PlanningCleanupError, PlanningInfrastructureError
from cooperagents.types import Assignment, TeamSpec


def response(name=None, **arguments):
    if name is None:
        content = "not a tool call"
    else:
        content = (
            f"<tool_call>\n<function={name}>\n"
            + "\n".join(f"<parameter={key}>\n{value}\n</parameter>" for key, value in arguments.items())
            + "\n</function>\n</tool_call>"
        )
    return litellm.ModelResponse(
        choices=[{"message": {"role": "assistant", "content": content}}], usage={"prompt_tokens": 10, "completion_tokens": 1}
    )


def binding(action):
    settings = CompletionSettings(model="dummy", revision="fixed-test", generation={"max_tokens": 2000}, timeout=30.0)

    def summary(request):
        assert request.purpose == "summary" and request.tools is None
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "fixed summary"}}])

    return CompletionBinding(action, settings, summary, settings)


@pytest.mark.parametrize(
    "raw",
    [
        "<tool_call><function=finish_planning><parameter=version>1</parameter></function></tool_call>",
        '<tool_call>{"name":"finish_planning","arguments":{"version":1}}</tool_call>',
    ],
)
def test_public_xml_parser_integer(raw):
    tool = {
        "type": "function",
        "function": {
            "name": "finish_planning",
            "parameters": {
                "type": "object",
                "properties": {"version": {"type": "integer"}},
                "required": ["version"],
                "additionalProperties": False,
            },
        },
    }
    result = response()
    result.choices[0].message.content = raw
    assert json.loads(parse_completion_tool_calls(result, [tool])[0]["arguments"]) == {"version": 1}


@pytest.mark.parametrize(
    "raw",
    [
        "nothing",
        "<tool_call><bad/></tool_call>",
        "<tool_call><function=finish_planning><parameter=version>1</parameter><parameter=version>2</parameter></function></tool_call>",
        "<tool_call><function=finish_planning><parameter=version>1</parameter></function>",
        "<tool_call><function=finish_planning><parameter=version>oops</parameter></function></tool_call>",
        '<tool_call>{"name":"finish_planning","arguments":{"version":1,"version":2}}</tool_call>',
    ],
)
def test_public_xml_parser_rejects_malformed(raw):
    tool = {
        "type": "function",
        "function": {
            "name": "finish_planning",
            "parameters": {"type": "object", "properties": {"version": {"type": "integer"}}, "required": ["version"]},
        },
    }
    result = response()
    result.choices[0].message.content = raw
    with pytest.raises(ValueError):
        parse_completion_tool_calls(result, [tool])


@pytest.mark.parametrize(
    "outcome", ["agreed", "forced", "protocol", "coordinator_service", "worker_service", "callback_protocol", "cleanup"]
)
def test_planning_only_real_lifecycle(tmp_path, monkeypatch, outcome):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network and ordinary completion are forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(litellm, "completion", forbidden)
    monkeypatch.setattr(LocalEnv, "git_diff", forbidden)
    path = tmp_path / "coordination" / "notebook.md"
    candidate = threading.Event()
    requests, worker_requests, environments = [], [], []
    base_ids = []

    def coordinator(request):
        assert request.actor_id == "coordinator" and request.purpose == "action"
        observation = json.loads(request.messages[1]["content"].split("OBSERVATION:\n")[1])
        assert request.messages[0] == {"role": "system", "content": COORDINATOR_PLANNING_SYSTEM}
        assert len(request.messages) == 2
        requests.append(request)
        if outcome == "coordinator_service":
            raise TimeoutError("synthetic service failure")
        if outcome == "callback_protocol":
            from cooperagents.planning import PlanningProtocolError

            raise PlanningProtocolError("native response truncated at token limit")
        tools = {tool["function"]["name"] for tool in request.tools}
        if observation["planning"]["phase"] == "finalizing":
            candidate.set()
            assert tools == {"update_notebook"}
            return (
                response()
                if outcome == "protocol"
                else response("update_notebook", content="Final: agent1 owns a.py; agent2 owns b.py; no shared interfaces; verify imports.")
            )
        assert "finish_planning" in tools
        state = observation["planning"]
        if observation["initial"]:
            return response("send_message", recipient="group", content="Inspect base source and report findings.")
        if len(state["participants"]) == 2 and state["candidate_version"] is None:
            candidate.set()
            return response(
                "update_notebook", content="Candidate: agent1 owns a.py; agent2 owns b.py; no shared interfaces; verify imports."
            )
        if len(state["confirmations"]) == 2:
            return response("finish_planning", version=observation["notebook"]["version"])
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": (
                            "<tool_call><function=send_message><parameter=recipient>group</parameter>"
                            "<parameter=content>Continue inspection.</parameter></function></tool_call>"
                        ),
                    }
                }
            ]
        )

    def worker(request):
        worker_requests.append(request)
        assert request.actor_id in ("agent1", "agent2") and request.purpose == "action"
        assert not any((message.get("content") or "").startswith("[IMPLEMENTATION MODE]") for message in request.messages)
        if outcome == "worker_service":
            raise TimeoutError("synthetic worker service failure")
        phase = next(
            message["content"] for message in reversed(request.messages) if "Current notebook version:" in (message.get("content") or "")
        )
        version = int(phase.split("Current notebook version: ")[1].splitlines()[0])
        if request.call_id == 1:
            return response("send_message", recipient="group", content="Inspected README.md; propose independent edits.")
        if version == 0:
            assert candidate.wait(30)
            return response("bash", command="printf inspected")
        if outcome == "agreed":
            return response("confirm_plan", version=version)
        return response("bash", command="printf inspected")

    def factory(aid):
        assert aid in ("agent1", "agent2"), "No merge, repair or implementation environment"
        env = LocalEnv.fresh()
        environments.append(env)
        env.execute(
            "GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z "
            "git -c user.name=cameron-chen -c user.email=abelco.cy@gmail.com commit --amend --allow-empty -qm 'base' --reset-author"
        )
        env.write_file("README.md", "synthetic pinned source")
        env.execute(
            "git add README.md && GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z "
            "git -c user.name=cameron-chen -c user.email=abelco.cy@gmail.com commit -qm 'test: seed source'"
        )
        base_ids.append(env.execute("git rev-parse HEAD").stdout.strip())
        execute = env.execute

        def run(command, **kwargs):
            if "/cbshared" in command or command.startswith(
                ("git push -q shared", "git fetch -q shared", "git diff --name-only HEAD...shared/")
            ):
                return ExecResult("", 0)
            if command.endswith("cat /coordination/notebook.md"):
                return ExecResult(path.read_text(), 0)
            return execute(command, **kwargs)

        env.execute = run
        if outcome == "cleanup":
            close = env.cleanup

            def failing_cleanup():
                close()
                raise OSError("synthetic cleanup failure after resource release")

            env.cleanup = failing_cleanup
        return env

    spec = TeamSpec(
        run_id="offline",
        repo="synthetic",
        task_id=1,
        features=[1, 2],
        assignments=[Assignment(agent_id=f"agent{i}", task=f"Synthetic feature {i}", feature_id=i) for i in (1, 2)],
        worker="mini_swe",
        model="dummy",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        coordinator=True,
        coordinator_notebook=True,
        repair_integrator=True,
        git_share=True,
    )
    harness = UnifiedHarness(
        step_limit=30,
        planning_step_limit=3 if outcome != "agreed" else 200,
        coordination_mode="group-chat-planning",
        planning_only=True,
        coordinator_completion=binding(coordinator),
        worker_completion=binding(worker),
        coordinator_notebook_path=path,
    )
    if outcome == "cleanup":
        with pytest.raises(PlanningCleanupError) as failure:
            harness.run(spec, env_factory=factory)
        assert failure.value.category == "cleanup"
    elif outcome.endswith("service"):
        with pytest.raises(PlanningInfrastructureError):
            harness.run(spec, env_factory=factory)
    else:
        result = harness.run(spec, env_factory=factory)
        planning = result.metrics["planning"]
        if outcome in ("protocol", "callback_protocol"):
            assert planning["outcome"] == "error"
            assert planning["failure_kind"] == "model_protocol"
            assert planning["final_plan"] is None
        else:
            assert planning["outcome"] == outcome and planning["failure_kind"] is None
            final = planning["final_plan"]
            assert final["sha256"] == hashlib.sha256(final["content"].encode()).hexdigest()
            assert final["version"] >= 1
            assert sorted(planning["worker_base_commits"].values()) == sorted(base_ids)
            assert all(agent.status == "planned" for agent in result.seeds.values())
    if outcome == "agreed":
        first_messages, first_tools = requests[0].messages, requests[0].tools
        offset = len(requests)
        path.unlink()
        candidate.clear()
        spec.run_id = "distinct-sibling"
        second = harness.run(spec, env_factory=factory)
        assert second.metrics["planning"]["outcome"] == "agreed"
        assert requests[offset].messages == first_messages
        assert requests[offset].tools == first_tools
    assert all(not Path(env.repo_path).exists() for env in environments)
    assert requests
    initial = json.loads(requests[0].messages[1]["content"].split("OBSERVATION:\n")[1])
    assert initial["planning"]["seconds_remaining"] == harness.planning_time_limit_s
    assert set(initial["git_snapshots"]["workers"]) == {"agent1", "agent2"}
    assert all(set(snapshot) == {"sha"} for snapshot in initial["git_snapshots"]["workers"].values())
    assert all(worker["status"] == "not_started" and worker["seconds_remaining"] is None for worker in initial["workers"])


@pytest.mark.parametrize("arguments", ['{"version":1}', '{"version":"1"}', '{"version":1,"version":2}'])
def test_public_sdk_parser_validates_schema_and_duplicate_arguments(arguments):
    tool = {
        "type": "function",
        "function": {
            "name": "finish_planning",
            "parameters": {"type": "object", "properties": {"version": {"type": "integer"}}, "required": ["version"]},
        },
    }
    result = litellm.ModelResponse(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "one", "type": "function", "function": {"name": "finish_planning", "arguments": arguments}}],
                }
            }
        ]
    )
    if arguments == '{"version":1}':
        assert parse_completion_tool_calls(result, [tool]) == [{"name": "finish_planning", "arguments": arguments}]
    else:
        with pytest.raises(ValueError):
            parse_completion_tool_calls(result, [tool])


def test_worker_discards_inflight_action_after_frozen_plan(tmp_path):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
    from cooperagents.vendor.mini_swe.exceptions import PlanningComplete
    from test_planning import planning

    monitor = planning(tmp_path)
    monitor.planning_only = True
    with monitor._condition:
        monitor.update_notebook("Frozen notebook")
        monitor._set_final_plan_locked("forced")
    agent = DefaultAgent.__new__(DefaultAgent)
    agent.coordinator = monitor
    operations = [
        agent.step,
        agent.query,
        lambda: agent.execute_actions({"extra": {"actions": [{"tool_name": "bash", "command": "must never execute"}]}}),
    ]
    for operation in operations:
        with pytest.raises(PlanningComplete):
            operation()


@pytest.mark.parametrize("transport", ["binding", "sdk"])
@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
def test_coordinator_system_switches_after_finalization(tmp_path, monkeypatch, transport, variant):
    from types import SimpleNamespace

    import openai

    from test_planning import planning, publish_plan

    def forbidden(*args, **kwargs):
        raise AssertionError("Network is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(litellm, "completion", forbidden)
    requests = []

    def create(**kwargs):
        requests.append(kwargs["messages"])
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "", "tool_calls": []}}])

    if transport == "sdk":
        monkeypatch.setattr(
            openai, "OpenAI", lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        )
        c = planning(tmp_path, coordination_variant=variant, complete=None)
    else:
        c = planning(
            tmp_path, coordination_variant=variant, complete=None,
            completion=binding(lambda request: create(messages=request.messages)),
        )
    c.decide(initial=True)
    publish_plan(c)
    for aid in c._assignments:
        assert c.confirm_plan(aid, {"version": c._version}, sole=True)["returncode"] == 0
    c._observed_confirmations = dict(c._confirmations)
    c.finish_planning(c._version)
    c.decide(initial=True)
    assert len(requests) == 2
    for messages, system in zip(requests, (COORDINATOR_PLANNING_SYSTEM, COORDINATOR_IMPLEMENTATION_SYSTEM), strict=True):
        assert len(messages) == 2 and messages[0] == {"role": "system", "content": system}
        user = messages[1]["content"]
        assert messages[1]["role"] == "user"
        assert "run shell commands or wait for replies" in user and "execute tools" not in user
        assert "This task ends after planning" not in system + user
    assert "If no further planning change is needed" in requests[0][1]["content"]
    assert "finish_planning(version) as the sole action" in requests[0][1]["content"]
    assert "OPENING PLANNING" not in requests[1][1]["content"]
    assert "finish_planning(version)" not in requests[1][1]["content"]
