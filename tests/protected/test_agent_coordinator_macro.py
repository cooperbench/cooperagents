"""Exercise agent coordination through offline mini-SWE episodes and real Git clones."""

import json
import shlex
import socket
import threading
from collections import Counter
from dataclasses import asdict, replace
from hashlib import sha256
from typing import NamedTuple

import pytest

from cooperagents.agent_coordination_types import AgentCoordinatorConfig, DecisionResult, GateConfig, OpeningPlan, WorkerPlan
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.local import LocalEnv
from cooperagents.git_snapshots import GitShareSync
from cooperagents.harness import UnifiedHarness
from cooperagents.trajectory import Trajectory, open_events
from cooperagents.types import Assignment, RunResult, TeamSpec

WORKERS = ("agent1", "agent2")
FIRST, SECOND = WORKERS
COORDINATOR = "coordinator"
GATE = "coordinator_gate"
WAIT_SECONDS = 4
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
FAIL = "sh .cb_checks/fail.sh"
OPENING_NOTE = "Keep the existing file interface."
ADVICE = "Inspect the failed check before retrying."
MODEL = "liquid/d1.2-ultra"
BASE_TEXT = "base one\nbase two\nbase three\n"


def unexpected(*args, **kwargs):
    raise AssertionError("Unexpected network access or unscripted completion")


def events(rows, kind, actor=None):
    return [row for row in rows if row["event"] == kind and (actor is None or row["actor"] == actor)]


def data_rows(rows, kind, actor=None):
    return [row["data"] for row in events(rows, kind, actor)]


def messages(row):
    return row["data"]["request"]["messages"]


def worker_requests(rows, aid):
    return [row for row in events(rows, "request", aid) if "messages" in row["data"]["request"]]


def message_text(messages, role):
    return "\n".join(message.get("content") or "" for message in messages if message["role"] == role)


def tool_names(tools):
    return {tool["function"]["name"] for tool in tools}


def tool(name, **arguments):
    return {"name": name, "arguments": json.dumps(arguments)}


def latest_result(messages):
    """Return the newest message before a trailing coordinator budget notice."""
    if messages[-1]["role"] == "user" and messages[-1]["content"].startswith("[remaining_tool_calls: "):
        return messages[-2]
    return messages[-1]


def opening_plan():
    return tool("submit_plan", **asdict(OpeningPlan(
        version=1, workers={aid: WorkerPlan([f"{aid}.txt"], [], [], OPENING_NOTE) for aid in WORKERS},
        shared_interfaces=[], risks=[], evidence=[],
    )))


def end_wake(_messages, tools):
    assert "end_wake" in tool_names(tools)
    return [tool("end_wake", note="Done")]


def advise(messages, tools):
    return [tool("send_message", recipient=FIRST, content=ADVICE), *end_wake(messages, tools)]


def decision_result(p_wake, *, p_skip=None):
    return DecisionResult(
        probabilities={"decision": {"WAKE": p_wake, "SKIP": 1 - p_wake if p_skip is None else p_skip}},
        chosen={"decision": "WAKE" if p_wake > 0.5 else "SKIP"},
        latency_ms=1, input_tokens=12, completion_tokens=0, cost_usd=0.001,
        raw_model=MODEL, provider="fixture", response_id="fixture-decision",
    )


def team_spec(**changes):
    return replace(TeamSpec(
        run_id="agent-macro", repo="synthetic", task_id=1, features=[1, 2],
        assignments=[Assignment(aid, f"Implement the fixture for {aid}.", feature_id=i) for i, aid in enumerate(WORKERS, 1)],
        worker="mini_swe", model="dummy", shared_workspace=True, coop_tools=True, seed_prior=False,
        git_share=True, agent_coordinator=True, agent_time_limit=15,
    ), **changes)


def config(gate=None, **changes):
    return replace(AgentCoordinatorConfig(
        gate=gate or GateConfig(policy="rule", heartbeat_steps=100),
        opening="none", spec_check=False, opening_time_limit_s=WAIT_SECONDS, wake_time_limit_s=WAIT_SECONDS,
        tick_s=0.01,
    ), **changes)


def model_gate(**changes):
    return replace(GateConfig(heartbeat_steps=100), **changes)


def edit(aid):
    return f"printf 'implemented {aid}\\n' > {aid}.txt"


def basic_worker(aid, turn, _journal):
    return (edit(aid), SUBMIT)[turn]


def wait_for(event):
    assert event.wait(WAIT_SECONDS), "Timed out waiting for scripted episode checkpoint"


class ObservedTrajectory(Trajectory):
    """Synchronize scripted turns on recorded events from the real coordinator."""

    def __init__(self, path):
        super().__init__(path)
        self.condition = threading.Condition()
        self.receipts = []

    def emit(self, actor, event, **data):
        seq = super().emit(actor, event, **data)
        with self.condition:
            self.receipts.append((actor, event, data))
            self.condition.notify_all()
        return seq

    def until(self, event, *, actor=None, count=1, **fields):
        def received():
            return sum(
                kind == event and (actor is None or source == actor)
                and all(data.get(key) == value for key, value in fields.items())
                for source, kind, data in self.receipts
            ) >= count

        with self.condition:
            assert self.condition.wait_for(received, timeout=WAIT_SECONDS), (event, actor, count, fields)


class Episode(NamedTuple):
    result: RunResult
    rows: list[dict]
    coordinator_status: str | None


@pytest.fixture
def episode(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-agent-gate")
    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://unused.invalid")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test Worker")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "worker@example.invalid")

    failures = []

    def guarded(callback):
        def call(*args, **kwargs):
            try:
                return callback(*args, **kwargs)
            except AssertionError as exc:
                failures.append(exc)
                raise
        return call

    forbid = guarded(unexpected)
    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid)

    import litellm
    import openai

    monkeypatch.setattr(openai, "OpenAI", forbid)
    base_env = LocalEnv.fresh(workdir=str(tmp_path))
    for name in (*(f"{aid}.txt" for aid in WORKERS), "shared.txt", "disjoint.txt"):
        base_env.write_file(name, BASE_TEXT)
    base_env.write_file(".cb_checks/fail.sh", "exit 1\n")
    base_env.write_file(".cb_checks/regression.sh", "test ! -e regression-broken\n")
    commit = base_env.execute("git add . && git commit -qm fixture")
    assert commit.exit_code == 0, commit.stdout
    run_number = 0

    def run(worker=basic_worker, *, settings=None, coordinator=unexpected, gate=unexpected, deterministic=False, **options):
        nonlocal run_number
        run_number += 1
        path = tmp_path / f"episode-{run_number}"
        journal = ObservedTrajectory(path / "trajectory.jsonl.gz")
        shared_git = shlex.quote(str(path / "shared.git"))
        turns = Counter()
        coordinator_status = None

        class MountedLocalEnv(LocalEnv):
            def execute(self, command, *, timeout=60):
                return super().execute(command.replace("/cbshared/repo.git", shared_git), timeout=timeout)

            def cleanup(self):
                nonlocal coordinator_status
                if self.actor == COORDINATOR:
                    status = super().execute("git status --porcelain")
                    assert status.exit_code == 0, status.stdout
                    coordinator_status = status.stdout
                super().cleanup()

        def factory(aid):
            env = MountedLocalEnv.clone(base_env.repo_path, workdir=str(path))
            env.actor = aid
            return env

        def complete_worker(messages, **_):
            context = message_text(messages, "user")
            actors = [aid for aid in WORKERS if f"Implement the fixture for {aid}." in context]
            assert len(actors) == 1, "Unexpected worker completion"
            (aid,) = actors
            turn = turns[aid]
            turns[aid] += 1
            command = worker(aid, turn, journal)
            return litellm.ModelResponse(
                choices=[{"message": {"role": "assistant", "content": "", "tool_calls": [{
                    "id": f"{aid}-{turn}", "type": "function", "function": tool("bash", command=command),
                }]}}],
                usage={"prompt_tokens": 10, "completion_tokens": 1},
            )

        monkeypatch.setattr(litellm, "completion", guarded(complete_worker))
        if not deterministic:
            options.update(
                agent_coordinator_config=settings or config(),
                agent_coordinator_complete=guarded(coordinator),
                agent_gate_complete=guarded(gate),
            )
        try:
            result = UnifiedHarness(
                bus=InMemoryBus("agent-macro"), trajectory=journal, step_limit=20,
                deterministic_coordinator_interval_s=0.01, **options,
            ).run(team_spec(agent_coordinator=not deterministic, deterministic_coordinator=deterministic), env_factory=factory)
        finally:
            journal.close()

        assert failures == []
        assert {aid: seed.status for aid, seed in result.seeds.items()} == dict.fromkeys(WORKERS, "submitted"), {
            aid: seed.error for aid, seed in result.seeds.items()
        }
        with open_events(journal.path) as handle:
            rows = list(map(json.loads, handle))
        assert not events(rows, "gate_error")
        return Episode(result, rows, coordinator_status)

    try:
        yield run
    finally:
        base_env.cleanup()


def test_a1_opening_plan_precedes_worker_requests(episode):
    def coordinator(messages, tools):
        assert {"read_file", "submit_plan"} <= tool_names(tools)
        if latest_result(messages)["role"] == "tool":
            assert latest_result(messages)["content"] == "base two\n"
            return [opening_plan()]
        return [tool("read_file", path="shared.txt", start=2, end=2)]

    run = episode(settings=config(opening="draft"), coordinator=coordinator)
    opening_requests = events(run.rows, "request", COORDINATOR)
    assert [row["data"]["phase"] for row in opening_requests] == ["opening-plan", "opening-plan"]
    opening_responses = events(run.rows, "response", COORDINATOR)
    assert [row["data"]["response"]["tool_calls"][0]["name"] for row in opening_responses] == ["read_file", "submit_plan"]
    (opened,) = events(run.rows, "opening_plan", COORDINATOR)
    assert opened["data"]["outcome"] == "delivered"
    for aid in WORKERS:
        first = worker_requests(run.rows, aid)[0]
        first_request = events(run.rows, "request", aid)[0]
        assert opening_requests[0]["seq"] < opening_responses[-1]["seq"] < opened["seq"] < first_request["seq"] <= first["seq"]
        inbox = message_text(messages(first), "user")
        assert "[Message from coordinator]: Proposed opening plan" in inbox
        (delivered_plan,) = [json.loads(line) for line in inbox.splitlines() if line.startswith('{"')]
        assert delivered_plan == opened["data"]["plan"]
        assert OPENING_NOTE in inbox
    assert run.coordinator_status == ""


def test_a2_rejected_read_file_paths_execute_no_commands(episode, monkeypatch):
    paths = ("../x", "/x", ".cb_checks/fail.sh")
    attempts = []
    executed = []
    original_execute = LocalEnv.execute

    def record_execute(env, command, *, timeout=60):
        executed.append((env.repo_path, command))
        return original_execute(env, command, timeout=timeout)

    monkeypatch.setattr(LocalEnv, "execute", record_execute)

    def coordinator(messages, _tools):
        # Opening runs before either worker and before the snapshot timer's first tick.
        if attempts:
            assert latest_result(messages)["role"] == "tool"
            assert latest_result(messages)["content"] == "Error: invalid repository path"
            assert executed == attempts[-1][1], f"Command executed for rejected path {attempts[-1][0]}"
        if len(attempts) == len(paths):
            return [opening_plan()]
        path = paths[len(attempts)]
        attempts.append((path, executed.copy()))
        return [tool("read_file", path=path, start=1, end=1)]

    run = episode(settings=config(opening="draft"), coordinator=coordinator)
    assert [path for path, _ in attempts] == list(paths)
    (opened,) = events(run.rows, "opening_plan", COORDINATOR)
    assert opened["data"]["outcome"] == "delivered"


def test_a3_no_progress_wakes_once_and_delivers_to_next_step(episode):
    def worker(aid, turn, journal):
        if aid == SECOND:
            return basic_worker(aid, turn, journal)
        if turn == 5:
            # The sixth turn's inbox was already drained when its poll
            # reported step five. Hold this turn until the wake delivers.
            journal.until("wake_end", target=FIRST, messages_sent=1)
        return ([FAIL] * 7 + [edit(aid), SUBMIT])[turn]

    run = episode(worker, settings=config(model_gate()), coordinator=advise, gate=lambda _: decision_result(0.9))
    (signal,) = [row for row in data_rows(run.rows, "signal", GATE) if row["kind"] == "NO_PROGRESS_FAILING"]
    assert (signal["target"], signal["severity"]) == (FIRST, "hard")
    assert signal["payload"]["window_steps"] == signal["payload"]["failures"] == 5

    (decision,) = data_rows(run.rows, "gate_decision", GATE)
    assert (decision["decision"], decision["source"], decision["probabilities"]["WAKE"]) == ("WAKE", "model", 0.9)
    assert decision["signal_ids"] == [signal["signal_id"]]
    recent = decision["state"]["recent_steps"]
    assert [(row["step"], row["command"], row["returncode"]) for row in recent] == [(i, FAIL, 1) for i in range(1, 6)]
    assert decision["state"]["diff"]["unchanged_for_steps"] == 5
    (started,) = events(run.rows, "wake_start", COORDINATOR)
    (ended,) = events(run.rows, "wake_end", COORDINATOR)
    assert started["data"]["candidate_id"] == decision["candidate_id"]
    assert started["data"]["target"] == ended["data"]["target"] == FIRST

    calls = worker_requests(run.rows, FIRST)
    assert calls[4]["seq"] < started["seq"] < ended["seq"] < calls[6]["seq"]
    next_request = next(row for row in calls if row["seq"] > ended["seq"])
    assert next_request == calls[6]
    assert f"[Message from coordinator]: {ADVICE}" in message_text(messages(next_request), "user")
    assert all(ADVICE not in message_text(messages(row), "user") for row in worker_requests(run.rows, SECOND))


@pytest.mark.parametrize(
    ("commands", "kind", "payload"),
    [
        pytest.param(
            ("printf 'changed\\nbase two\\nbase three\\n' > shared.txt", "git restore -- shared.txt",
             "printf 'changed again\\nbase two\\nbase three\\n' > shared.txt"),
            "HUNK_CHURN", {"path": "shared.txt", "region": "L1-1 / L1-1", "steps": 3, "last_test_pass_step": None},
            id="hunk-churn",
        ),
        pytest.param(
            ("sh .cb_checks/regression.sh", "touch regression-broken", "sh .cb_checks/regression.sh"),
            "TEST_REGRESSION", {"command": "sh .cb_checks/regression.sh", "passed_at_step": 1, "failed_at_step": 3},
            id="test-regression",
        ),
    ],
)
def test_a4_signals_fire_at_threshold(episode, commands, kind, payload):
    def worker(aid, turn, journal):
        if aid == SECOND:
            return basic_worker(aid, turn, journal)
        return (*commands, SUBMIT)[turn]

    run = episode(worker, settings=config(GateConfig(policy="rule", wake_budget=0)))
    (signal,) = [row for row in events(run.rows, "signal", GATE) if row["data"]["kind"] == kind]
    assert signal["data"]["target"] == FIRST
    assert signal["data"]["payload"] == payload
    _, _, threshold_request, following_request = worker_requests(run.rows, FIRST)
    assert threshold_request["seq"] < signal["seq"] < following_request["seq"]


def test_a5_snapshot_overlap_is_deduplicated_per_pair_path_and_label(episode, monkeypatch):
    edited = {aid: threading.Event() for aid in WORKERS}
    repeated = threading.Event()
    original_evidence = GitShareSync.evidence
    observations = []

    def publish_after_edits(sync):
        for checkpoint in edited.values():
            wait_for(checkpoint)
        sync.publish_cycle()

    def evidence(sync):
        result = original_evidence(sync)
        comparison = result.comparisons.get(WORKERS)
        if comparison and comparison.overlaps:
            observations.append(comparison)
            if len(observations) >= 3:
                repeated.set()
        return result

    # Drive one real publication as soon as both edits are ready instead of
    # waiting for the 45-second snapshot timer. The gate polls it repeatedly.
    monkeypatch.setattr(GitShareSync, "run", publish_after_edits)
    monkeypatch.setattr(GitShareSync, "evidence", evidence)

    def worker(aid, turn, _journal):
        if turn == 0:
            overlap = f"printf 'base one\\n{aid}\\nbase three\\n' > shared.txt"
            left = "printf 'left\\nbase two\\nbase three\\n' > disjoint.txt"
            right = "printf 'base one\\nbase two\\nright\\n' > disjoint.txt"
            return f"{overlap} && {left if aid == FIRST else right}"
        assert turn == 1
        edited[aid].set()
        wait_for(repeated)
        return SUBMIT

    run = episode(worker, settings=config(GateConfig(policy="rule", wake_budget=0)))
    label = "L2-2 / L2-2"
    assert observations[:3] == [observations[0]] * 3
    assert observations[0].covered == {"shared.txt", "disjoint.txt"}
    assert observations[0].overlaps == {"shared.txt": label}

    signals = [row for row in data_rows(run.rows, "signal", GATE) if row["kind"] == "PEER_OVERLAP"]
    assert Counter(row["target"] for row in signals) == dict.fromkeys(WORKERS, 1)
    assert len({row["signal_id"] for row in signals}) == 1
    for signal in signals:
        assert signal["severity"] == "hard"
        assert signal["payload"] == {"pair": list(WORKERS), "path": "shared.txt", "regions": label, "source": "snapshot"}


def test_a6_third_qualifying_candidate_skips_without_a_model_call(episode):
    model_calls = []

    def gate(request):
        model_calls.append(request)
        return decision_result(0.9)

    def worker(aid, turn, journal):
        if aid == SECOND:
            assert turn == 0
            return SUBMIT
        if turn in (1, 2):
            journal.until("wake_end", target=FIRST, count=turn, outcome="ended")
        if turn == 3:
            journal.until("gate_decision", target=FIRST, source="budget")
        return (edit(aid), "true", "true", SUBMIT)[turn]

    gate_config = model_gate(wake_budget=2, heartbeat_steps=1, min_steps_between_wakes=0, min_seconds_between_wakes=0)
    run = episode(worker, settings=config(gate_config), coordinator=end_wake, gate=gate)
    decisions = data_rows(run.rows, "gate_decision", GATE)
    assert [(row["decision"], row["source"], row["budget_remaining_after"]) for row in decisions] == [
        ("WAKE", "model", 1), ("WAKE", "model", 0), ("SKIP", "budget", 0),
    ]
    assert [row["state"]["recent_steps"][-1]["step"] for row in decisions] == [1, 2, 3]
    assert all(row["target"] == FIRST and row["candidate_kind"] == "heartbeat" for row in decisions)
    assert len(events(run.rows, "wake_start", COORDINATOR)) == 2
    assert len(model_calls) == len(events(run.rows, "request", GATE)) == 2
    assert all(row["request"]["tool_calls"] == [] for row in data_rows(run.rows, "request", GATE))


def json_shape(value):
    if isinstance(value, dict):
        assert not any("trigger" in key for key in value)
        return {key: json_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_shape(item) for item in value]
    return type(value)


def test_a7_random_wakes_match_drawn_team_steps_and_model_observations(episode):
    # The specified draw for seed 0, synthetic:1:1,2, and rollout 0 over eight steps.
    schedule = [1, 7]
    steps_per_worker = 4
    horizon = 2 * steps_per_worker
    options = dict(wake_budget=2, min_steps_between_wakes=0, min_seconds_between_wakes=0, heartbeat_steps=1)

    def worker(aid, turn, journal):
        offset = WORKERS.index(aid) * steps_per_worker
        if aid == SECOND and turn == 0:
            journal.until("signal", actor=GATE, target=FIRST, kind="WORKER_FINISHED")
        if turn:
            team_step = offset + turn
            journal.until("gate_decision", target=aid, count=turn)
            if team_step in schedule:
                journal.until("wake_end", target=aid, outcome="ended")
        return (edit(aid), "true", "true", "true", SUBMIT)[turn]

    def model(request):
        state = json.loads(request.state_text.rsplit("\n\n", 1)[1])
        team_step = WORKERS.index(state["target"]) * steps_per_worker + state["recent_steps"][-1]["step"]
        return decision_result(0.9 if team_step in schedule else 0.1)

    random_run = episode(worker, coordinator=end_wake, settings=config(GateConfig(
        policy="random", random_wakes=2, random_step_horizon=horizon, random_seed=0, **options,
    )))
    model_run = episode(worker, coordinator=end_wake, gate=model, settings=config(model_gate(**options)))

    shapes = []
    for run, policy in ((random_run, "random"), (model_run, "model")):
        metrics = run.result.metrics["agent_coordinator"]
        assert metrics["team_worker_steps"] == horizon
        decisions = {row["candidate_id"]: row for row in data_rows(run.rows, "gate_decision", GATE) if row["decision"] == "WAKE"}
        assert len(decisions) == 2
        starts = data_rows(run.rows, "wake_start", COORDINATOR)
        wake_requests = [row for row in events(run.rows, "request", COORDINATOR) if row["data"]["phase"] == "wake"]
        observed_steps = []
        observations = []
        for start, request in zip(starts, wake_requests, strict=True):
            decision = decisions[start["candidate_id"]]
            assert decision["source"] == policy
            worker_step = decision["state"]["recent_steps"][-1]["step"]
            observed_steps.append(WORKERS.index(start["target"]) * steps_per_worker + worker_step)
            observation = json.loads(messages(request)[-1]["content"])
            assert observation == start["observation"]
            assert set(observation) == {"roster", "opening_plan", "workers", "git_evidence", "unread_messages", "previous_messages"}
            observations.append(json_shape(observation))
        assert observed_steps == schedule
        shapes.append(observations)

    assert shapes[0] == shapes[1]
    assert not events(random_run.rows, "request", GATE)
    assert random_run.result.metrics["agent_coordinator"]["gate"]["random_unused"] == 0


def test_a8_frozen_diff_rejection_acceptance_and_model_error(episode):
    revision = "printf 'covers acceptance\\n' >> agent1.txt"

    def worker(aid, turn, journal):
        if aid == FIRST:
            return (edit(aid), SUBMIT, revision, SUBMIT)[turn]
        return basic_worker(aid, turn, journal)

    def coordinator(messages, tools):
        assert tool_names(tools) == {"spec_verdict"}
        observation = json.loads(messages[-1]["content"])
        feature = observation["feature"]
        if feature["feature_id"] == "2":
            raise RuntimeError("Scripted spec completion outage")
        assert "implemented agent1" in observation["frozen_diff"]
        if "covers acceptance" in observation["frozen_diff"]:
            return [tool("spec_verdict", verdict="ACCEPT", findings=[])]
        return [tool("spec_verdict", verdict="REJECT", findings=[dict(
            kind="missing_requirement", feature_id="1", quote=feature["task"], path="agent1.txt",
        )])]

    run = episode(worker, settings=config(spec_check=True, spec_gate_max_rejections=2), coordinator=coordinator,
                  persist_worker_patches=True)
    verdicts = events(run.rows, "spec_check", COORDINATOR)
    rejected, accepted = [row for row in verdicts if row["data"]["target"] == FIRST]
    (errored,) = [row for row in verdicts if row["data"]["target"] == SECOND]
    assert [row["data"]["verdict"] for row in (rejected, accepted, errored)] == ["REJECT", "ACCEPT", "ERROR"]
    retry = next(row for row in worker_requests(run.rows, FIRST) if row["seq"] > rejected["seq"])
    assert "SPEC_CHECK_REJECTED" in message_text(messages(retry), "tool")
    assert retry["seq"] < accepted["seq"]
    spec_requests = [row for row in events(run.rows, "request", COORDINATOR) if row["data"]["phase"] == "spec-check"]
    first_diff = next(
        obs["frozen_diff"] for row in spec_requests
        if row["seq"] < rejected["seq"] and (obs := json.loads(messages(row)[-1]["content"]))["feature"]["feature_id"] == "1"
    )
    assert rejected["data"]["frozen_diff_sha256"] == sha256(first_diff.encode()).hexdigest()
    for aid, row in ((FIRST, accepted), (SECOND, errored)):
        patch = run.result.metrics["raw_worker_patches"][aid]
        assert f"+implemented {aid}" in patch
        assert row["data"]["frozen_diff_sha256"] == sha256(patch.encode()).hexdigest()
        (ended,) = events(run.rows, "agent_end", aid)
        assert row["seq"] < ended["seq"] and ended["data"]["status"] == "submitted"
    assert "+covers acceptance" in run.result.metrics["raw_worker_patches"][FIRST]
    assert rejected["data"]["frozen_diff_sha256"] != accepted["data"]["frozen_diff_sha256"]
    (failure,) = data_rows(run.rows, "error", COORDINATOR)
    assert failure["error"] == "Scripted spec completion outage"


def test_a9_worker_prompt_and_tools_match_deterministic_family(episode):
    agent = episode()
    deterministic = episode(deterministic=True)

    def system_prompt(row):
        return [message["content"] for message in messages(row) if message["role"] == "system"]

    for aid in WORKERS:
        agent_calls = worker_requests(agent.rows, aid)
        deterministic_calls = worker_requests(deterministic.rows, aid)
        assert len(agent_calls) == len(deterministic_calls) == 2
        for agent_call, deterministic_call in zip(agent_calls, deterministic_calls, strict=True):
            assert system_prompt(agent_call) == system_prompt(deterministic_call)
            assert len(system_prompt(agent_call)) == 1
            assert tool_names(agent_call["data"]["request"]["tools"]) == tool_names(deterministic_call["data"]["request"]["tools"]) == {
                "bash", "send_message",
            }


@pytest.mark.parametrize("family", ["coordinator", "deterministic_coordinator"])
def test_a10_coordinator_families_are_mutually_exclusive(family):
    with pytest.raises(ValueError, match="Coordinator families are mutually exclusive"):
        UnifiedHarness().run(team_spec(**{family: True}), env_factory=unexpected)


@pytest.mark.parametrize(("option", "value"), [
    ("shared_workspace", False), ("coop_tools", False), ("worker", "builtin"), ("seed_prior", True), ("git_share", False),
    ("team_roles", True), ("adaptive", True), ("decompose", True), ("best_of_n", 2), ("contract_first", True),
    ("allow_spawn_tool", True), ("task_board", True), ("claim_mode", True), ("wait_protocol", True),
    pytest.param("assignments", [Assignment(FIRST, "one worker")], id="single-worker"),
    pytest.param("assignments", [Assignment(FIRST, "one"), Assignment(FIRST, "two")], id="duplicate-worker"),
    pytest.param("assignments", [Assignment("", "one"), Assignment(SECOND, "two")], id="empty-worker-id"),
    pytest.param("assignments", [Assignment(COORDINATOR, "one"), Assignment(SECOND, "two")], id="reserved-worker-id"),
])
def test_a10_incompatible_team_config_fails_before_env_factory(option, value):
    with pytest.raises(ValueError, match="Agent coordinator"):
        UnifiedHarness(agent_coordinator_config=config()).run(team_spec(**{option: value}), env_factory=unexpected)


@pytest.mark.parametrize("option", ["coordinator_complete", "coordinator_completion"])
def test_a10_legacy_completion_callbacks_fail_before_env_factory(option):
    harness = UnifiedHarness(coordination_mode="group-chat-planning", **{option: unexpected})
    with pytest.raises(ValueError, match="Agent coordinator requires its own completion callbacks"):
        harness.run(team_spec(), env_factory=unexpected)


@pytest.mark.parametrize(("gate", "error"), [
    pytest.param(GateConfig(policy="model", model=""), "invalid model", id="empty-model"),
    pytest.param(GateConfig(policy="model", base_url="http://example.com"), "invalid gate base URL", id="insecure-url"),
    pytest.param(GateConfig(policy="model", api_key_env="UNSET_MACRO_GATE_KEY"), "API key environment variable is empty", id="unset-key"),
])
def test_a10_invalid_model_gate_fails_before_env_factory(monkeypatch, gate, error):
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-agent-gate")
    monkeypatch.delenv("UNSET_MACRO_GATE_KEY", raising=False)
    harness = UnifiedHarness(agent_coordinator_config=config(gate), agent_gate_complete=unexpected)
    with pytest.raises(ValueError, match=error):
        harness.run(team_spec(), env_factory=unexpected)


def test_a11_metrics_schema_counts_match_trajectory(episode):
    def worker(aid, turn, journal):
        if turn == 1:
            journal.until("gate_decision", target=aid, source="model")
            if aid == FIRST:
                journal.until("wake_end", target=aid, outcome="ended")
        return basic_worker(aid, turn, journal)

    def coordinator(messages, tools):
        names = tool_names(tools)
        if "submit_plan" in names:
            return [opening_plan()]
        if names == {"spec_verdict"}:
            return [tool("spec_verdict", verdict="ACCEPT", findings=[])]
        return advise(messages, tools)

    def gate(request):
        state = json.loads(request.state_text.rsplit("\n\n", 1)[1])
        return decision_result(0.9 if state["target"] == FIRST else 0.1)

    gate_config = model_gate(heartbeat_steps=1, min_seconds_between_wakes=0)
    run = episode(worker, settings=config(gate_config, opening="draft", spec_check=True), coordinator=coordinator, gate=gate)
    metrics = run.result.metrics["agent_coordinator"]
    assert set(metrics) == {"schema", "config", "opening", "signals", "gate", "wakes", "spec_check", "probe", "usage",
                            "team_worker_steps", "timeline"}
    assert metrics["schema"] == "agent-coordinator-metrics/1"
    (start,) = data_rows(run.rows, "agent_coordinator_start", COORDINATOR)
    assert metrics["config"] == {
        "gate": {key: getattr(gate_config, key) for key in (
            "policy", "model", "base_url", "wake_threshold", "timeout_s", "wake_budget", "per_worker_wake_cap",
        )},
        "opening": "draft", "spec_check": True, "prompt_ids": start["prompt_ids"],
    }
    (opening,) = data_rows(run.rows, "opening_plan", COORDINATOR)
    assert set(metrics["opening"]) == {"outcome", "tool_calls", "seconds", "plan_sha256"}
    assert metrics["opening"] == {key: opening[key] for key in metrics["opening"]}
    assert opening["outcome"] == "delivered" and opening["tool_calls"] == 1

    signals = data_rows(run.rows, "signal", GATE)
    assert metrics["signals"] == {"by_kind": Counter(row["kind"] for row in signals),
                                  "by_worker": Counter(row["target"] for row in signals)}
    decisions = data_rows(run.rows, "gate_decision", GATE)
    gate_metrics = metrics["gate"]
    assert set(gate_metrics) == {"candidates", "wake", "skip", "by_reason", "by_source", "by_candidate_kind", "model_calls", "fallbacks",
                                 "p_wake", "latency_ms", "budget", "budget_remaining", "random_planned", "random_unused"}
    assert gate_metrics["candidates"] == len(decisions) == 2
    assert gate_metrics["wake"] == sum(row["decision"] == "WAKE" for row in decisions) == 1
    assert gate_metrics["skip"] == sum(row["decision"] == "SKIP" for row in decisions) == 1
    assert gate_metrics["by_reason"] == Counter(row["reason_code"] for row in decisions)
    assert gate_metrics["by_source"] == Counter(row["source"] for row in decisions)
    assert gate_metrics["by_candidate_kind"] == Counter(row["candidate_kind"] for row in decisions)
    assert gate_metrics["model_calls"] == len(events(run.rows, "request", GATE)) == 2
    assert gate_metrics["fallbacks"] == {"timeout": 0, "transport": 0, "invalid_response": 0}
    assert gate_metrics["p_wake"] == pytest.approx({"n": 2, "mean": 0.5, "p50": 0.5, "p90": 0.82})
    assert gate_metrics["budget"] == gate_config.wake_budget
    assert gate_metrics["budget_remaining"] == gate_config.wake_budget - 1
    assert gate_metrics["random_planned"] is gate_metrics["random_unused"] is None

    (wake,) = data_rows(run.rows, "wake_end", COORDINATOR)
    assert metrics["wakes"] == {"total": len(events(run.rows, "wake_start", COORDINATOR)), "tool_calls": wake["tool_calls"],
                               "messages_sent": wake["messages_sent"], "seconds": {"p50": wake["seconds"], "p95": wake["seconds"]}}
    verdicts = Counter(row["verdict"] for row in data_rows(run.rows, "spec_check", COORDINATOR))
    assert metrics["spec_check"] == {"checked": verdicts.total(), "accepted": verdicts["ACCEPT"],
                                     "rejected": verdicts["REJECT"], "errors": verdicts["ERROR"]}
    assert verdicts == {"ACCEPT": 2}

    worker_calls = sum(len(worker_requests(run.rows, aid)) for aid in WORKERS)
    assert set(metrics["probe"]) == {"calls", "errors", "latency_ms"}
    assert metrics["probe"]["calls"] == worker_calls
    assert metrics["probe"]["errors"] == sum(row["kind"] == "PROBE_ERROR" for row in signals) == 0
    assert metrics["team_worker_steps"] == worker_calls - len(WORKERS) == 2
    for latency in (gate_metrics["latency_ms"], metrics["probe"]["latency_ms"]):
        assert set(latency) == {"p50", "p95"}
        assert 0 <= latency["p50"] <= latency["p95"]

    assert metrics["usage"] == {
        COORDINATOR: {"calls": len(events(run.rows, "request", COORDINATOR)), "prompt_tokens": 0,
                      "cached_tokens": 0, "completion_tokens": 0},
        GATE: {"calls": len(events(run.rows, "request", GATE)), "prompt_tokens": sum(row["input_tokens"] for row in decisions),
               "cached_tokens": 0, "completion_tokens": sum(row["completion_tokens"] for row in decisions),
               "reported_cost_usd": pytest.approx(sum(row["response"]["cost_usd"] for row in data_rows(run.rows, "response", GATE)))},
    }
    timeline = metrics["timeline"]
    assert set(timeline) == {"opening_end_s", "first_worker_edit_s", "worker_end_s"}
    assert set(timeline["first_worker_edit_s"]) == set(timeline["worker_end_s"]) == set(WORKERS)
    for aid in WORKERS:
        assert 0 <= timeline["opening_end_s"] <= timeline["first_worker_edit_s"][aid] <= timeline["worker_end_s"][aid]


def test_a12_threshold_boundary_routes_heartbeats_to_model(episode):
    probabilities = iter((0.49, 0.50))
    calls = []

    def gate(request):
        calls.append(request)
        return decision_result(next(probabilities))

    def worker(aid, turn, journal):
        if aid == SECOND:
            assert turn == 0
            return SUBMIT
        if turn == 1:
            journal.until("gate_decision", target=aid, decision="SKIP", source="model")
        if turn == 2:
            journal.until("wake_end", target=aid, outcome="ended")
        return (edit(aid), "true", SUBMIT)[turn]

    run = episode(worker, settings=config(model_gate(wake_threshold=0.5, heartbeat_steps=1)), coordinator=end_wake, gate=gate)
    decisions = data_rows(run.rows, "gate_decision", GATE)
    requests = data_rows(run.rows, "request", GATE)
    assert len(decisions) == len(requests) == len(calls) == 2
    for decision, recorded, call, expected in zip(decisions, requests, calls, ((0.49, "SKIP"), (0.50, "WAKE")), strict=True):
        p_wake, outcome = expected
        assert decision["decision"] == outcome
        assert decision["source"] == "model"
        assert decision["candidate_kind"] == "heartbeat"
        assert decision["probabilities"] == pytest.approx({"WAKE": p_wake, "SKIP": 1 - p_wake})
        assert decision["threshold"] == 0.5
        assert decision["model"] == MODEL
        assert decision["completion_tokens"] == 0
        request = recorded["request"]
        assert request["tool_calls"] == []
        assert request["model"] == MODEL
        assert request["state_text"] == call.state_text
        assert len(call.questions) == 1 and call.questions[0].id == "decision"
        assert set(call.questions[0].criteria) == {"WAKE", "SKIP"}
        assert json.loads(call.state_text.rsplit("\n\n", 1)[1]) == decision["state"]
    (wake,) = data_rows(run.rows, "wake_start", COORDINATOR)
    assert wake["candidate_id"] == decisions[1]["candidate_id"]


@pytest.mark.parametrize(("error", "commands", "heartbeat", "outcome"), [
    pytest.param("transport", ("true",), 1, "SKIP", id="raising-heartbeat"),
    pytest.param("invalid_response", (FAIL,) * 5, 100, "WAKE", id="invalid-probability-sum-on-hard-signal"),
])
def test_a12_model_errors_fall_back_to_rule_gate(episode, error, commands, heartbeat, outcome):
    def gate(_request):
        if error == "transport":
            raise RuntimeError("Scripted decision completion outage")
        return decision_result(0.6, p_skip=0.6)

    def worker(aid, turn, journal):
        if aid == SECOND:
            assert turn == 0
            return SUBMIT
        if turn == len(commands):
            journal.until("gate_decision", target=aid)
            if outcome == "WAKE":
                journal.until("wake_end", target=aid, outcome="ended")
        return (*commands, SUBMIT)[turn]

    model = episode(worker, settings=config(model_gate(heartbeat_steps=heartbeat)), coordinator=end_wake, gate=gate)
    rule = episode(worker, settings=config(GateConfig(policy="rule", heartbeat_steps=heartbeat)), coordinator=end_wake)
    (fallback,) = data_rows(model.rows, "gate_decision", GATE)
    (reference,) = data_rows(rule.rows, "gate_decision", GATE)
    assert fallback["decision"] == reference["decision"] == outcome
    assert fallback["reason_code"] == reference["reason_code"]
    assert fallback["candidate_kind"] == reference["candidate_kind"]
    assert fallback["source"] == "fallback" and reference["source"] == "rule"
    assert fallback["error"] == error and fallback["probabilities"] is None
    (request,) = data_rows(model.rows, "request", GATE)
    assert request["request"]["tool_calls"] == []
    assert not events(rule.rows, "request", GATE)
    if error == "invalid_response":
        (response,) = data_rows(model.rows, "response", GATE)
        assert response["response"]["answers"]["decision"]["probabilities"] == {"WAKE": 0.6, "SKIP": 0.6}
    else:
        (failure,) = data_rows(model.rows, "error", GATE)
        assert failure["error"] == "Scripted decision completion outage"
