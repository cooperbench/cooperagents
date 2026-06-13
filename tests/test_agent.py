"""The unified agent loop."""

from __future__ import annotations

from cooperagents.agent import Agent
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.local import LocalEnv
from cooperagents.llm import Action, ScriptedLLM


def _agent(actions, *, role="member", allow_spawn=False, **kw):
    env = LocalEnv.fresh()
    bus = InMemoryBus("t")
    return Agent(
        agent_id="agent1",
        role=role,
        task="do the thing",
        env=env,
        llm=ScriptedLLM({"*": actions}),
        bus=bus,
        allow_spawn=allow_spawn,
        **kw,
    )


def test_write_file_produces_patch():
    a = _agent(
        [
            Action(tool="write_file", args={"path": "hello.txt", "content": "hi\n"}),
            Action(tool="finish"),
        ]
    )
    res = a.run()
    assert res.status == "submitted"
    assert "hello.txt" in res.patch
    assert res.steps == 2


def test_bash_observation_recorded():
    a = _agent(
        [
            Action(tool="bash", args={"command": "echo marker123"}),
            Action(tool="finish"),
        ]
    )
    res = a.run()
    assert any("marker123" in m["content"] for m in res.messages)


def test_step_limit_enforced():
    # Never finishes; step_limit caps it.
    a = _agent([Action(tool="bash", args={"command": "true"})] * 100, step_limit=3)
    res = a.run()
    assert res.status == "limit"
    assert res.steps == 3


def test_cost_limit_enforced():
    a = _agent([Action(tool="bash", args={"command": "true"}, cost=0.5)] * 100, cost_limit=1.0)
    res = a.run()
    assert res.status == "limit"


def test_spawn_tool_gated_off_by_default():
    a = _agent([Action(tool="spawn_helper", args={"task": "x"}), Action(tool="finish")], allow_spawn=False)
    res = a.run()
    # spawn_helper is not in the catalog, so it's reported unknown and no request is enqueued.
    assert a.spawn_requests == []
    assert any("unknown or disallowed" in m["content"] for m in res.messages)


def test_spawn_tool_enqueues_when_allowed():
    a = _agent([Action(tool="spawn_helper", args={"task": "x"}), Action(tool="finish")], allow_spawn=True)
    res = a.run()
    assert len(a.spawn_requests) == 1
    assert res.status == "submitted"


def test_incoming_message_injected():
    env = LocalEnv.fresh()
    bus = InMemoryBus("t")
    bus.register_agent("agent1", role="member")
    bus.send(sender="lead", to="agent1", content="please add tests")
    a = Agent(
        agent_id="agent1",
        role="member",
        task="work",
        env=env,
        llm=ScriptedLLM({"*": [Action(tool="finish")]}),
        bus=bus,
    )
    res = a.run()
    assert any("please add tests" in m["content"] for m in res.messages)


def test_team_tools_act_on_bus():
    env = LocalEnv.fresh()
    bus = InMemoryBus("t")
    a = Agent(
        agent_id="lead",
        role="lead",
        task="organize",
        env=env,
        llm=ScriptedLLM(
            {
                "*": [
                    Action(tool="task_create", args={"title": "subtask A"}),
                    Action(tool="finish"),
                ]
            }
        ),
        bus=bus,
    )
    a.run()
    assert bus.list_tasks()[0]["title"] == "subtask A"
