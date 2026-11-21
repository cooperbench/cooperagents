"""Exercise the coordinator through real worker loops and local Git checkouts."""

import json
import re
import shlex
import socket
import threading
from collections import Counter
from types import SimpleNamespace

import pytest

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.trajectory import Trajectory, open_events
from cooperagents.types import Assignment, TeamSpec

WORKERS = ("agent1", "agent2")
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
WAIT_SECONDS = 65  # Two real direct-mode monitor ticks take 40 seconds.


def unexpected(*args, **kwargs):
    raise AssertionError("Unexpected network access or unscripted completion")


def action(name, **arguments):
    return name, arguments


def send(recipient, content):
    return action("send_message", recipient=recipient, content=content)


def bash(command):
    return action("bash", command=command)


def response(*actions):
    from litellm import ModelResponse

    return ModelResponse(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": f"tool-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                        for i, (name, args) in enumerate(actions)
                    ],
                }
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 1},
    )


def observation(messages):
    return json.loads(messages[-1]["content"].split("\nOBSERVATION:\n", 1)[1])


def user_text(messages):
    return "\n".join(m.get("content") or "" for m in messages if m["role"] == "user")


def tool_names(tools):
    return {tool["function"]["name"] for tool in tools}


def events(rows, kind, actor=None):
    return [row for row in rows if row["event"] == kind and (actor is None or row["actor"] == actor)]


def requests(rows, actor):
    return [row for row in events(rows, "request", actor) if "messages" in row["data"]["request"]]


def request_text(row):
    return user_text(row["data"]["request"]["messages"])


def matching_response(rows, request):
    (reply,) = [row for row in events(rows, "response", request["actor"]) if row["data"]["call_id"] == request["seq"]]
    return reply


def sends_from_response(row):
    calls = row["data"]["response"]["choices"][0]["message"].get("tool_calls") or []
    return [json.loads(call["function"]["arguments"]) for call in calls if call["function"]["name"] == "send_message"]


def wait(event):
    assert event.wait(WAIT_SECONDS), "Timed out waiting for the scripted peer"


class ObservedTrajectory(Trajectory):
    """Let scripted completions wait for public trace events to be recorded."""

    def __init__(self, path):
        super().__init__(path)
        self.condition = threading.Condition()
        self.receipts = []

    def emit(self, actor, event, **data):
        seq = super().emit(actor, event, **data)
        with self.condition:
            self.receipts.append((event, data))
            self.condition.notify_all()
        return seq

    def until(self, event, predicate=lambda _: True, **expected):
        def received():
            return any(
                kind == event and predicate(data) and all(data.get(key) == value for key, value in expected.items())
                for kind, data in self.receipts
            )

        with self.condition:
            assert self.condition.wait_for(received, timeout=WAIT_SECONDS), (event, expected)


class NotebookLocalEnv(LocalEnv):
    """Map the worker's read-only notebook mount to the test's notebook file."""

    def __init__(self, repo_path, notebook):
        super().__init__(repo_path)
        self.notebook = notebook

    def execute(self, command, *, timeout=60):
        command = command.replace("cat /coordination/notebook.md", f"cat {shlex.quote(str(self.notebook))}")
        return super().execute(command, timeout=timeout)


def team_spec(**changes):
    return TeamSpec(
        run_id="offline-macro",
        repo="synthetic",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id=aid, role="member", task=f"Implement fixture for {aid}.", feature_id=i) for i, aid in enumerate(WORKERS, 1)
        ],
        worker="mini_swe",
        model="dummy",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        coordinator=True,
        agent_time_limit=120,
        **changes,
    )


@pytest.fixture
def journal(tmp_path):
    return ObservedTrajectory(tmp_path / "trajectory.jsonl.gz")


@pytest.fixture
def episode(tmp_path, monkeypatch, journal):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://unused.invalid")
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected)

    import litellm
    import openai

    def run(coordinator, worker, *, planning=False, notebook=False, **options):
        # Exercise the real SDK request path for direct mode, including its live tool schema.
        monkeypatch.setattr(
            openai,
            "OpenAI",
            lambda **_: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=coordinator)),
            ),
        )
        monkeypatch.setattr(litellm, "completion", unexpected if planning else worker)
        if planning:
            from cooperagents.completion import CompletionBinding, CompletionSettings

            settings = CompletionSettings(model="dummy", revision="macro-test", generation={"max_tokens": 1000}, timeout=WAIT_SECONDS)
            options["worker_completion"] = CompletionBinding(worker, settings, unexpected, settings)

        notebook_path = tmp_path / "coordination" / "notebook.md"
        bus = InMemoryBus("offline-macro")
        seed = LocalEnv.fresh(workdir=str(tmp_path))
        seed.write_file("shared.txt", "base\n")
        tracked = seed.execute("git add shared.txt && git commit -qm 'test fixture'")
        assert tracked.exit_code == 0, tracked.stdout

        def factory(aid):
            checkout = tmp_path / aid
            cloned = seed.execute(f"git clone -q -- . {shlex.quote(str(checkout))}")
            assert cloned.exit_code == 0, cloned.stdout
            return NotebookLocalEnv(str(checkout), notebook_path)

        harness = UnifiedHarness(
            bus=bus,
            trajectory=journal,
            coordination_mode="group-chat-planning" if planning else "direct",
            coordinator_notebook_path=notebook_path if notebook else None,
            step_limit=32,
            worker_reminder_interval=0,
            **options,
        )
        try:
            result = harness.run(team_spec(coordinator_notebook=notebook), env_factory=factory)
        finally:
            journal.close()
        with open_events(journal.path) as handle:
            rows = list(map(json.loads, handle))
        return result, rows, bus.message_log(), notebook_path

    return run


def actor_for(messages):
    task = user_text(messages)
    (aid,) = [aid for aid in WORKERS if f"Implement fixture for {aid}." in task]
    return aid


@pytest.mark.parametrize("notebook", [False, True], ids=["message-only", "with-notebook"])
def test_direct_private_delivery_tools_and_message_accounting(episode, notebook):
    advice = "private coordinator instruction"
    peer_message = "peer handoff café"
    peer_started, sent = threading.Event(), threading.Event()

    def coordinator(**request):
        actions = [send("agent1", advice)]
        if notebook:
            actions.append(action("update_notebook", content="Use separate files."))
        return response(*actions)

    def worker(messages, **_):
        aid = actor_for(messages)
        first = not any(m["role"] == "assistant" for m in messages)
        if aid == "agent1":
            if first:
                wait(peer_started)
                return response(send("agent2", peer_message))
            sent.set()
        elif first:
            peer_started.set()
            wait(sent)
            return response(bash("true"))
        return response(bash(SUBMIT))

    result, rows, bus_log, notebook_path = episode(coordinator, worker, notebook=notebook)
    assert {aid: agent.status for aid, agent in result.seeds.items()} == dict.fromkeys(WORKERS, "submitted")

    (coord_request,) = requests(rows, "coordinator")
    expected_tools = {"send_message", "update_notebook"} if notebook else {"send_message"}
    assert tool_names(coord_request["data"]["request"]["tools"]) == expected_tools
    coord_response = matching_response(rows, coord_request)
    assert sends_from_response(coord_response) == [{"recipient": "agent1", "content": advice}]
    (nudge,) = events(rows, "nudge", "coordinator")
    (delivery,) = [row for row in events(rows, "delivery", "coordinator") if any(advice in text for text in row["data"]["messages"])]
    first_request = requests(rows, "agent1")[0]
    assert nudge["data"]["target"] == delivery["data"]["target"] == "agent1"
    assert nudge["data"]["text"] in delivery["data"]["messages"]
    assert nudge["data"]["text"] in request_text(first_request)
    assert coord_response["seq"] < nudge["seq"] < delivery["seq"] < first_request["seq"]
    assert all(advice not in json.dumps(row["data"]["request"]["messages"]) for row in requests(rows, "agent2"))
    if notebook:
        assert "Use separate files." in notebook_path.read_text()

    sender_call = matching_response(rows, requests(rows, "agent1")[0])
    assert sends_from_response(sender_call) == [{"recipient": "agent2", "content": peer_message}]
    (sent_event,) = events(rows, "message_sent")
    (delivered_event,) = events(rows, "message_delivered")
    byte_count = len(peer_message.encode())
    assert sent_event["actor"] == "agent1"
    assert sent_event["data"] == {"recipient": "agent2", "content": peer_message, "bytes": byte_count, "step": 1}
    assert delivered_event["actor"] == "agent2"
    assert {key: value for key, value in delivered_event["data"].items() if key != "step"} == {
        "sender": "agent1",
        "content": peer_message,
        "bytes": byte_count,
    }
    next_request = next(row for row in requests(rows, "agent2") if row["seq"] > delivered_event["seq"])
    assert sender_call["seq"] < sent_event["seq"] < delivered_event["seq"] < next_request["seq"]
    assert peer_message in request_text(next_request)
    assert [(m["from"], m["to"], m["content"]) for m in bus_log] == [("agent1", "agent2", peer_message)]


def test_direct_failed_decision_keeps_replies_and_signals_private(episode, journal):
    initial, correction = "guard the original interface", "proceed with the agreed interface"
    first_reply, late_reply = "initial worker proposal", "reply while coordinator is busy"
    rejected = "invalid recipient payload"
    expected_error = "Unsupported recipient 'all'. Supported values: agent1, agent2."
    decision_started, late_arrived = threading.Event(), threading.Event()
    active = threading.Lock()
    worker_calls = Counter()

    def coordinator(messages, **_):
        assert active.acquire(blocking=False), "Coordinator completions overlapped"
        try:
            seen = observation(messages)
            if seen["initial"]:
                return response(send("agent1", initial))
            if not seen["previous_error"]:
                decision_started.set()
                wait(late_arrived)
                return response(send("all", rejected))
            return response(send("agent1", correction))
        finally:
            active.release()

    def worker(messages, **_):
        aid = actor_for(messages)
        worker_calls[aid] += 1
        step = worker_calls[aid]
        if step == 1:
            edit = bash("printf 'both workers changed this file\n' > shared.txt")
            return response(edit, send("coordinator", first_reply)) if aid == "agent1" else response(edit)
        if aid == "agent1" and step <= 5:
            return response(bash("printf 'error: same failure\n'; exit 1"))
        if aid == "agent2" and step == 2:
            wait(decision_started)
            return response(send("coordinator", late_reply))
        if aid == "agent2" and step == 3:
            late_arrived.set()
            journal.until("decision", valid=False, error=expected_error)
            return response(bash("true"))
        if correction not in user_text(messages):
            journal.until("nudge", target="agent1", text=f"[coordinator] {correction}")
            if aid == "agent1":
                return response(bash("true"))
        return response(bash(SUBMIT))

    result, rows, bus_log, _ = episode(coordinator, worker)
    assert all(agent.status == "submitted" for agent in result.seeds.values())
    calls = requests(rows, "coordinator")
    assert len(calls) == 3
    for previous, following in zip(calls[:-1], calls[1:], strict=True):
        assert matching_response(rows, previous)["seq"] < following["seq"]
    observations = [observation(row["data"]["request"]["messages"]) for row in calls]
    assert [[reply["content"] for reply in obs["replies"]] for obs in observations] == [[], [first_reply], [first_reply, late_reply]]
    assert observations[2]["previous_error"] == expected_error
    (invalid,) = [row for row in events(rows, "decision", "coordinator") if not row["data"]["valid"]]
    assert invalid["data"]["error"] == expected_error
    (late_event,) = [row for row in events(rows, "message_sent") if row["data"]["content"] == late_reply]
    assert calls[1]["seq"] < late_event["seq"] < matching_response(rows, calls[1])["seq"]

    warnings = {worker["id"]: worker["warnings"] for worker in observations[1]["workers"]}
    signals = {"LOOP", "STALL", "POTENTIAL_CONFLICT"}
    assert {warning.partition(":")[0] for warning in warnings["agent1"]} == signals
    assert any(warning.startswith("POTENTIAL_CONFLICT:") and "shared.txt" in warning for warning in warnings["agent2"])
    received = [json.dumps(row["data"]["request"]["messages"]) for aid in WORKERS for row in requests(rows, aid)]
    assert all(rejected not in text and not any(f"{signal}:" in text for signal in signals) for text in received)
    assert all(initial not in request_text(row) and correction not in request_text(row) for row in requests(rows, "agent2"))
    nudges = events(rows, "nudge", "coordinator")
    assert [(row["data"]["target"], row["data"]["text"]) for row in nudges] == [
        ("agent1", f"[coordinator] {initial}"),
        ("agent1", f"[coordinator] {correction}"),
    ]
    for nudge in nudges:
        next_request = next(row for row in requests(rows, "agent1") if row["seq"] > nudge["seq"])
        assert nudge["data"]["text"] in request_text(next_request)
    final_request = request_text(requests(rows, "agent1")[-1])
    assert final_request.index(initial) < final_request.index(correction)
    expected_replies = [("agent1", "coordinator", first_reply), ("agent2", "coordinator", late_reply)]
    assert [(m["from"], m["to"], m["content"]) for m in bus_log] == expected_replies
    assert [
        (row["actor"], item["recipient"], item["content"])
        for aid in WORKERS
        for call in requests(rows, aid)
        for row in [matching_response(rows, call)]
        for item in sends_from_response(row)
    ] == expected_replies
    assert [(row["actor"], row["data"]["recipient"], row["data"]["content"]) for row in events(rows, "message_sent")] == expected_replies
    assert [
        (item["from"], item["to"], item["content"]) for row in events(rows, "replies", "coordinator") for item in row["data"]["messages"]
    ] == expected_replies
    assert not events(rows, "message_delivered")


def phase_notice(messages):
    return next(
        m["content"]
        for m in reversed(messages)
        if m["role"] == "user" and (m.get("content") or "").startswith(("[PLANNING MODE]", "[IMPLEMENTATION MODE]"))
    )


def implement(aid):
    return response(bash(f"printf 'implemented {aid}\n' > {aid}.txt; {SUBMIT}"))


def assert_plan_delivery(result, rows, worker_requests, content, outcome):
    assert {aid: agent.status for aid, agent in result.seeds.items()} == dict.fromkeys(WORKERS, "submitted")
    planning = result.metrics["planning"]
    assert planning["outcome"] == outcome
    assert planning["delivered"] == list(WORKERS)
    plan = planning["final_plan"]
    assert plan["content"] == content and plan["outcome"] == outcome

    (start,) = events(rows, "planning_start", "coordinator")
    assert start["data"]["phase"] == "planning"
    (end,) = events(rows, "planning_end", "coordinator")
    assert end["data"]["version"] == plan["version"]
    assert end["data"]["outcome"] == outcome
    deliveries = events(rows, "plan_delivery", "coordinator")
    assert {row["data"]["target"] for row in deliveries} == set(WORKERS)
    assert len(deliveries) == len(WORKERS)
    for row in deliveries:
        assert row["seq"] > end["seq"]
        assert {key: row["data"][key] for key in ("version", "sha256", "content")} == {
            key: plan[key] for key in ("version", "sha256", "content")
        }

    implementation = [row for row in events(rows, "worker_request") if row["data"]["phase"] == "implementation"]
    assert {row["data"]["target"] for row in implementation} == set(WORKERS)
    assert max(row["seq"] for row in deliveries) < min(row["seq"] for row in implementation)
    for aid in WORKERS:
        worker_turns = [req for req in worker_requests if req.actor_id == aid]
        assert phase_notice(worker_turns[0].messages).startswith("[PLANNING MODE]")
        implementation_turns = [req for req in worker_turns if phase_notice(req.messages).startswith("[IMPLEMENTATION MODE]")]
        assert implementation_turns and content in phase_notice(implementation_turns[0].messages)
        assert f"diff --git a/{aid}.txt b/{aid}.txt" in result.integrated.patch
        assert f"+implemented {aid}" in result.integrated.patch
    return end


def test_planning_rejects_invalid_finish_then_delivers_agreed_public_plan(episode, journal):
    group_opening, roster_opening = "inspect the source independently", "agent1 should check base behavior"
    reports = {"agent1": "agent1 found a separate edit", "agent2": "agent2 found an independent edit"}
    first_plan = "First candidate: split file ownership."
    final_plan = "Revised candidate: agent1 owns agent1.txt and agent2 owns agent2.txt."
    stale_report, mixed_report = "worker checked revised output", "worker checked the final file names"
    unwanted = "mixed finish must not publish this message"
    stale, mixed, missing = "version mismatch", "must be the only action", "missing valid confirmations"
    attempts = set()
    worker_requests = []
    confirmations_sent = set()

    def coordinator(messages, **_):
        obs = observation(messages)
        state = obs["planning"]
        if obs["initial"]:
            return response(send("group", group_opening), send("agent1", roster_opening))
        candidate = state["candidate_version"]
        confirmed = state["confirmations"]
        if set(state["participants"]) != set(WORKERS):
            return response()
        if candidate is None:
            return response(action("update_notebook", content=first_plan))
        if candidate == 1 and confirmed == {"agent1": 1}:
            return response(action("update_notebook", content=final_plan))
        if candidate != 2 or confirmed.get("agent1") != 2:
            return response()
        if confirmed == dict.fromkeys(WORKERS, 2):
            return response(action("finish_planning", version=2))
        chat = {row["content"] for key in ("unread", "recent") for row in obs["group_chat"][key]}
        if stale not in attempts:
            attempts.add(stale)
            return response(action("finish_planning", version=1))
        if stale_report in chat and mixed not in attempts:
            attempts.add(mixed)
            return response(send("group", unwanted), action("finish_planning", version=2))
        if mixed_report in chat and missing not in attempts:
            attempts.add(missing)
            return response(action("finish_planning", version=2))
        return response()

    def worker(request):
        worker_requests.append(request)
        aid = request.actor_id
        notice = phase_notice(request.messages)
        if notice.startswith("[IMPLEMENTATION MODE]"):
            return implement(aid)
        if request.call_id == 1:
            return response(send("group" if aid == "agent1" else "agent1", reports[aid]))
        version = int(re.search(r"Current notebook version: (\d+)", notice)[1])
        if version < 2 and (version == 0 or aid == "agent2"):
            journal.until("notebook_update", version=version + 1)
            return response(bash("true"))
        if aid == "agent1":
            if version in confirmations_sent:
                if version == 1:
                    journal.until("notebook_update", version=2)
                else:
                    journal.until("planning_end")
                return response(bash("true"))
            confirmations_sent.add(version)
            return response(action("confirm_plan", version=version))
        text = user_text(request.messages)
        if stale_report not in text:
            journal.until("decision", lambda data: stale in data.get("error", ""), valid=False)
            return response(send("group", stale_report))
        if mixed_report not in text:
            journal.until("decision", lambda data: mixed in data.get("error", ""), valid=False)
            return response(send("agent1", mixed_report))
        journal.until("decision", lambda data: missing in data.get("error", ""), valid=False)
        return response(action("confirm_plan", version=version))

    result, rows, _, notebook_path = episode(
        coordinator,
        worker,
        planning=True,
        notebook=True,
        coordinator_implementation_enabled=False,
    )
    initial_request = requests(rows, "coordinator")[0]["data"]["request"]
    assert observation(initial_request["messages"])["planning"]["phase"] == "planning"
    assert tool_names(initial_request["tools"]) == {"send_message", "update_notebook", "finish_planning"}

    end = assert_plan_delivery(result, rows, worker_requests, final_plan, "agreed")
    assert final_plan in notebook_path.read_text()
    assert result.metrics["planning"]["final_plan"]["confirmations"] == dict.fromkeys(WORKERS, 2)
    accepted = [row for row in events(rows, "plan_confirmation") if row["data"]["accepted"]]
    assert {(row["data"]["target"], row["data"]["version"]) for row in accepted} == {
        ("agent1", 1),
        ("agent1", 2),
        ("agent2", 2),
    }
    rejected = [row for row in events(rows, "decision") if not row["data"]["valid"]]
    stale_error, mixed_error, missing_error = [
        next(row for row in rejected if error in row["data"]["error"]) for error in (stale, mixed, missing)
    ]
    assert stale_error["seq"] < mixed_error["seq"] < missing_error["seq"] < end["seq"]
    assert "Requested: 1. Current candidate: 2" in stale_error["data"]["error"]
    assert "agent2" in missing_error["data"]["error"]
    assert all(row["seq"] < end["seq"] for row in accepted)
    assert missing_error["seq"] < next(row["seq"] for row in accepted if row["data"]["target"] == "agent2")

    finishing_request = requests(rows, "coordinator")[-1]
    final_observation = observation(finishing_request["data"]["request"]["messages"])["planning"]
    assert final_observation["candidate_version"] == 2
    assert final_observation["confirmations"] == dict.fromkeys(WORKERS, 2)
    finished_response = matching_response(rows, finishing_request)
    calls = finished_response["data"]["response"]["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1 and calls[0]["function"]["name"] == "finish_planning"
    assert json.loads(calls[0]["function"]["arguments"]) == {"version": 2}
    assert finished_response["seq"] < end["seq"]

    group_messages = {row["data"]["content"]: row["data"] for row in events(rows, "group_message")}
    public = {group_opening: "group", roster_opening: "agent1", reports["agent1"]: "group", reports["agent2"]: "agent1"}
    assert {message: group_messages[message]["addressed_to"] for message in public} == public
    assert unwanted not in group_messages
    for aid in WORKERS:
        turns = [req for req in worker_requests if req.actor_id == aid]
        for message in public:
            record = group_messages[message]
            rendered = f"{record['sender']} -> {record['addressed_to']}] {message}"
            assert any(rendered in user_text(req.messages) for req in turns[1:])
        assert all(unwanted not in user_text(req.messages) for req in turns)


@pytest.mark.parametrize("reason", ["step_limit", "time_limit"])
def test_planning_budget_exhaustion_records_forced_handoff(episode, journal, reason):
    final_plan = "Budget fallback: agent1 and agent2 each edit their assigned file."
    worker_requests = []

    def coordinator(messages, **_):
        obs = observation(messages)
        if obs["planning"]["phase"] == "finalizing":
            return response(action("update_notebook", content=final_plan))
        return response()

    def worker(request):
        worker_requests.append(request)
        if phase_notice(request.messages).startswith("[IMPLEMENTATION MODE]"):
            return implement(request.actor_id)
        if reason == "time_limit":
            journal.until("planning_finalizing", reason="time_limit")
        return response(bash("true"))

    result, rows, _, _ = episode(
        coordinator,
        worker,
        planning=True,
        notebook=True,
        coordinator_implementation_enabled=False,
        planning_step_limit=3 if reason == "step_limit" else 20,
        planning_time_limit_s=3 if reason == "time_limit" else 60,
    )
    end = assert_plan_delivery(result, rows, worker_requests, final_plan, "forced")
    (start,) = events(rows, "planning_start")
    (finalizing,) = events(rows, "planning_finalizing")
    assert start["seq"] < finalizing["seq"] < end["seq"]
    assert finalizing["data"]["reason"] == end["data"]["reason"] == result.metrics["planning"]["reason"] == reason
    assert end["data"]["confirmations"] == {}
    frozen = finalizing["data"]["frozen"]["planning"]
    if reason == "step_limit":
        assert max(frozen["planning_steps"].values()) == frozen["step_limit"] == 3
        assert frozen["seconds_remaining"] > 0
    else:
        assert frozen["seconds_remaining"] == 0
        assert max(frozen["planning_steps"].values()) < frozen["step_limit"]
    (finalizing_request,) = [
        row["data"]["request"]
        for row in requests(rows, "coordinator")
        if observation(row["data"]["request"]["messages"])["planning"]["phase"] == "finalizing"
    ]
    assert tool_names(finalizing_request["tools"]) == {"update_notebook"}


@pytest.mark.parametrize(
    ("option", "value", "error"),
    [
        ("coordinator_notebook", False, "Group coordination requires"),
        ("wait_protocol", True, "no wait-protocol"),
        ("task_board", True, "does not support task-board"),
        ("claim_mode", True, "does not support task-board, claim-mode"),
        ("allow_spawn_tool", True, "helpers"),
    ],
)
def test_planning_rejects_incompatible_configuration_before_setup(tmp_path, option, value, error):
    notebook = tmp_path / "coordination" / "notebook.md"
    harness = UnifiedHarness(
        bus=InMemoryBus("invalid"),
        coordination_mode="group-chat-planning",
        coordinator_notebook_path=notebook,
        coordinator_complete=unexpected,
    )
    options = {"coordinator_notebook": True, option: value}
    with pytest.raises(ValueError, match=error):
        harness.run(team_spec(**options), env_factory=unexpected)
    assert not notebook.exists()
