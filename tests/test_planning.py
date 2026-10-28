"""Opening planning is a prompt contract; host guarantees version and delivery ordering."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from cooperagents.env.base import ExecResult
from cooperagents.harness import UnifiedHarness
from cooperagents.types import Assignment, TeamSpec
from test_coordinator import coordinator, message, tool


def agent():
    messages = []
    return SimpleNamespace(
        messages=messages,
        n_calls=0,
        cost=0,
        _compaction_count=0,
        config=SimpleNamespace(step_limit=1000, cost_limit=5, wall_deadline=time.time() + 3600),
        model=SimpleNamespace(format_message=lambda **kwargs: kwargs),
        add_messages=lambda m: messages.append(m),
    )


def planning(tmp_path, **kwargs):
    c = coordinator(coordination_mode="group-chat-planning", notebook_path=tmp_path / "notebook.md", **kwargs)
    c.start_planning()
    for aid in c._assignments:
        c.register(aid, agent())
    return c


def publish_plan(c):
    for aid in c._assignments:
        c.publish(aid, "group", f"Inspected source for {aid}; propose independent region.")
    c.update_notebook("agent1: a.py; agent2: b.py. APIs: none shared. No dependencies. Verify imports.")
    for aid in c._assignments:
        c.before_query(aid, c._agents[aid])


def test_confirmation_revision_and_mixed_batch(tmp_path):
    c = planning(tmp_path)
    assert c.confirm_plan("agent1", {"version": 0}, sole=True)["returncode"] == 1
    publish_plan(c)
    v = c._version
    assert c.confirm_plan("agent1", {"version": v}, sole=False)["returncode"] == 1
    assert c.confirm_plan("agent1", {"version": v - 1}, sole=True)["returncode"] == 1
    for aid in c._assignments:
        assert c.confirm_plan(aid, {"version": v}, sole=True)["returncode"] == 0
    c.publish("agent1", "group", "New API question")
    assert c._confirmations == {"agent2": v}
    c.update_notebook(c._notebook + " Additional check.")
    assert not c._confirmations
    assert c.confirm_plan("agent2", {"version": v}, sole=True)["returncode"] == 1
    with pytest.raises(ValueError):
        c.finish_planning(c._version)
    assert c.phase == "planning"


def test_discussion_revocation_notice_is_private_and_once(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    for aid in c._assignments:
        c.drain(aid)
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    messages_before = len(c._chat)
    c.publish("agent1", "group", "New API question")
    assert c._confirmations == {"agent2": c._version}
    assert len(c._chat) == messages_before + 1
    c.before_query("agent1", c._agents["agent1"])
    notices = [m["content"] for m in c._agents["agent1"].messages if m["content"].startswith("[harness]")]
    assert len(notices) == 1
    assert f"confirmation of notebook v{c._version} was cleared" in notices[0]
    assert "new discussion message" in notices[0] and "confirm_plan" in notices[0]
    assert "[harness]" not in "\n".join(c.drain("agent2"))
    c.publish("agent1", "group", "More details while still discussing")
    c.before_query("agent1", c._agents["agent1"])
    assert sum(m["content"].startswith("[harness]") for m in c._agents["agent1"].messages) == 1
    assert c.confirm_plan("agent1", {"version": c._version}, sole=True)["returncode"] == 0
    c._complete = lambda _: [tool("finish_planning", version=c._version)]
    c.decide()
    assert c.phase == "implementation"


def test_notebook_revision_notice_explains_reconfirmation(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    for aid in c._assignments:
        c.drain(aid)
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    c.update_notebook(c._notebook)
    assert len(c._confirmations) == 2
    assert all(not c.drain(aid) for aid in c._assignments)
    c.update_notebook(c._notebook + " Revised shared API.")
    assert not c._confirmations
    for aid in c._assignments:
        delivered = c.drain(aid)
        assert len(delivered) == 1
        assert f"Notebook v{c._version}" in delivered[0]
        assert "confirmation of an earlier version is invalid" in delivered[0]
        assert "confirm_plan" in delivered[0]
        assert not c.drain(aid)
        c.before_query(aid, c._agents[aid])
        assert c.confirm_plan(aid, {"version": c._version}, sole=True)["returncode"] == 0


@pytest.mark.parametrize("state", ["failed", "finished"])
def test_finish_planning_rejects_unavailable_state(tmp_path, state):
    c = planning(tmp_path)
    publish_plan(c)
    if state == "failed":
        c.stop()
    else:
        c._finished["agent1"] = "submitted"
    with pytest.raises(ValueError, match="unavailable in phase"):
        c.finish_planning(c._version)
    assert c._final_plan is None


@pytest.mark.parametrize("stale", ["request", "candidate"])
def test_finish_planning_reports_version_mismatch(tmp_path, stale):
    c = planning(tmp_path)
    publish_plan(c)
    version = c._version - 1 if stale == "request" else c._version
    if stale == "candidate":
        c._candidate_version = None
    with pytest.raises(ValueError, match="no candidate" if stale == "candidate" else "version mismatch") as error:
        c.finish_planning(version)
    if stale == "request":
        assert f"Requested: {version}" in str(error.value)
        assert f"Current candidate: {c._candidate_version}" in str(error.value)
    assert "missing" not in str(error.value)
    assert c.phase == "planning"


@pytest.mark.parametrize("invalid", ["missing", "wrong-version", "unexpected-worker"])
def test_finish_planning_reports_invalid_confirmations(tmp_path, invalid):
    c = planning(tmp_path)
    publish_plan(c)
    for aid in c._assignments:
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    if invalid == "missing":
        del c._confirmations["agent2"]
    elif invalid == "wrong-version":
        c._confirmations["agent2"] -= 1
    else:
        c._confirmations["unexpected"] = c._version
    expected_missing = [] if invalid == "unexpected-worker" else ["agent2"]
    with pytest.raises(ValueError, match="missing valid confirmations") as error:
        c.finish_planning(c._version)
    assert f"Missing or invalid: {expected_missing}" in str(error.value)
    assert f"Current confirmations: {c._confirmations}" in str(error.value)
    assert c.phase == "planning"


def test_missing_confirmation_error_reaches_next_coordinator_observation(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    c.confirm_plan("agent1", {"version": c._version}, sole=True)
    c._complete = lambda _: [tool("finish_planning", version=c._version)]
    c.decide(initial=True)
    assert "Missing or invalid: ['agent2']" in c._last_error
    assert c.phase == "planning"
    seen = []
    c._complete = lambda prompt: seen.append(json.loads(prompt.split("OBSERVATION:\n")[1])) or []
    c.decide(initial=True)
    assert "Missing or invalid: ['agent2']" in seen[0]["previous_error"]
    assert seen[0]["planning"]["confirmations"] == {"agent1": c._version}


@pytest.mark.parametrize("reason", ["step_limit", "time_limit"])
def test_forced_finalization_exact_limit(tmp_path, monkeypatch, reason):
    c = planning(tmp_path)
    if reason == "step_limit":
        c._planning_steps["agent1"] = 200
    else:
        monkeypatch.setattr("cooperagents.harness.time.time", lambda: c._planning_deadline)
    seen = []

    def complete(prompt):
        seen.append(json.loads(prompt.split("OBSERVATION:\n")[1]))
        return [tool("update_notebook", content="Final decision: agent1 a.py; agent2 b.py; no shared API; verify imports.")]

    c._complete = complete
    c.decide()
    assert c.phase == "implementation" and c._planning_outcome == "forced"
    assert c._planning_reason == reason and len(seen) == 1
    assert len(seen[0]["workers"]) == 2 and "frozen_evidence" not in seen[0]
    assert not c._confirmations
    assert c.confirm_plan("agent1", {"version": c._version}, sole=True)["returncode"] == 1


def test_confirmed_worker_wakes_for_public_message(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    c.confirm_plan("agent1", {"version": c._version}, sole=True)
    # Consume the first reports before testing no-new-information wait.
    c._requests["agent1"]["chat_position"] = len(c._chat)
    c._chat_positions["agent1"] = len(c._chat)
    done = threading.Event()
    thread = threading.Thread(target=lambda: (c.before_query("agent1", c._agents["agent1"]), done.set()))
    thread.start()
    assert not done.wait(0.05)
    c.publish("agent2", "agent1", "Please reconsider signature.")
    assert done.wait(2)
    thread.join(2)
    assert "Please reconsider signature" in json.dumps(c._agents["agent1"].messages)


def test_final_delivery_barrier_failure_and_immutable_plan(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    for aid in c._assignments:
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    c._observed_confirmations = dict(c._confirmations)
    c.finish_planning(c._version)
    old = dict(c._final_plan)
    returned = threading.Event()
    thread = threading.Thread(target=lambda: (c.before_query("agent1", c._agents["agent1"]), returned.set()))
    thread.start()
    assert not returned.wait(0.05)
    assert c._delivered == {"agent1"}
    c.before_query("agent2", c._agents["agent2"])
    assert returned.wait(2)
    thread.join(2)
    for aid in c._assignments:
        assert "[IMPLEMENTATION MODE]" in c._agents[aid].messages[-1]["content"]
        assert old["content"] in c._agents[aid].messages[-1]["content"]
    c.update_notebook("Runtime notebook changed.")
    assert c._final_plan == old
    c._agents["agent1"]._compaction_count += 1
    c.before_query("agent1", c._agents["agent1"])
    assert old["content"] in c._agents["agent1"].messages[-1]["content"]


@pytest.mark.parametrize("response", [[], [message()], [tool("update_notebook", content="x" * 8001)]])
def test_bad_finalization_wakes_waiters(tmp_path, response):
    c = planning(tmp_path)
    c._planning_steps["agent1"] = 200
    c._complete = lambda _: response
    c.decide()
    assert c.phase == "failed" and c._planning_outcome == "error"
    with pytest.raises(RuntimeError, match="planning_failed"):
        c.before_query("agent2", c._agents["agent2"])


@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
@pytest.mark.parametrize("outcome", ["agreed", "forced", "error"])
@pytest.mark.parametrize("early_edit", [False, True])
def test_real_loop_planning_patch_and_request_order(tmp_path, monkeypatch, variant, outcome, early_edit):
    import litellm

    from cooperagents.env.local import LocalEnv
    from cooperagents.trajectory import Trajectory, replay

    path = tmp_path / "coordination" / "notebook.md"
    ready = threading.Event()
    roster = [Assignment(agent_id=f"agent{i}", role="member", task=f"SYNTHETIC feature {i}", feature_id=i) for i in (1, 2)]
    observations, requests = [], []

    def decide(prompt):
        obs = json.loads(prompt.split("OBSERVATION:\n")[1])
        observations.append(obs)
        if obs["planning"]["phase"] == "finalizing":
            ready.set()
            return (
                []
                if outcome == "error"
                else [
                    tool("update_notebook", content="Final: agent1 a.py, agent2 b.py; no shared APIs or dependencies; run python checks.")
                ]
            )
        state = obs["planning"]
        if obs["initial"]:
            return [message("Inspect source and publicly report findings.", "group")]
        if len(state["participants"]) == 2 and state["candidate_version"] is None:
            ready.set()
            return [
                tool("update_notebook", content="Candidate: agent1 a.py, agent2 b.py; no shared APIs or dependencies; run python checks.")
            ]
        if len(state["confirmations"]) == 2:
            return [tool("finish_planning", version=obs["notebook"]["version"])]
        return []

    def complete(**kwargs):
        messages = kwargs["messages"]
        system = next(m["content"] for m in messages if m["role"] == "system")
        if variant == "auto-prompt":
            assert "OPENING PHASE PROTOCOL" in system
            assert "Earlier advice to continue independent edits" in system
            assert "Mode labels in group chat do not" in system
        else:
            task = messages[1]["content"]
            assert "SYNTHETIC feature" in task
            assert "RIGHT NOW" not in task
            if "CURRENT PHASE — PLANNING" in system:
                assert "clearly independent edits" not in system
                assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in task
                assert "MUST include AT LEAST ONE bash" not in task
                assert "confirm_plan(version)" in task
            else:
                assert "CURRENT PHASE — IMPLEMENTATION" in system
                assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in task
                assert "The coordinator may update the plan" in system
                assert not any(m.get("content", "").startswith("[PLANNING MODE]") for m in messages)
                assert any(m["role"] == "assistant" for m in messages[2:])
        requests.append(json.loads(json.dumps(kwargs, default=str)))
        assert {"bash", "send_message"} <= {t["function"]["name"] for t in kwargs["tools"]}
        assert (
            "wait"
            not in next(t for t in kwargs["tools"] if t["function"]["name"] == "send_message")["function"]["parameters"]["properties"]
        )
        phase_text = next(
            m["content"]
            for m in reversed(messages)
            if "[PLANNING MODE]" in (m.get("content") or "") or "[IMPLEMENTATION MODE]" in (m.get("content") or "")
        )
        step = sum(m["role"] == "assistant" for m in messages)
        if phase_text.startswith("[IMPLEMENTATION MODE]"):
            actions = [("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})]
        elif step == 0:
            # Deliberate early edit proves prompt-only permission and raw patch preservation.
            actions = [
                (
                    "bash",
                    {"command": "cat README.md; echo early > early.py" if early_edit else "cat README.md; python3 -c 'assert 1+1==2'"},
                ),
                ("send_message", {"recipient": "group", "content": "Inspected README.md and code; independent file proposal."}),
            ]
        elif outcome != "agreed":
            actions = [("bash", {"command": "echo discussion"})]
        elif "Current notebook version: 0" in phase_text:
            assert ready.wait(5)
            actions = [("bash", {"command": "echo candidate published"})]
        else:
            version = int(phase_text.split("Current notebook version: ")[1].splitlines()[0])
            actions = [("confirm_plan", {"version": version})]
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "synthetic reply",
                        "tool_calls": [
                            {"id": f"call-{step}-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                            for i, (name, args) in enumerate(actions)
                        ],
                    }
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 1},
        )

    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    monkeypatch.setattr(litellm, "completion", complete)

    def factory(aid):
        env = LocalEnv.fresh()
        env.write_file("README.md", "synthetic source evidence")
        env.execute("git add README.md && git -c user.name=cameron-chen -c user.email=abelco.cy@gmail.com commit -qm 'test: seed source'")
        env._base_commit = env._current_commit()
        execute = env.execute
        env.execute = lambda command, **kwargs: (
            ExecResult(path.read_text(), 0) if command.endswith("cat /coordination/notebook.md") else execute(command, **kwargs)
        )
        return env

    spec = TeamSpec(
        run_id="offline",
        repo="synthetic",
        task_id=1,
        features=[1, 2],
        assignments=roster,
        worker="mini_swe",
        model="dummy",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        coordinator=True,
        coordinator_notebook=True,
        repair_integrator=outcome == "error",
    )
    journal = Trajectory(tmp_path / "trajectory.jsonl")
    try:
        result = UnifiedHarness(
            step_limit=20,
            coordination_variant=variant,
            coordination_mode="group-chat-planning",
            planning_step_limit=2 if outcome != "agreed" else 200,
            coordinator_complete=decide,
            coordinator_notebook_path=path,
            trajectory=journal,
        ).run(spec, env_factory=factory)
    finally:
        journal.close()
    assert result.metrics["planning"]["outcome"] == outcome
    raw_patch = result.integrated.patch if outcome != "error" else result.metrics["raw_worker_patches"]["agent1"]
    assert ("early.py" in raw_patch) == early_edit
    from cooperagents.eval.cooperbench import write_run_outputs

    output = write_run_outputs(result, run_name="synthetic", logs_dir=tmp_path / "logs", model="dummy")
    saved = json.loads((output / "result.json").read_text())
    assert saved["metrics"]["planning"]["outcome"] == outcome
    assert (output / "agent1.patch").exists() and not (output / "eval.json").exists()
    if outcome == "error":
        assert (output / "agent1.raw.patch").read_text() == raw_patch
    if not early_edit:
        assert raw_patch == ""
    assert not replay(journal.path)["pending_calls"]
    events = [json.loads(line) for line in journal.path.read_text().splitlines()]
    from test_coordination_experiment import script

    receipt = script("audit_trajectories").audit_coordination(events, {"metrics": result.metrics})
    assert receipt["planning_outcome"] == outcome
    if outcome == "error":
        assert result.integrated.status == "planning_failed"
        assert not any(a.startswith("integrator") for a in result.seeds)
    else:
        deliveries = [e for e in events if e["event"] == "plan_delivery"]
        implementations = [e for e in events if e["event"] == "worker_request" and e["data"]["phase"] == "implementation"]
        assert len(deliveries) == 2 and max(e["seq"] for e in deliveries) < min(e["seq"] for e in implementations)
        assert deliveries[0]["data"]["content"] == deliveries[1]["data"]["content"]
        assert all(a.status == "submitted" for a in result.seeds.values())
    assert "[PLANNING MODE]" in json.dumps(requests[0]["messages"])


def test_inflight_decision_cannot_use_new_confirmations(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    c.confirm_plan("agent1", {"version": c._version}, sole=True)
    entered, release = threading.Event(), threading.Event()

    def complete(prompt):
        entered.set()
        assert release.wait(2)
        return [tool("finish_planning", version=c._version)]

    c._complete = complete
    thread = threading.Thread(target=c.decide)
    thread.start()
    assert entered.wait(2)
    c.confirm_plan("agent2", {"version": c._version}, sole=True)
    release.set()
    thread.join(2)
    assert c.phase == "planning" and c._planning_outcome is None
    assert c._last_error == "finish_planning failed: confirmation state changed during this decision. Planning remains active."
    assert "missing" not in c._last_error
    assert c._confirmations == {aid: c._version for aid in c._assignments}
    c._complete = lambda _: [tool("finish_planning", version=c._version)]
    c.decide()
    assert c._planning_outcome == "agreed"


@pytest.mark.parametrize("failure", ["timeout", "worker_exit"])
def test_finalization_transport_timeout_and_early_exit(tmp_path, failure):
    c = planning(tmp_path)
    if failure == "timeout":
        c._planning_steps["agent1"] = 200
        c._complete = lambda _: (_ for _ in ()).throw(TimeoutError("synthetic transport timeout"))
        c.decide()
    else:
        c.mark_finished("agent1", "submitted")
    assert c.phase == "failed" and c._planning_outcome == "error"
    with pytest.raises(RuntimeError, match="planning_failed"):
        c.before_query("agent2", c._agents["agent2"])


def test_overflow_retry_keeps_original_phase_and_candidate(tmp_path):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent

    c = planning(tmp_path)
    publish_plan(c)
    calls = []

    def query(messages):
        calls.append(json.loads(json.dumps(messages)))
        if len(calls) == 1:
            c._planning_steps["agent1"] = 200
            c._complete = lambda _: [tool("update_notebook", content="Forced final notebook: separate regions, verify imports.")]
            c.decide()
            raise RuntimeError("context length exceeded")
        return {"role": "assistant", "content": "retry retained", "extra": {"cost": 0}}

    model = SimpleNamespace(query=query, format_message=lambda **kwargs: kwargs)
    worker = DefaultAgent(
        model,
        SimpleNamespace(get_template_vars=lambda: {}),
        agent_id="agent1",
        system_template="system",
        instance_template="task",
        step_limit=1000,
    )
    worker.coordinator = c
    c._agents["agent1"] = worker
    worker.add_messages({"role": "system", "content": "system"}, {"role": "user", "content": "task"})
    worker.query()
    assert worker.n_calls == 1 and c._planning_steps["agent1"] == 200
    assert c._requests["agent1"]["phase"] == "planning" and not c._delivered
    phase = next(m["content"] for m in reversed(calls[1]) if m["content"].startswith("[PLANNING MODE]"))
    assert "Current notebook version: 1" in phase
    assert c.confirm_plan("agent1", {"version": 1}, sole=True)["returncode"] == 1


@pytest.mark.parametrize(
    "raw", ["[]", "null", '"text"', '{"command":"echo hi","tool_name":"confirm_plan"}', '{"command":"echo hi","tool_call_id":"fake"}']
)
def test_parser_rejects_non_object_and_forged_identity(raw):
    from cooperagents.vendor.mini_swe.exceptions import FormatError
    from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import parse_toolcall_actions

    call = SimpleNamespace(id="actual", function=SimpleNamespace(name="bash", arguments=raw))
    with pytest.raises(FormatError):
        parse_toolcall_actions([call], format_error_template="{{error}}")


@pytest.mark.parametrize("mode", ["direct", "group-chat", "group-chat-planning"])
@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
def test_six_configs_and_unsupported_early_rejection(tmp_path, monkeypatch, mode, variant):
    h = UnifiedHarness(coordination_mode=mode, coordination_variant=variant, coordinator_notebook_path=tmp_path / "notebook.md")
    spec = TeamSpec(
        run_id="test",
        repo="synthetic",
        task_id=1,
        features=[1, 2],
        assignments=[Assignment(agent_id=f"agent{i}", role="member", task="t") for i in (1, 2)],
        worker="mini_swe",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        coordinator=True,
        coordinator_notebook=True,
    )
    monkeypatch.setattr(h, "_run_isolated", lambda *a: "accepted")
    assert h.run(spec, env_factory=lambda _: (_ for _ in ()).throw(AssertionError("environment accessed"))) == "accepted"
    if mode != "direct":
        spec.wait_protocol = True
        with pytest.raises(ValueError, match="Group coordination"):
            h.run(spec, env_factory=lambda _: (_ for _ in ()).throw(AssertionError("environment accessed")))


def test_mixed_confirmation_preserves_bash_and_rejects_embedded_wait(tmp_path):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
    from cooperagents.workers.mini_swe_worker import BusComm

    c = planning(tmp_path)
    publish_plan(c)
    commands = []

    class Model:
        def get_template_vars(self):
            return {}

        def format_observation_messages(self, message, outputs, template_vars):
            return [{"role": "user", "content": output["output"]} for output in outputs]

    worker = DefaultAgent(
        Model(),
        SimpleNamespace(
            execute=lambda a: commands.append(a["command"]) or {"output": "read executed", "returncode": 0}, get_template_vars=lambda: {}
        ),
        agent_id="agent1",
        comm=BusComm(c._bus, "agent1", group=c),
        system_template="system",
        instance_template="task",
    )
    worker.tool_handlers = {"confirm_plan": lambda action: c.confirm_plan("agent1", action, sole=worker._sole_confirmation)}
    worker.execute_actions(
        {"extra": {"actions": [{"tool_name": "confirm_plan", "version": c._version}, {"tool_name": "bash", "command": "cat source.py"}]}}
    )
    assert commands == ["cat source.py"] and not c._confirmations
    previous = len(c._chat)
    worker.execute_actions({"extra": {"actions": [{"tool_name": "bash", "command": 'send_message --wait group "question"'}]}})
    assert len(c._chat) == previous and "wait is unavailable" in worker.messages[-1]["content"]


@pytest.mark.parametrize("mode", ["group-chat", "group-chat-planning"])
@pytest.mark.parametrize("kind", ["send_message", "bash", "bash-with-command", "bash-with-failed-command"])
@pytest.mark.parametrize(("recipient", "content", "error"), [
    ("all", "Please confirm the API.", "Unsupported recipient 'all'. Supported values: group, agent1, agent2, coordinator."),
    ("group", "   ", "Group message must be nonempty text"),
])
def test_group_message_error_allows_correction(tmp_path, mode, kind, recipient, content, error):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
    from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import format_toolcall_observation_messages
    from cooperagents.workers.mini_swe_worker import BusComm, _solo_config

    c = coordinator(coordination_mode=mode, notebook_path=tmp_path / "notebook.md")
    c.start_planning()
    commands, requests = [], []
    remaining_command = "\nprintf inspected" if kind.startswith("bash-with-") else ""
    command_returncode = 7 if kind == "bash-with-failed-command" else 0

    def action(to, text):
        if kind == "send_message":
            return dict(tool_name="send_message", recipient=to, content=text, tool_call_id="message-call")
        return dict(tool_name="bash", command=f"send_message {to} {json.dumps(text)}{remaining_command}", tool_call_id="bash-call")

    pending = iter([action(recipient, content), action("group", "Corrected group update")])

    def query(messages):
        requests.append(json.loads(json.dumps(messages)))
        return {"role": "assistant", "content": "", "extra": {"actions": [next(pending)]}}

    model = SimpleNamespace(
        query=query, get_template_vars=lambda: {}, format_message=lambda **kwargs: kwargs,
        format_observation_messages=lambda message, outputs, template_vars: format_toolcall_observation_messages(
            actions=message["extra"]["actions"], outputs=outputs,
            observation_template=_solo_config()["model"]["observation_template"], template_vars=template_vars,
        ),
    )
    worker = DefaultAgent(
        model,
        SimpleNamespace(
            get_template_vars=lambda: {},
            execute=lambda a: commands.append(a["command"]) or {
                "output": "inspected", "returncode": command_returncode, "exception_info": "",
            },
        ),
        agent_id="agent1", comm=BusComm(c._bus, "agent1", group=c),
        system_template="system", instance_template="task", cost_limit=0, compaction_enabled=False,
    )
    worker.coordinator = c
    c.register("agent1", worker)
    initial_phase = c.phase
    feedback = worker.step()[0]
    assert feedback["role"] == "tool"
    assert json.loads(feedback["content"]) == {
        "returncode": command_returncode or 1,
        "output": f"send_message failed: {error}" + ("\ninspected" if remaining_command else ""),
    }
    assert not c._chat and not worker.sent_messages and not c._finished
    assert c.phase == initial_phase

    corrected = worker.step()[0]
    assert feedback in requests[1]
    assert json.loads(corrected["content"])["returncode"] == command_returncode
    assert worker.n_calls == 2 and c.phase == initial_phase and not c._finished
    assert len(c._chat) == 1 and c._chat[0]["addressed_to"] == "group"
    assert c._chat[0]["content"] == "Corrected group update"
    assert worker.sent_messages == [{"to": "group", "content": "Corrected group update"}]
    assert commands == (["printf inspected"] * 2 if remaining_command else [])


@pytest.mark.parametrize("trigger", ["worker_exit", "stopped", "total_budget"])
def test_planning_failure_records_elapsed_once(tmp_path, monkeypatch, trigger):
    now = [1000.0]
    monkeypatch.setattr("cooperagents.harness.time.time", lambda: now[0])
    c = planning(tmp_path)
    now[0] += 150
    if trigger == "worker_exit":
        c.mark_finished("agent1", "submitted")
    elif trigger == "stopped":
        c.stop()
    else:
        c._agents["agent1"].config.wall_deadline = now[0]
        with pytest.raises(RuntimeError, match="total_budget"):
            c.before_query("agent2", c._agents["agent2"])
    metrics = c.planning_metrics()["planning"]
    assert metrics["outcome"] == "error"
    assert metrics["discussion_seconds"] == 150
    assert metrics["finalizing_seconds"] == 0
    now[0] += 20
    c.mark_finished("agent2", "error")
    assert c.planning_metrics()["planning"] == metrics


def test_forced_finalization_failure_records_elapsed(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("cooperagents.harness.time.time", lambda: now[0])
    c = planning(tmp_path)
    now[0] += 600

    def complete(_):
        now[0] += 12
        raise ConnectionError("synthetic finalization failure")

    c._complete = complete
    c.decide()
    metrics = c.planning_metrics()["planning"]
    assert metrics["outcome"] == "error" and c._planning_reason.startswith("Completion failed")
    assert metrics["discussion_seconds"] == 600
    assert metrics["finalizing_seconds"] == 12
    now[0] += 20
    c.stop()
    assert c.planning_metrics()["planning"] == metrics


def test_failure_preserves_elapsed_outside_planning_phases(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("cooperagents.harness.time.time", lambda: now[0])
    unstarted = coordinator(coordination_mode="group-chat-planning")
    unstarted.stop()
    assert unstarted.planning_metrics()["planning"]["discussion_seconds"] == 0

    c = planning(tmp_path)
    publish_plan(c)
    for aid in c._assignments:
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    c._complete = lambda _: [tool("finish_planning", version=c._version)]
    now[0] += 30
    c.decide()
    assert c.phase == "implementation"
    now[0] += 20
    c.mark_finished("agent1", "limit")  # Final plan has not reached both workers yet.
    metrics = c.planning_metrics()["planning"]
    assert metrics["outcome"] == "error"
    assert metrics["discussion_seconds"] == 30
    assert metrics["finalizing_seconds"] == 0


def test_write_error_exit_and_total_budget_wake_all(tmp_path, monkeypatch):
    c = planning(tmp_path)
    publish_plan(c)
    for aid in c._assignments:
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    done = threading.Event()
    errors = []

    def waiting():
        try:
            c.before_query("agent1", c._agents["agent1"])
        except RuntimeError as exc:
            errors.append(str(exc))
        finally:
            done.set()

    # Request snapshot already consumed all reports, so confirmation waits without spinning.
    thread = threading.Thread(target=waiting)
    thread.start()
    assert not done.wait(0.05)
    c._complete = lambda _: [tool("update_notebook", content="Revised candidate")]
    monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    c.decide()
    assert done.wait(2) and c.phase == "failed" and errors
    thread.join(2)
    monkeypatch.undo()
    c2 = planning(tmp_path / "second")
    c2._agents["agent1"].config.wall_deadline = time.time() - 1
    with pytest.raises(RuntimeError, match="total_budget"):
        c2.before_query("agent2", c2._agents["agent2"])


@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
def test_forced_backlog_never_claims_unseen_coordinator_delivery(tmp_path, monkeypatch, variant):
    from functools import partial

    import openai

    from cooperagents.planner import _default_planner_complete
    from cooperagents.trajectory import Trajectory
    from test_coordination_experiment import script

    c = planning(tmp_path, coordination_variant=variant)
    journal = Trajectory(tmp_path / "forced-backlog.jsonl")
    c.trace = partial(journal.emit, "coordinator")
    for aid in c._assignments:
        c.publish(aid, "group", f"Unique latest public report from {aid}")
    c._chat_positions["coordinator"] = 2
    for index in range(12):
        c.publish("coordinator", "group", f"Discussion {index}: " + "x" * 1100)
    captures = []

    def create(**kwargs):
        import litellm

        captures.append(json.loads(json.dumps(kwargs)))
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "forced",
                                "type": "function",
                                "function": {"name": "update_notebook", "arguments": json.dumps({"content": "Forced complete plan."})},
                            }
                        ],
                    }
                }
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        )

    monkeypatch.setattr(
        openai, "OpenAI", lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    c._complete = _default_planner_complete("synthetic", None, None, trace=c.trace, tools=c._tools)
    c._injected = False
    c._planning_steps["agent1"] = 200
    with c._condition:
        c._check_planning_limits_locked()
    c.publish("agent2", "group", "Late report must remain outside the frozen request")
    c._planning_steps["agent1"] = 201
    c.decide()
    journal.close()
    assert c._chat_positions["coordinator"] == 2
    from cooperagents.coordination_prompts import COORDINATOR_PLANNING_SYSTEM

    assert captures[0]["messages"][0] == {"role": "system", "content": COORDINATOR_PLANNING_SYSTEM}
    observation = json.loads(captures[0]["messages"][1]["content"].split("OBSERVATION:\n")[1])
    assert "frozen_evidence" not in observation and "replies" not in observation
    assert set(observation["group_chat"]) == {"unread", "recent", "backlog"}
    ids = [row["message_id"] for row in observation["group_chat"]["unread"]]
    assert ids == list(range(9, 15))
    assert [row["message_id"] for row in observation["group_chat"]["recent"]] == [1, 2]
    assert observation["group_chat"]["backlog"] == 12
    assert observation["planning"]["phase"] == "finalizing"
    assert observation["planning"]["reason"] == "step_limit"
    assert observation["planning"]["planning_steps"]["agent1"] == 200
    assert len(observation["workers"]) == 2 and all(worker["task"] for worker in observation["workers"])
    for aid in c._assignments:
        assert json.dumps(observation).count(f"Unique latest public report from {aid}") == 1
    assert "Late report" not in json.dumps(observation)
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert not any(row["event"] == "group_delivery" and row["data"]["target"] == "coordinator" for row in rows)
    receipt = script("audit_trajectories").audit_coordination(rows, {"metrics": {"coordination_mode": "group-chat"}})
    assert receipt["coordinator_input_evidence"] == "sdk_requests"
    # Old journals remain readable without requiring the retired input wrapper.
    legacy_rows = json.loads(json.dumps(rows))
    for row in legacy_rows:
        if row["event"] == "request" and row["actor"] == "coordinator" and "messages" in row["data"].get("request", {}):
            legacy = dict(observation)
            legacy["frozen_evidence"] = {"group_chat": legacy.pop("group_chat")["unread"]}
            row["data"]["request"]["messages"] = [{"role": "user", "content": "OBSERVATION:\n" + json.dumps(legacy)}]
    assert script("audit_trajectories").audit_coordination(
        legacy_rows, {"metrics": {"coordination_mode": "group-chat"}}
    )["coordinator_input_evidence"] == "sdk_requests"


def test_late_worker_reply_and_stale_coordinator_update_preserve_frozen_plan(tmp_path):
    c = planning(tmp_path)
    publish_plan(c)
    entered, release = threading.Event(), threading.Event()

    def complete(prompt):
        entered.set()
        assert release.wait(2)
        return [tool("update_notebook", content="Stale normal decision must not be applied")]

    c._complete = complete
    thread = threading.Thread(target=lambda: c.decide(initial=True))
    thread.start()
    assert entered.wait(2)
    c._planning_steps["agent1"] = 200
    with c._condition:
        c._check_planning_limits_locked()
    frozen = json.dumps(c._frozen)
    c.publish("agent2", "group", "Late in-flight report preserved for implementation")
    release.set()
    thread.join(2)
    assert c.phase == "finalizing" and json.dumps(c._frozen) == frozen
    assert "Stale normal decision" not in c._notebook
    c._complete = lambda _: [tool("update_notebook", content="Complete final decision from frozen evidence.")]
    c.decide()
    assert c._planning_outcome == "forced"
    assert "Late in-flight report" in c._chat[-1]["content"]
