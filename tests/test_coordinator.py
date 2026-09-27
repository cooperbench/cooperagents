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
NOOP = '{"actions":[]}'


def message(text="advice", recipient="agent1"):
    return {"action": "send_message", "recipient": recipient, "content": text}


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
        coordinator()._parse_actions(json.dumps({"actions": [{"action": "update_notebook", "content": "disabled"}]}))
    previous = path.read_text()
    update = {"action": "update_notebook", "content": "new content"}
    bad_batches = [
        [message(), {**update, "path": "elsewhere"}],
        [update, update],
        [message(), message()],
        [message(recipient="other")],
        [{**update, "content": "x" * 8001}],
        [message("x" * 1201)],
        [{"action": "unknown", "content": "x"}],
        [message(" ")],
    ]
    for actions in bad_batches:
        with pytest.raises(ValueError):
            c._apply_actions(c._parse_actions(json.dumps({"actions": actions})))
        assert path.read_text() == previous and c._version == 0
        assert c.events() == [] and c._queues == {a.agent_id: [] for a in ROSTER}
    for raw in (None, "", "not json", "[]", '{"actions":[],"extra":true}', "x" * 65537):
        with pytest.raises(ValueError):
            c._parse_actions(raw)
    with monkeypatch.context() as patch:
        patch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            c._apply_actions(c._parse_actions(json.dumps({"actions": [message(), update]})))
    assert path.read_text() == previous and c._version == 0 and not c.events()
    assert list(tmp_path.iterdir()) == [path]
    c._apply_actions(c._parse_actions(json.dumps({"actions": [message(), update]})))
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


def test_concurrent_drain_and_ended_workers():
    rows = []
    c = coordinator(trace=lambda event, **data: rows.append((event, data)))
    barrier = threading.Barrier(2)
    delivered = []

    def send():
        for index in range(200):
            barrier.wait()
            c._apply_actions(c._parse_actions(json.dumps({"actions": [message(str(index))]})))
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
    c._apply_actions(c._parse_actions(json.dumps({"actions": [message("queued")]})))
    c.mark_finished("agent1", "submitted")
    c._apply_actions(c._parse_actions(json.dumps({"actions": [message("late")]})))
    assert c.drain("agent1") == [] and c._queues["agent1"] == []
    assert any(event == "worker_finished" and data["dropped"] == ["[coordinator] queued"] for event, data in rows)
    assert any(event == "message_dropped" and data["content"] == "late" for event, data in rows)


def test_failed_decisions_retain_replies_and_new_arrivals(monkeypatch):
    bus = InMemoryBus("replies")
    bus.send(sender="agent1", to="coordinator", content="proposal")
    observations = []
    outcomes = iter([ConnectionError("offline"), "invalid", NOOP, NOOP])

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


def test_finish_joins_inflight_even_if_already_stopped():
    started, release, returned = threading.Event(), threading.Event(), threading.Event()

    def complete(_):
        started.set()
        assert release.wait(3)
        return json.dumps({"actions": [message()]})

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
            actions.append({"action": "update_notebook", "content": marker + str(len(decisions))})
        return json.dumps({"actions": actions})

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
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "", "tool_calls": calls}}],
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
    monkeypatch.setattr(DefaultAgent, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("worker failure")))
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
    assert events.index("worker_finished") < events.index("agent_end")
    assert c.drain("agent1") == []
