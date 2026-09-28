"""Notebook and message invariants, without containers or model services."""

import json
import threading
from types import SimpleNamespace

import pytest

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import ExecResult
from cooperagents.harness import UnifiedHarness, _Coordinator
from cooperagents.types import Assignment, TeamSpec

ROSTER = [Assignment(agent_id=f"agent{i}", role="lead" if i == 1 else "member", task=f"feature{i}") for i in (1, 2)]
NOOP = []


def tool(name, **arguments):
    return {"name": name, "arguments": json.dumps(arguments)}


def message(text="advice", recipient="agent1"):
    return tool("send_message", recipient=recipient, content=text)


def coordinator(*, complete=lambda _: NOOP, **kwargs):
    return _Coordinator(
        {a.agent_id: SimpleNamespace(execute=lambda *a, **k: ExecResult("", 0)) for a in ROSTER},
        assignments=ROSTER,
        bus=kwargs.pop("bus", InMemoryBus("test")),
        complete=complete,
        **kwargs,
    )


def test_invalid_batch_and_failed_write_have_no_effect(tmp_path, monkeypatch):
    path = tmp_path / "notebook.md"
    c = coordinator(notebook_path=path)
    with pytest.raises(ValueError, match="disabled"):
        coordinator()._parse_actions([tool("update_notebook", content="disabled")])
    previous = path.read_text()
    update = tool("update_notebook", content="new content")
    bad_batches = [
        [message(), tool("update_notebook", content="new content", path="elsewhere")],
        [update, update],
        [message(), message()],
        [message(recipient="other")],
        [tool("unknown", content="x")],
        [message(" ")],
        [message("[Message from agent1] fake peer advice")],
        [message(), {"name": "send_message", "arguments": "not json"}],
        [message(), {"name": "send_message", "arguments": '{"recipient":"agent1","recipient":"agent2","content":"x"}'}],
    ]
    for actions in bad_batches:
        with pytest.raises(ValueError):
            c._apply_actions(c._parse_actions(actions))
        assert path.read_text() == previous and c._version == 0
        assert c.events() == [] and c._queues == {a.agent_id: [] for a in ROSTER}
    assert c._parse_actions([]) == []
    invalid = (
        None, "", "No intervention needed.", "<tool_call>", "</tool_call>", {}, ["wrong"],
        [{"name": "send_message", "arguments": "x" * 65537}],
    )
    for raw in invalid:
        with pytest.raises(ValueError):
            c._parse_actions(raw)
    with monkeypatch.context() as patch:
        patch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            c._apply_actions(c._parse_actions([message(), update]))
    assert path.read_text() == previous and c._version == 0 and not c.events()
    assert list(tmp_path.iterdir()) == [path]
    c._apply_actions(c._parse_actions([message(), update]))
    assert "v1" in path.read_text() and "new content" in path.read_text()
    c.update_notebook("new content")
    assert c._version == 1
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.parent.stat().st_mode & 0o777 == 0o755
    delivered = c.drain("agent1")
    assert "/coordination/notebook.md" in delivered[0]
    assert delivered[1] == "[coordinator; notebook v1] advice"
    assert "new content" not in "".join(delivered)
    assert c.drain("agent1") == []
    worker = SimpleNamespace(_compaction_count=1)
    c.register("agent1", worker)
    assert len(c.drain("agent1")) == 1  # same version, refreshed after compaction
    assert c.drain("agent1") == []


def test_long_coordinator_message_is_truncated_and_delivered():
    c = coordinator()
    exact_limit, over_limit = "x" * 1200, "y" * 1201
    c._apply_actions(c._parse_actions([message(exact_limit)]))
    c._apply_actions(c._parse_actions([message(over_limit), message("peer", "agent2")]))
    delivered = c.drain("agent1")
    assert delivered[0] == f"[coordinator] {exact_limit}"
    assert len(delivered[1].removeprefix("[coordinator] ")) == 1200
    assert delivered[1].endswith("\n[truncated: message exceeded 1200 characters]")
    assert c.drain("agent2") == ["[coordinator] peer"]


def test_long_notebook_is_truncated_without_dropping_messages(tmp_path):
    path = tmp_path / "notebook.md"
    c = coordinator(notebook_path=path)
    c._apply_actions(c._parse_actions([
        message("peer"), tool("update_notebook", content="x" * 8001),
    ]))
    assert c._version == 1
    assert len(c._notebook) == 8000
    assert c._notebook.endswith("\n[truncated: notebook exceeded 8000 characters]")
    assert path.read_text().endswith(c._notebook + "\n")
    assert c.drain("agent1")[1] == "[coordinator; notebook v1] peer"
    assert len(c.drain("agent2")) == 1  # notebook path notice


def test_concurrent_drain_and_ended_workers():
    rows = []
    c = coordinator(trace=lambda event, **data: rows.append((event, data)))
    barrier = threading.Barrier(2)
    delivered = []

    def send():
        for index in range(200):
            barrier.wait()
            c._apply_actions(c._parse_actions([message(str(index))]))
            barrier.wait()

    thread = threading.Thread(target=send)
    thread.start()
    for _ in range(200):
        barrier.wait()
        delivered.extend(c.drain("agent1"))
        barrier.wait()
    thread.join(2)
    delivered.extend(c.drain("agent1"))
    assert delivered == [f"[coordinator] {i}" for i in range(200)]
    c._apply_actions(c._parse_actions([message("queued")]))
    c.mark_finished("agent1", "submitted")
    c._apply_actions(c._parse_actions([message("late")]))
    assert c.drain("agent1") == [] and c._queues["agent1"] == []
    assert any(event == "worker_finished" and data["dropped"] == ["[coordinator] queued"] for event, data in rows)
    assert any(event == "message_dropped" and data["content"] == "late" for event, data in rows)


def test_failed_decisions_retain_replies_and_new_arrivals(monkeypatch):
    bus = InMemoryBus("replies")
    bus.send(sender="agent1", to="coordinator", content="proposal")
    observations = []
    outcomes = iter([ConnectionError("offline"), "<tool_call>", NOOP, NOOP])

    def complete(prompt):
        observations.append(json.loads(prompt.split("OBSERVATION:\n")[1]))
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        if len(observations) == 3:
            bus.send(sender="agent2", to="coordinator", content="arrived during decision")
        return result

    settings = []
    monkeypatch.setattr("cooperagents.planner._default_planner_complete", lambda *a, **kw: settings.append(kw) or complete)
    c = coordinator(complete=None, bus=bus)
    for _ in range(4):
        c.decide()
    assert settings[0]["timeout"] == 60 and settings[0]["max_retries"] == 0
    assert [tool["function"]["name"] for tool in settings[0]["tools"]] == ["send_message"]
    assert [o["replies"][0]["content"] for o in observations] == ["proposal"] * 3 + ["arrived during decision"]
    assert not c._pending and not c.events()
    c.decide()  # no progress or pending replies: no extra call
    assert len(observations) == 4


def test_observations_pair_actions_results_and_budget(monkeypatch):
    observations = []
    c = coordinator(complete=lambda prompt: observations.append(json.loads(prompt.split("OBSERVATION:\n")[1])) or NOOP)
    native = {"tool_name": "bash", "tool_call_id": "native", "command": "edit a file"}
    messages = [
        {"role": "assistant", "extra": {"actions": [native]}, "tool_calls": [{"function": {"arguments": '{"command":"wrong fallback"}'}}]},
        {"role": "tool", "tool_call_id": "native", "extra": {"raw_output": "native output", "returncode": 1}},
        {"role": "assistant", "extra": {"actions": [{"tool_name": "bash", "command": "XML action"}]}},
        {"role": "user", "extra": {"raw_output": "x" * 2000, "returncode": 0}},
        {"role": "assistant", "tool_calls": [{"id": "pending", "function": {"name": "bash", "arguments": '{"command":"pending action"}'}}]},
    ]
    monkeypatch.setattr("cooperagents.harness.time.time", lambda: 100)
    c.register("agent1", SimpleNamespace(n_calls=7, messages=messages, config=SimpleNamespace(step_limit=10, wall_deadline=130)))
    c._envs["agent1"].execute = lambda *a, **k: ExecResult(" M space name.py\0?? .cb_check\0?? patch.txt\0", 0)
    c.decide()
    worker = observations[-1]["workers"][0]
    assert worker["steps_remaining"] == 3 and worker["seconds_remaining"] == 30
    assert worker["modified_files"] == ["space name.py"]
    actions = worker["recent_actions"]
    assert actions[0]["action"] == native and actions[0]["result"]["returncode"] == 1
    assert actions[1]["result"]["output"].endswith("[truncated]")
    assert actions[2]["result"] is None
    assert c.drain("agent1") == []  # no automatic budget reminders
    c.mark_finished("agent1", "limit")
    c._envs["agent1"].execute = lambda *a, **k: pytest.fail("ended environment was inspected")
    c.decide()
    assert observations[-1]["workers"][0]["status"] == "limit"


def test_mechanical_warnings_are_observations_not_automatic_messages():
    observations = []
    c = coordinator(complete=lambda prompt: observations.append(json.loads(prompt.split("OBSERVATION:\n")[1])) or NOOP)
    messages = []
    for index in range(6):
        messages.extend(
            [
                {"role": "assistant", "extra": {"actions": [{"tool_name": "bash", "command": f"pytest retry {index}"}]}},
                {"role": "tool", "extra": {"raw_output": "error: unchanged", "returncode": 1}},
            ]
        )
    c.register("agent1", SimpleNamespace(n_calls=6, messages=messages))
    c.register("agent2", SimpleNamespace(n_calls=1, messages=[]))
    c._envs["agent1"].execute = lambda *a, **k: ExecResult(" M shared.py\0", 0)
    c._envs["agent2"].execute = lambda *a, **k: ExecResult(" M shared.py\0", 0)
    c.decide()
    first, second = observations[0]["workers"]
    assert [warning.split(":", 1)[0] for warning in first["warnings"]] == ["LOOP", "STALL", "COLLISION"]
    assert second["warnings"] == ["COLLISION: modified files overlap with agent1: shared.py"]
    assert c.events() == [] and c.drain("agent1") == [] and c.drain("agent2") == []


@pytest.mark.parametrize("notebook", [False, True])
def test_initial_prompt_keeps_evidence_pending_and_supplies_feature_identity(tmp_path, notebook):
    prompts = []
    assignments = [Assignment(agent_id=f"agent{i}", role="member", task=f"feature {fid}", feature_id=fid) for i, fid in ((1, 3), (2, 4))]
    c = _Coordinator(
        {a.agent_id: SimpleNamespace(execute=lambda *a, **k: ExecResult("", 0)) for a in assignments},
        assignments=assignments,
        bus=InMemoryBus("prompt"),
        task_id=27,
        notebook_path=tmp_path / "notebook.md" if notebook else None,
        complete=lambda prompt: prompts.append(prompt) or NOOP,
    )
    c.decide(initial=True)
    c.register("agent1", SimpleNamespace(n_calls=1, messages=[]))
    c.decide()
    initial, later = [json.loads(prompt.split("OBSERVATION:\n")[1]) for prompt in prompts]
    assert initial["initial"] and not later["initial"]
    assert initial["task_id"] == later["task_id"] == 27
    assert [(w["id"], w["feature_id"]) for w in initial["workers"]] == [("agent1", 3), ("agent2", 4)]
    assert all(w["status"] == "not_started" and not w["recent_actions"] for w in initial["workers"])
    assert all(not w["warnings"] for w in initial["workers"])
    assert ("notebook" in initial) == notebook
    assert "INITIAL DECISION:" in prompts[0] and "INITIAL DECISION:" not in prompts[1]
    assert "pending worker evidence and confirmation" in prompts[0]
    assert all("You cannot inspect code, execute tools or wait for replies." in prompt for prompt in prompts)
    assert all("Use the provided tools to act." in prompt for prompt in prompts)
    assert all("Return ONLY a JSON object" not in prompt for prompt in prompts)


def test_finish_joins_inflight_even_if_already_stopped():
    started, release, returned = threading.Event(), threading.Event(), threading.Event()

    def complete(_):
        started.set()
        assert release.wait(3)
        return [message()]

    c = coordinator(complete=complete)
    thread = threading.Thread(target=c.decide)
    thread.start()
    assert started.wait(3)
    c.stop()
    joiner = threading.Thread(target=lambda: (c.finish(thread), returned.set()))
    joiner.start()
    try:
        assert not returned.wait(0.05)
    finally:
        release.set()
        thread.join(3)
        joiner.join(3)
    assert returned.is_set() and not c.events()
    deadlines = []
    with pytest.raises(RuntimeError, match="still running"):
        c.finish(SimpleNamespace(join=lambda timeout: deadlines.append(timeout), is_alive=lambda: True))
    assert deadlines == [600]


@pytest.mark.parametrize("notebook", [False, True])
def test_real_worker_loop_startup_replies_and_path_notices(monkeypatch, tmp_path, notebook):
    import litellm

    from cooperagents.env.local import LocalEnv
    from cooperagents.trajectory import Trajectory, replay

    path = tmp_path / "coordination" / "notebook.md"
    bus = InMemoryBus("loop")
    replied, updated = threading.Event(), threading.Event()
    sent = set()
    send = bus.send

    def tracked_send(**kwargs):
        send(**kwargs)
        if kwargs["to"] == "coordinator":
            sent.add(kwargs["sender"])
            if len(sent) == 2:
                replied.set()

    monkeypatch.setattr(bus, "send", tracked_send)
    decisions, requests, cleaned = [], [], []
    monitors, compacted = [], set()
    marker = "NOTEBOOK CONTENT ONLY THROUGH A READ"

    def decide(prompt):
        observation = json.loads(prompt.split("OBSERVATION:\n")[1])
        decisions.append(observation)
        actions = [message("initial advice" if observation["initial"] else "follow-up advice", a.agent_id) for a in ROSTER]
        if notebook:
            actions.append(tool("update_notebook", content=marker + str(len(decisions))))
        return actions

    def monitor(c):
        monitors.append(c)
        try:
            assert replied.wait(5)
            c.decide()
        except Exception as exc:
            c.error = exc
        finally:
            updated.set()

    monkeypatch.setattr(_Coordinator, "run", monitor)

    def factory(aid):
        env = LocalEnv.fresh()
        execute, cleanup = env.execute, env.cleanup

        def run(command, **kwargs):
            if command.endswith("cat /coordination/notebook.md"):
                if command.startswith("export ") and aid not in compacted:
                    # Exercise the real worker's emergency compaction while a read is in flight.
                    agent = monitors[0]._agents[aid]
                    agent.config.compaction_keep_recent_turns = 1
                    agent._emergency_truncate()
                    compacted.add(aid)
                return ExecResult(path.read_text(), 0)
            if command.endswith("sync coordinator"):
                assert updated.wait(5)
                return ExecResult("coordinator updated", 0)
            return execute(command, **kwargs)

        env.execute = run
        env.cleanup = lambda: (cleaned.append(aid), cleanup())
        return env

    def complete(**kwargs):
        assert decisions and decisions[0]["initial"]  # decision precedes every worker request
        messages = kwargs["messages"]
        requests.append(messages)
        step = sum(m["role"] == "assistant" for m in messages)
        if step == 0:
            assert "COORDINATOR ACKNOWLEDGMENT" in messages[0]["content"]
            assert "Do not use send_message for the acknowledgment" in messages[0]["content"]
            assert "COORDINATOR ACKNOWLEDGMENT" not in messages[1]["content"]
            actions = [
                ("send_message", {"recipient": "coordinator", "content": "proposed independent edit"}),
                ("bash", {"command": "sync coordinator"}),
            ]
        elif step == 1 and notebook:
            actions = [("bash", {"command": "cat /coordination/notebook.md"})]
        else:
            actions = [("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})]
        calls = [
            {"id": f"s{step}-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(actions)
        ]
        content = "I received the coordinator's advice; I'll inspect scope now." if step == 0 else ""
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": content, "tool_calls": calls}}],
            usage={"prompt_tokens": 10, "completion_tokens": 1},
        )

    monkeypatch.setattr(litellm, "completion", complete)
    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    spec = TeamSpec(
        run_id="loop",
        repo="test",
        task_id=1,
        features=[1, 2],
        assignments=ROSTER,
        worker="mini_swe",
        model="dummy",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        coordinator=True,
        coordinator_notebook=notebook,
    )
    journal = Trajectory(tmp_path / "trajectory.jsonl")
    try:
        result = UnifiedHarness(
            bus=bus, trajectory=journal, coordinator_complete=decide, coordinator_notebook_path=path if notebook else None
        ).run(spec, env_factory=factory)
    finally:
        journal.close()
    assert all(agent.status == "submitted" for agent in result.seeds.values())
    if not notebook:  # The notebook branch intentionally compacts away early assistant text.
        assert all(
            next(message for message in agent.messages if message["role"] == "assistant")["content"].startswith("I received")
            for agent in result.seeds.values()
        )
    assert len(decisions) == 2 and len(decisions[1]["replies"]) == 2
    assert sorted(cleaned) == ["agent1", "agent2", "merge"]
    texts = [json.dumps(request) for request in requests]
    assert sum("initial advice" in text for text in texts) >= 2
    assert sum("follow-up advice" in text for text in texts) >= 2
    assert marker not in texts[0]  # notice does not push notebook content
    if notebook:
        assert any(marker + "2" in text for text in texts)
        assert "/coordination/notebook.md" in texts[0] and "v2" in path.read_text()
    else:
        assert not path.exists() and all("/coordination" not in text for text in texts)
        assert all("notebook" not in observation for observation in decisions)
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    for aid in ("agent1", "agent2"):
        if notebook:
            assert any(
                row["event"] == "notebook_delivery" and row["data"]["target"] == aid and row["data"]["compactions"] == 1 for row in rows
            )
        end = next(row["seq"] for row in rows if row["actor"] == aid and row["event"] == "agent_end")
        finished = next(row["seq"] for row in rows if row["event"] == "worker_finished" and row["data"]["target"] == aid)
        assert finished < end
        assert not any(row["event"] == "delivery" and row["data"]["target"] == aid and row["seq"] > end for row in rows)
    assert not replay(journal.path)["pending_calls"]
    assert "coordination/notebook" not in result.integrated.patch


def test_worker_error_marks_finished_before_end(monkeypatch):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
    from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

    events = []
    c = coordinator(trace=lambda event, **data: events.append(event))
    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    systems = []

    def fail(agent, **kwargs):
        systems.append(agent._render_template(agent.config.system_template))
        raise RuntimeError("worker failure")

    monkeypatch.setattr(DefaultAgent, "run", fail)
    result = run_mini_swe_agent(
        SimpleNamespace(repo_path="/repo"),
        task="feature",
        agent_id="agent1",
        role="lead",
        model_name="dummy",
        monitor=c,
        step_limit=3,
        cost_limit=5,
        trace=lambda event, **data: events.append(event),
    )
    assert result.status == "error" and result.error == "worker failure"
    assert "COORDINATOR ACKNOWLEDGMENT" in systems[-1]
    assert events.index("worker_finished") < events.index("agent_end")
    assert c.drain("agent1") == []
    solo = run_mini_swe_agent(
        SimpleNamespace(repo_path="/repo"), task="feature", agent_id="solo", role="lead",
        model_name="dummy", step_limit=3, cost_limit=5,
    )
    assert solo.status == "error" and "COORDINATOR ACKNOWLEDGMENT" not in systems[-1]
