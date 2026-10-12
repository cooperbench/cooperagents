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


def test_coordination_variant_names(tmp_path):
    assert UnifiedHarness().coordination_variant == "auto-prompt"
    assert coordinator().coordination_variant == "auto-prompt"
    for variant in ("auto-prompt", "human-prompt"):
        assert UnifiedHarness(coordination_variant=variant).coordination_variant == variant
        c = coordinator(notebook_path=tmp_path / variant / "notebook.md", coordination_variant=variant)
        assert c.coordination_variant == variant
    for old_name in ("current", "human_in_loop"):
        with pytest.raises(ValueError, match="Unknown coordination variant"):
            UnifiedHarness(coordination_variant=old_name)
        with pytest.raises(ValueError, match="Unknown coordination variant"):
            coordinator(coordination_variant=old_name)


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
    assert actions[1]["result"]["output"] == "x" * 300 + "\n[truncated]\n" + "x" * 700
    assert actions[1]["result"]["output_chars"] == 2000
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
    assert [warning.split(":", 1)[0] for warning in first["warnings"]] == ["STALL", "POTENTIAL_CONFLICT"]
    assert second["warnings"] == [
        'POTENTIAL_CONFLICT: same file modified; regions unknown with agent1: "shared.py" (source=current path observation)'
    ]
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
    assert "collected_at" not in initial and "collected_at" not in later
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


def test_human_prompt_initial_prompt_and_direct_message_wrapper(tmp_path):
    prompts = []
    c = coordinator(
        notebook_path=tmp_path / "notebook.md",
        coordination_variant="human-prompt",
        complete=lambda prompt: prompts.append(prompt) or NOOP,
    )
    c.decide(initial=True)
    expected_initial = (
        'You coordinate software workers implementing features in separate copies of one repository. Their '
        'patches will later be merged. Make one coordination decision from the current OBSERVATION.\n'
        '\n'
        '## 1. Evidence boundary\n'
        '\n'
        'You cannot inspect code, execute tools, or wait for replies. Use only facts supported by the '
        'observation and worker reports.\n'
        '\n'
        'Task requirements describe requested behavior, not inspected-code facts. Do not invent files, '
        'symbols, interfaces, agreement, verification, completed tests, or remaining budgets. Missing '
        'evidence is unknown; ask workers to inspect when needed.\n'
        '\n'
        'Use task_id and worker id/feature_id exactly as supplied.\n'
        '\n'
        '## 2. When to coordinate\n'
        '\n'
        'Keep implementation moving with minimal coordination overhead.\n'
        '\n'
        'Intervene when the observation shows a concrete shared dependency, contested ownership/interface, '
        'implementation overlap, repeated failure, handoff need, or verification need. Same-file edits are '
        'only potential overlap; inspect the underlying observed evidence before acting.\n'
        '\n'
        'After the initial decision, take no action when there is no useful new coordination advice.\n'
        '\n'
        '## 3. Ownership and shared interfaces\n'
        '\n'
        'For a shared definition or interface, propose one worker as the writer and have affected workers '
        'confirm the ownership and consumer-facing interface before conflicting shared edits proceed. Do not '
        'assign multiple workers to implement the same shared definition.\n'
        '\n'
        'Hold only disputed shared edits while clarification is pending; independent work should continue.\n'
        '\n'
        'Do not invent an API or concrete edit boundary merely to make the plan specific. Preserve '
        'conflicting reports as unresolved until clarified.\n'
        '\n'
        '## 4. Messages and notebook\n'
        '\n'
        'Use tool calls rather than describing intended actions.\n'
        '\n'
        'Per decision, send at most one message to each worker, under 500 characters. Center it on one '
        'coordination issue: state the relevant observed fact or uncertainty, then give one concrete next '
        'action or confirmation question.\n'
        '\n'
        'Use the notebook to preserve shared state across decisions: responsibilities/regions, shared '
        'interfaces/dependencies, unresolved issues, verification state, and handoffs.\n'
        '\n'
        'Distinguish proposed, worker-reported, confirmed, and verified state. Confirmation requires explicit '
        'agreement where shared ownership or interfaces are contested; verification requires an observed '
        'scoped check.\n'
        '\n'
        'Update the notebook only when this shared state is established or materially changes. Do not finish '
        'with only a notebook update.\n'
        '\n'
        '## 5. Initial decision\n'
        '\n'
        'Workers have not yet inspected the code. Request focused inspection and reports of proposed '
        'files/regions, shared interfaces, and dependencies.\n'
        '\n'
        'You may suggest provisional task-level responsibilities from the feature requirements, but concrete '
        'file, symbol, region, and interface ownership remains pending inspection and confirmation.\n'
        '\n'
        'Allow independent work to continue while shared decisions are unresolved.\n'
        '\n'
        'OBSERVATION:\n'
    )
    from cooperagents.coordination_prompts import COORDINATOR_WARNING_RULES

    expected_initial = expected_initial.replace("\nOBSERVATION:\n", "\n" + COORDINATOR_WARNING_RULES + "\nOBSERVATION:\n")
    assert prompts[0].startswith(expected_initial)
    assert json.loads(prompts[0][len(expected_initial):])["initial"] is True
    c.register("agent1", SimpleNamespace(n_calls=1, messages=[]))
    c.decide()
    expected_later = expected_initial.split("## 5. Initial decision\n")[0] + COORDINATOR_WARNING_RULES + "\nOBSERVATION:\n"
    assert prompts[1].startswith(expected_later)
    assert json.loads(prompts[1][len(expected_later):])["initial"] is False
    c._apply_actions(c._parse_actions([message("propose scope")]))
    assert c.drain("agent1")[1] == (
        "[COORDINATION NOTEBOOK]\nVersion: 0\n"
        "Read /coordination/notebook.md if needed.\n\n"
        "[FROM COORDINATOR]\npropose scope\n[END COORDINATOR MESSAGE]"
    )
    with pytest.raises(ValueError, match="requires a notebook"):
        coordinator(coordination_variant="human-prompt")


def test_human_prompt_notebook_update_and_message_combinations(tmp_path):
    c = coordinator(notebook_path=tmp_path / "notebook.md", coordination_variant="human-prompt")
    assert len(c.drain("agent1")) == 1  # initial notebook notice
    assert c.drain("agent1") == []  # no update, no direct message

    c._apply_actions(c._parse_actions([message("ordinary advice")]))
    assert c.drain("agent1") == [
        "[COORDINATION NOTEBOOK]\nVersion: 0\n"
        "Read /coordination/notebook.md if needed.\n\n"
        "[FROM COORDINATOR]\nordinary advice\n[END COORDINATOR MESSAGE]"
    ]

    c._apply_actions(c._parse_actions([tool("update_notebook", content="revision one")]))
    assert c.drain("agent1") == [
        "[coordinator] Notebook v1 is available. Read the latest file at "
        "/coordination/notebook.md (`cat /coordination/notebook.md`) before continuing affected work."
    ]

    c._apply_actions(c._parse_actions([message("new plan"), tool("update_notebook", content="revision two")]))
    assert c.drain("agent1") == [
        "[coordinator] Notebook v2 is available. Read the latest file at "
        "/coordination/notebook.md (`cat /coordination/notebook.md`) before continuing affected work.",
        "[COORDINATION NOTEBOOK]\nVersion: 2\n"
        "Notebook updated! Read /coordination/notebook.md before continuing affected work.\n\n"
        "[FROM COORDINATOR]\nnew plan\n[END COORDINATOR MESSAGE]",
    ]

    c._apply_actions(c._parse_actions([tool("update_notebook", content="revision two"), message("same version")]))
    assert c.drain("agent1") == [
        "[COORDINATION NOTEBOOK]\nVersion: 2\n"
        "Read /coordination/notebook.md if needed.\n\n"
        "[FROM COORDINATOR]\nsame version\n[END COORDINATOR MESSAGE]"
    ]
    c._apply_actions(c._parse_actions([]))
    assert c.drain("agent1") == []


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


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("notebook", [False, True])
def test_real_worker_loop_startup_replies_and_path_notices(monkeypatch, tmp_path, notebook, remote):
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
    if remote:
        from test_observability import memory_trace

        sink, exporter = memory_trace(journal)
        monkeypatch.setattr("cooperagents.observability.LangfuseTrace", lambda *a: sink)
    try:
        result = UnifiedHarness(
            bus=bus, trajectory=journal, langfuse=remote, coordinator_complete=decide, coordinator_notebook_path=path if notebook else None
        ).run(spec, env_factory=factory)
    finally:
        journal.close()
    if remote:
        assert sink._closed and not sink._calls
        roots = {s.name for s in exporter.get_finished_spans() if s.attributes.get("langfuse.internal.as_root")}
        assert {"agent1", "agent2", "coordinator"} <= roots
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


def test_worker_system_variant_keeps_reviewed_shared_prefix(monkeypatch, tmp_path):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
    from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    systems = []

    def capture(agent, **kwargs):
        systems.append(agent._render_template(agent.config.system_template))
        raise RuntimeError("captured")

    monkeypatch.setattr(DefaultAgent, "run", capture)
    for variant in ("auto-prompt", "human-prompt"):
        c = coordinator(notebook_path=tmp_path / variant / "notebook.md", coordination_variant=variant)
        result = run_mini_swe_agent(
            SimpleNamespace(repo_path="/repo"), task="feature", agent_id="agent1", role="lead",
            model_name="dummy", step_limit=3, cost_limit=5, comm=SimpleNamespace(), monitor=c, git_share=True,
        )
        assert result.status == "error"
        system = systems[-1]
        assert system.index("A shared git remote") < system.index("You also have a second tool")
        assert system.index("You also have a second tool") < system.index("The coordinator is also")
        if variant == "auto-prompt":
            assert "COORDINATOR ACKNOWLEDGMENT" in system
        else:
            assert "COORDINATOR NOTICES" in system and "at most two focused inspections" not in system
            assert "A focused code inspection" in system


@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
def test_group_canonical_pages_failed_decision_and_restore(tmp_path, variant):
    c = coordinator(coordination_mode="group-chat", coordination_variant=variant, notebook_path=tmp_path / "notebook.md")
    threads = [
        threading.Thread(target=lambda aid=aid: [c.publish(aid, "group", "x" * 1201) for _ in range(10)]) for aid in ("agent1", "agent2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert [row["message_id"] for row in c._chat] == list(range(1, 21))
    received = {aid: [] for aid in ("agent1", "agent2")}
    for aid in received:
        while c._chat_positions[aid] < 20:
            received[aid].extend(text for text in c.drain(aid) if text.startswith("[group"))
    assert received["agent1"] == received["agent2"]

    def fail(prompt):
        c.publish("agent1", "coordinator", "arrived during call")
        raise RuntimeError("offline failure")

    c._complete = fail
    with pytest.raises(RuntimeError):
        c.decide()
    assert c._chat_positions["coordinator"] == 0
    seen = []
    c._complete = lambda prompt: seen.append(json.loads(prompt.split("OBSERVATION:\n")[1])) or []
    c.decide()
    assert c._chat_positions["coordinator"] == 6  # canonical complete-message page
    assert len(seen[0]["group_chat"]["unread"]) == 6
    c._apply_actions(c._parse_actions([message("public point", "agent1")]))
    assert any("public point" in line for line in c.drain("agent2"))
    c.mark_finished("agent1", "submitted")
    assert c.drain("agent1") == []
    restored = []
    agent = SimpleNamespace(add_messages=lambda m: restored.append(m), model=SimpleNamespace(format_message=lambda **kwargs: kwargs))
    c.restore_context("agent2", agent)
    assert "group #" in restored[0]["content"]


def test_group_inflight_stop_drops_returned_messages(tmp_path):
    c = coordinator(coordination_mode="group-chat", notebook_path=tmp_path / "notebook.md")
    entered, release = threading.Event(), threading.Event()

    def complete(_):
        entered.set()
        assert release.wait(2)
        return [message("late public advice", "group")]

    c._complete = complete
    errors = []

    def decide():
        try:
            c.decide(initial=True)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=decide)
    thread.start()
    assert entered.wait(2)
    c.mark_finished("agent1", "submitted")
    c.mark_finished("agent2", "submitted")
    release.set()
    thread.join(2)
    assert not errors and not c._chat


@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
def test_real_group_loop_public_messages_in_actual_requests(tmp_path, monkeypatch, variant):
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial

    import litellm

    from cooperagents.env.local import LocalEnv
    from cooperagents.trajectory import Trajectory, replay
    from cooperagents.workers.mini_swe_worker import BusComm, run_mini_swe_agent

    c = coordinator(coordination_mode="group-chat", coordination_variant=variant, notebook_path=tmp_path / "notebook.md")
    journal = Trajectory(tmp_path / "group-trajectory.jsonl")
    c.trace = partial(journal.emit, "coordinator")
    c._apply_actions(c._parse_actions([message("Public targeted advice", "agent1")]))
    barrier = threading.Barrier(2)
    requests = []

    def completion(**kwargs):
        messages = kwargs["messages"]
        requests.append(json.loads(json.dumps(messages)))
        aid = messages[1]["content"].split("TASK ID ")[1].split()[0]
        if not any(m["role"] == "assistant" for m in messages):
            actions = [
                ("send_message", {"recipient": "group", "content": f"{aid} inspected its code"}),
                ("bash", {"command": "sync offline workers"}),
            ]
        else:
            assert all(f"{actor} inspected its code" in json.dumps(messages) for actor in ("agent1", "agent2"))
            actions = [("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})]
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "synthetic",
                        "tool_calls": [
                            {"id": f"{aid}-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                            for i, (name, args) in enumerate(actions)
                        ],
                    }
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 1},
        )

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setenv("OPENAI_API_KEY", "offline")

    def worker(aid):
        env = LocalEnv.fresh()
        execute = env.execute

        def command(command, **kwargs):
            if command.endswith("sync offline workers"):
                barrier.wait(timeout=2)
                return ExecResult("synchronized", 0)
            return execute(command, **kwargs)

        env.execute = command
        try:
            return run_mini_swe_agent(
                env,
                task=f"TASK ID {aid}",
                agent_id=aid,
                role="member",
                model_name="dummy",
                step_limit=10,
                cost_limit=5,
                comm=BusComm(c._bus, aid, group=c),
                monitor=c,
                poller=SimpleNamespace(poll=lambda: "\n".join(c.drain(aid))),
                trace=partial(journal.emit, aid),
            )
        finally:
            env.cleanup()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(worker, ("agent1", "agent2")))
    assert all(r.status == "submitted" for r in result), [(r.status, r.error) for r in result]
    assert all("Public targeted advice" in json.dumps(req) for req in requests)
    assert len(c._chat) == 3
    journal.close()
    assert not replay(journal.path)["pending_calls"]
    from test_coordination_experiment import script

    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    receipt = script("audit_trajectories").audit_coordination(rows, {"metrics": c.planning_metrics()})
    assert receipt["public_records_in_requests"] == {"agent1": 3, "agent2": 3}
