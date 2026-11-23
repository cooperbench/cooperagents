from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cooperagents.agent_coordination_types import (
    AgentComplete,
    AgentCoordinatorConfig,
    DecisionQuestion,
    DecisionRequest,
    DecisionResult,
    DiffProbe,
    GateConfig,
    GateDecision,
    GateState,
    OpeningPlan,
    Signal,
    SignalConfig,
    StepRecord,
    from_json,
    to_json,
)
from cooperagents.agent_coordinator import (
    AgentCoordinator,
    AgentCoordinatorPoller,
    DecisionCallRecorder,
    ReadOnlyGit,
    composed_completion_gate,
)
from cooperagents.agent_coordinator_prompts import GATE_PROMPT_ID, gate_prompt_id, prompt_id
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import Environment, ExecResult
from cooperagents.env.local import LocalEnv
from cooperagents.git_snapshots import GitEvidence, Snapshot
from cooperagents.harness import _TeammatePoller
from cooperagents.trajectory import Trajectory, replay
from cooperagents.types import Assignment

FIXTURES = Path(__file__).parent / "fixtures/agent_coordination/coordinator"
FEATURES = [Assignment("agent1", "Parse text using a public parse function.", feature_id=1),
            Assignment("agent2", "Render the parsed output.", feature_id=2)]
HARD_SIGNAL = Signal("agent1:TEST_REGRESSION:1", "TEST_REGRESSION", "hard", {})
PROBE = DiffProbe("base-fingerprint", {}, 0, 0, 2.0)
RULE = GateConfig(policy="rule", min_seconds_between_wakes=0, min_steps_between_wakes=0)
MODEL = GateConfig(min_seconds_between_wakes=0, min_steps_between_wakes=0)


class TrackerStub:
    def __init__(self, *, signals: list[Signal] | None = None) -> None:
        self.steps: list[StepRecord] = []
        self.attempts: list[tuple[str, str | None]] = []
        self.signals = signals or []

    def observe(self, records: list[StepRecord]) -> list[Signal]:
        self.steps.extend(records)
        return self.signals if records else []

    def submit_attempt(self, aid: str, error: str | None) -> list[Signal]:
        self.attempts.append((aid, error))
        signals = [Signal(f"{aid}:SUBMIT_ATTEMPT", "SUBMIT_ATTEMPT", "info", {})]
        if error:
            signals.append(Signal(f"{aid}:SUBMIT_REJECTED", "SUBMIT_REJECTED", "warn", {"error_head": error}))
        return signals


class GateStub:
    def __init__(self, *, wakes: bool = True) -> None:
        self.wakes = wakes
        self.states: list[GateState] = []

    def decide(self, state: GateState, *, candidate_id: str, team_step: int) -> GateDecision:
        self.states.append(state)
        return GateDecision(
            candidate_id, state.candidate_kind, "WAKE" if self.wakes else "SKIP",
            "HEARTBEAT_PATTERN" if not state.signals else "TEST_REGRESSION",
            state.target, [s.signal_id for s in state.signals], "rule", None, None, None, 1.0, None, None,
            GATE_PROMPT_ID, state.budget["remaining"] - self.wakes,
        )


class CompletionStub:
    def __init__(self, *responses: list[dict] | Exception) -> None:
        self.responses = iter(responses)
        self.inputs: list[tuple[list[dict], list[dict]]] = []

    def __call__(self, messages: list[dict], tools: list[dict]) -> list[dict]:
        self.inputs.append((json.loads(json.dumps(messages)), tools))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def call(name: str, **args: Any) -> dict:
    return dict(name=name, arguments=json.dumps(args))


def step(index: int = 1) -> StepRecord:
    return StepRecord("agent1", index, "pytest " + "x" * 200, 1, "not-visible-in-wake", True)


def wait_for(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the coordinator"
        time.sleep(0.005)


@pytest.fixture
def repo(tmp_path):
    original = LocalEnv.fresh(workdir=str(tmp_path))
    original.write_file("src/core.py", "alpha\nbeta\nalpha\n")
    original.write_file("src/view.py", "alpha\n")
    original.write_file("src/uses ' $ chars.py", "quoted path\n")
    original.write_file("src/long.py", "a" * 17000 + "\n" + ("a\n" * 500))
    assert original.execute("git add . && git commit -qm baseline").exit_code == 0
    yield LocalEnv(original.repo_path)
    original.cleanup()


@pytest.fixture
def journal(tmp_path):
    trajectory = Trajectory(tmp_path / "trajectory.jsonl")
    yield trajectory
    trajectory.close()


@pytest.fixture
def coordinators(repo, journal):
    all_coordinators = []

    def create(**kwargs) -> AgentCoordinator:
        kwargs.setdefault("trackers", {aid.agent_id: TrackerStub() for aid in FEATURES})
        kwargs.setdefault("gate", GateStub())
        kwargs.setdefault("config", AgentCoordinatorConfig(gate=RULE, opening="none", tick_s=0.01))
        coordinator = AgentCoordinator(repo, InMemoryBus("pair"), FEATURES, **kwargs)
        all_coordinators.append(coordinator)
        return coordinator

    yield create
    for coordinator in all_coordinators:
        coordinator.finish()


def events(journal: Trajectory, event: str) -> list[dict]:
    return [
        row for line in journal.path.read_text().splitlines()
        if (row := json.loads(line))["event"] == event
    ]


def test_read_only_git_reads_objects_and_bounds_output(repo, tmp_path):
    base = repo._base_commit
    bare = tmp_path / "repo.git"
    assert repo.execute(f"git clone -q --bare . {bare}").exit_code == 0
    repo.write_file("src/core.py", "worker draft\n")
    assert repo.execute(f"git commit -qam 'worker draft' && git push -q {bare} HEAD:snapshot").exit_code == 0
    sha = repo.execute("git rev-parse HEAD").stdout.strip()
    repo.write_file("src/core.py", "private working tree content\n")
    evidence = GitEvidence(base, {"agent1": Snapshot(sha, "captured", "published")})
    git = ReadOnlyGit(repo, base, snapshot_repo=str(bare))

    assert git.read("list_dir", {"path": "src"}, evidence=evidence).splitlines() == [
        "core.py", "long.py", "uses ' $ chars.py", "view.py",
    ]
    assert git.read("list_dir", {"path": "."}, evidence=evidence) == "src\n"
    assert git.read("read_file", {"path": "src/core.py", "start": 2, "end": 3}, evidence=evidence) == "beta\nalpha\n"
    assert git.read("read_file", {"path": "src/core.py", "start": 1, "end": 2}, evidence=evidence, worker="agent1") == "worker draft\n"
    assert git.read("read_file", {"path": "src/uses ' $ chars.py", "start": 1, "end": 1}, evidence=evidence) == "quoted path\n"
    assert len(git.read("read_file", {"path": "src/long.py", "start": 1, "end": 1000}, evidence=evidence)) == 16000
    assert len(git.read("read_file", {"path": "src/long.py", "start": 2, "end": 1000}, evidence=evidence).splitlines()) == 400
    matches = git.read("search_code", {"pattern": "alpha", "path_glob": "src/*.py"}, evidence=evidence)
    assert [row.removeprefix(base + ":") for row in matches.splitlines()] == [
        "src/core.py:1:alpha", "src/core.py:3:alpha", "src/view.py:1:alpha",
    ]
    assert git.read("search_code", {"pattern": "absent", "path_glob": "*"}, evidence=evidence) == ""
    assert "worker draft" in git.read("git_show", {"rev": sha}, evidence=evidence)
    assert "+worker draft" in git.read("git_show", {"rev": sha, "path": "src/core.py"}, evidence=evidence)
    assert "baseline" in git.read("git_show", {"rev": base}, evidence=evidence)
    assert repo.execute("git status --porcelain").stdout == " M src/core.py\n"


@pytest.mark.parametrize("bad_path", [
    "/etc/passwd", "../core.py", "src/../core.py", "bad\x00path", "bad\npath", ".cb_config", "./.cb_state",
])
def test_tool_rejection_does_not_execute(bad_path):
    class RecordingEnv:
        def __init__(self):
            self.commands: list[str] = []

        def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
            self.commands.append(command)
            return ExecResult("", 0)

    env = RecordingEnv()
    base = "b" * 40
    git = ReadOnlyGit(cast(Environment, env), base)
    for name, args in [
        ("list_dir", {"path": bad_path}),
        ("read_file", {"path": bad_path, "start": 1, "end": 3}),
        ("search_code", {"pattern": "alpha", "path_glob": bad_path}),
        ("git_show", {"rev": base, "path": bad_path}),
    ]:
        with pytest.raises(ValueError, match="path"):
            git.read(name, args, evidence=GitEvidence())
    for rev in ("HEAD", "--help", base + "~1", "a" * 40):
        with pytest.raises(ValueError, match="revision"):
            git.read("git_show", {"rev": rev}, evidence=GitEvidence())
    with pytest.raises(ValueError, match="line range"):
        git.read("read_file", {"path": "src/core.py", "start": True, "end": 10}, evidence=GitEvidence())
    assert env.commands == []


def test_opening_delivers_a_valid_plan_once(coordinators, journal):
    data = (FIXTURES / "opening_plan.json").read_text()
    plan = from_json(OpeningPlan, data)
    complete = CompletionStub([call("read_file", path="src/core.py", start=1, end=2)], [call("submit_plan", **json.loads(data))])
    config = AgentCoordinatorConfig(gate=RULE, prompts={"opening-plan": "Inspect the base and draft a proposal."})
    coordinator = coordinators(complete=complete, config=config, trajectory=journal)
    assert coordinator.run_opening() == plan
    assert coordinator.run_opening() == plan
    assert [message["to"] for message in coordinator.bus.message_log()] == ["agent1", "agent2"]
    for aid in ("agent1", "agent2"):
        message, = coordinator.bus.receive(aid)
        assert message["from"] == "coordinator"
        assert "Verify it against the code" in message["content"]
        assert json.loads(message["content"].split("\n", 1)[1]) == json.loads(data)
    opening = coordinator.metrics()["opening"]
    assert opening["outcome"] == "delivered"
    assert opening["tool_calls"] == 2
    assert opening["plan_sha256"] == sha256(to_json(plan).encode()).hexdigest()
    assert events(journal, "opening_plan")[0]["data"]["prompt_id"] == prompt_id("opening-plan", config.prompts["opening-plan"])
    assert complete.inputs[1][0][-1]["content"] == "alpha\nbeta\n"


@pytest.mark.parametrize("response", [
    [call("list_dir", path=".")],
    [{"name": "submit_plan", "arguments": "{invalid"}],
    TimeoutError("test timeout"),
])
def test_opening_failure_is_passive(coordinators, response):
    complete = CompletionStub(response)
    coordinator = coordinators(complete=complete, config=AgentCoordinatorConfig(gate=RULE, opening_max_tool_calls=1))
    assert coordinator.run_opening() is None
    assert coordinator.metrics()["opening"]["outcome"] == "failed"
    assert coordinator.bus.message_log() == []
    assert len(complete.inputs) == 1


def test_wake_limits_blind_observation_and_compacts_prior_tools(coordinators, journal):
    gate = GateStub()
    complete = CompletionStub(
        [
            call("read_file", path="src/core.py", start=1, end=3),
            call("send_message", recipient="agent1", content="Check parse."),
            call("send_message", recipient="agent2", content="Coordinate render."),
            call("send_message", recipient="agent2", content="A third message is refused."),
            call("list_dir", path="."),
        ],
        [call("end_wake", note="Checked.")],
    )
    config = AgentCoordinatorConfig(gate=RULE, opening="none", wake_max_tool_calls=4, tick_s=0.01)
    coordinator = coordinators(gate=gate, complete=complete, config=config, trajectory=journal)
    coordinator.observe("agent1", [step()], PROBE)
    coordinator.enqueue("agent1", "signal", [HARD_SIGNAL])
    coordinator.start()
    wait_for(lambda: len(events(journal, "wake_end")) == 1)
    assert [msg["content"] for msg in coordinator.bus.message_log()] == ["Check parse.", "Coordinate render."]
    assert events(journal, "wake_end")[0]["data"]["tool_calls"] == 4
    keys = json.loads((FIXTURES / "wake_observation_keys.json").read_text())
    observation = json.loads(complete.inputs[0][0][-1]["content"])
    assert set(observation) == set(keys["observation"])
    assert set(observation["workers"]["agent1"]) == set(keys["worker"])
    assert set(observation["workers"]["agent1"]["recent_steps"][0]) == set(keys["step"])
    assert len(observation["workers"]["agent1"]["recent_steps"][0]["command"]) == 160
    assert "not-visible-in-wake" not in json.dumps(observation)
    coordinator.enqueue("agent2", "heartbeat")
    wait_for(lambda: len(events(journal, "wake_end")) == 2)
    messages = complete.inputs[1][0]
    outputs = [row["content"] for row in messages if row["role"] == "tool"]
    assert len(outputs) == 4
    assert all("\n" not in output and output.endswith(" chars") for output in outputs)
    assert outputs[0].startswith("read_file ")
    assert "alpha" not in outputs[0]
    assert json.loads(messages[-1]["content"])["previous_messages"] == [
        {"to": "agent1", "content": "Check parse."}, {"to": "agent2", "content": "Coordinate render."},
    ]


@pytest.mark.parametrize("content, finding", [(None, "empty_diff"), ("<<<<<<< LOCAL\n=======\n>>>>>>> peer\n", "conflict_marker")])
def test_spec_check_structural_rejections_do_not_call_model(coordinators, repo, journal, content, finding):
    complete = CompletionStub()
    coordinator = coordinators(complete=complete, trajectory=journal)
    repo.write_file("tests/test_before_submit.py", "test changes are stripped\n")
    if content:
        repo.write_file("src/core.py", content)
    assert finding in coordinator.spec_check("agent1", repo)
    assert coordinator.spec_check("agent1", repo) is None
    assert complete.inputs == []
    row, = events(journal, "spec_check")
    assert row["data"]["verdict"] == "REJECT"
    assert len(row["data"]["frozen_diff_sha256"]) == 64
    if not content:
        assert row["data"]["frozen_diff_sha256"] == sha256(b"").hexdigest()


def test_spec_check_freezes_untracked_and_strips_tests(coordinators, repo, journal):
    source = "src/new ' implementation.py"
    repo.write_file(source, "before model\n")
    repo.write_file("tests/test_new.py", "this is graded separately\n")

    def complete(messages, tools):
        submitted = json.loads(messages[-1]["content"])
        assert submitted["teammate_features"] == [{"feature_id": "2", "task": FEATURES[1].task}]
        assert submitted["feature"] == {"feature_id": "1", "task": FEATURES[0].task}
        assert "+before model" in submitted["frozen_diff"]
        assert "graded separately" not in submitted["frozen_diff"]
        repo.write_file(source, "after model\n")
        return [call("spec_verdict", verdict="ACCEPT", findings=[])]

    coordinator = coordinators(complete=complete, trajectory=journal, presub_merge=True)
    frozen = coordinator._freeze_diff(repo)
    assert coordinator.spec_check("agent1", repo) is None
    row, = events(journal, "spec_check")
    assert row["data"]["verdict"] == "ACCEPT"
    assert row["data"]["frozen_diff_sha256"] == sha256(frozen.encode()).hexdigest()
    assert repo.execute("git diff --cached --stat").stdout == ""


def test_spec_check_reject_then_accept_error_and_composition(coordinators, repo, journal):
    repo.write_file("src/core.py", "different\n")
    complete = CompletionStub(
        [call("spec_verdict", verdict="REJECT", findings=[
            {"kind": "missing_requirement", "feature_id": "1", "quote": "public parse function", "path": "src/core.py"},
        ])],
        [call("spec_verdict", verdict="ACCEPT", findings=[])],
        TimeoutError("test timeout"),
        [call("spec_verdict", verdict="REJECT", findings=[
            {"kind": "missing_requirement", "feature_id": "2", "quote": "invented requirement"},
        ])],
    )
    coordinator = coordinators(complete=complete, trajectory=journal)
    tracker = cast(TrackerStub, coordinator.trackers["agent1"])
    composed = composed_completion_gate(coordinator, "agent1", lambda env: "existing gate rejected")
    assert composed(repo) == "existing gate rejected"
    assert complete.inputs == []
    composed = composed_completion_gate(coordinator, "agent1")
    assert composed(repo) == "SPEC_CHECK_REJECTED\n- missing_requirement (feature 1) src/core.py: public parse function"
    assert composed(repo) is None
    assert tracker.attempts == [("agent1", "existing gate rejected"), ("agent1", None), ("agent1", None)]
    assert coordinator.spec_check("agent2", repo) is None
    assert coordinator.spec_check("agent2", repo) is None
    assert coordinator.spec_check("agent2", repo) is None
    assert len(complete.inputs) == 4
    assert [row["data"]["verdict"] for row in events(journal, "spec_check")] == ["REJECT", "ACCEPT", "ERROR", "ERROR"]
    assert coordinator.metrics()["spec_check"] == {"checked": 4, "accepted": 1, "rejected": 1, "errors": 2}


def test_poller_keeps_teammate_text_and_does_not_wait_for_gate(coordinators, repo, monkeypatch):
    gate_started = threading.Event()
    release_gate = threading.Event()

    class SlowGate(GateStub):
        def decide(self, state, *, candidate_id, team_step):
            gate_started.set()
            assert release_gate.wait(3)
            return super().decide(state, candidate_id=candidate_id, team_step=team_step)

    tracker = TrackerStub(signals=[HARD_SIGNAL])
    coordinator = coordinators(trackers={"agent1": tracker, "agent2": TrackerStub()}, gate=SlowGate())
    teammate = _TeammatePoller("agent1", {"agent1": repo, "agent2": repo})
    monkeypatch.setattr(teammate, "poll", lambda: " \n[git] teammate note\n")
    parsed = []
    probes = []

    def parse(worker: str, messages: list[dict], *, start: int, config: SignalConfig) -> list[StepRecord]:
        parsed.append(messages)
        return [step(start + 1)] if messages else []

    def probe(env: Environment) -> DiffProbe:
        probes.append(env)
        return PROBE

    poller = AgentCoordinatorPoller(coordinator, "agent1", repo, teammate, parse_steps=parse, probe=probe)
    agent = SimpleNamespace(messages=[{"role": "assistant"}])
    poller.attach(agent)
    coordinator.start()
    try:
        assert poller.poll() == " \n[git] teammate note\n"
        assert gate_started.wait(1)
        before = time.monotonic()
        assert poller.poll() == " \n[git] teammate note\n"
        assert time.monotonic() - before < 0.5
        assert parsed == [agent.messages, []] and probes == [repo, repo]
        assert tracker.steps == [replace(step(), fingerprint=PROBE.fingerprint)]
    finally:
        release_gate.set()


def test_gate_keeps_running_during_wake_and_finish_joins_both(coordinators):
    wake_started, release_wake = threading.Event(), threading.Event()
    gate = GateStub()

    def complete(messages: list[dict], tools: list[dict]) -> list[dict]:
        wake_started.set()
        release_wake.wait(10)
        return [call("send_message", recipient="agent1", content="Too late.")]

    coordinator = coordinators(gate=gate, complete=complete)
    coordinator.start()
    try:
        coordinator.enqueue("agent1", "heartbeat")
        assert wake_started.wait(1)
        coordinator.enqueue("agent2", "heartbeat")
        wait_for(lambda: len(gate.states) == 2)
        assert all(thread.is_alive() and thread.daemon for thread in coordinator._threads)
        before = time.monotonic()
        coordinator.stop()
        coordinator.finish()
        assert time.monotonic() - before < 10
        assert not any(thread.is_alive() for thread in coordinator._threads)
    finally:
        release_wake.set()
    assert coordinator.bus.message_log() == []


def test_candidates_coalesce_and_pending_wakes_merge_signals(coordinators, journal):
    wake_started, release_wake = threading.Event(), threading.Event()
    gate = GateStub()

    def complete(messages, tools):
        wake_started.set()
        assert release_wake.wait(3)
        return [call("end_wake", note="Done")]

    coordinator = coordinators(gate=gate, complete=complete, trajectory=journal)
    coordinator.enqueue("agent1", "heartbeat")
    coordinator.enqueue("agent1", "signal", [HARD_SIGNAL])
    coordinator.start()
    try:
        assert wake_started.wait(1)
        first = replace(HARD_SIGNAL, signal_id="agent2:TEST_REGRESSION:1")
        second = replace(first, signal_id="agent2:TEST_REGRESSION:2")
        coordinator.enqueue("agent2", "signal", [first])
        wait_for(lambda: coordinator.metrics()["gate"]["candidates"] == 2)
        coordinator.enqueue("agent2", "signal", [second])
        wait_for(lambda: coordinator.metrics()["gate"]["candidates"] == 3)
    finally:
        release_wake.set()
    wait_for(lambda: len(events(journal, "wake_end")) == 2)
    assert [state.candidate_kind for state in gate.states] == ["signal", "signal", "signal"]
    merged = events(journal, "wake_end")[1]["data"]
    assert len(merged["candidate_ids"]) == 2
    assert merged["signal_ids"] == [first.signal_id, second.signal_id]
    assert coordinator.metrics()["gate"]["budget_remaining"] == RULE.wake_budget - 2


def test_stale_wake_rechecks_constraints_without_scoring_again(coordinators, journal):
    now = [0.0]
    gate = GateStub()
    wake_started, release_wake = threading.Event(), threading.Event()

    def complete(messages, tools):
        wake_started.set()
        assert release_wake.wait(3)
        return [call("end_wake", note="Done")]

    coordinator = coordinators(gate=gate, complete=complete, clock=lambda: now[0], trajectory=journal)
    coordinator.start()
    try:
        coordinator.enqueue("agent1", "heartbeat")
        assert wake_started.wait(1)
        coordinator.enqueue("agent2", "heartbeat")
        wait_for(lambda: coordinator.metrics()["gate"]["wake"] == 2)
        now[0] = 61
        coordinator.worker_finished("agent2")
    finally:
        release_wake.set()
    wait_for(lambda: len(events(journal, "wake_end")) == 2)
    assert [row["data"]["outcome"] for row in events(journal, "wake_end")] == ["ended", "stale"]
    assert len(gate.states) == 2


def test_gate_model_request_rows_and_metrics(coordinators, journal):
    request_snapshots: list[DecisionRequest] = []

    def decide(request: DecisionRequest) -> DecisionResult:
        request_snapshots.append(request)
        if len(request_snapshots) == 2:
            raise TimeoutError("test timeout")
        return DecisionResult({"decision": {"WAKE": 0.78, "SKIP": 0.22}}, {"decision": "WAKE"}, 2.0, 734, 0, 0.00003,
                              "liquid/d1.2-ultra", "Liquid", "response-123")

    config = AgentCoordinatorConfig(gate=replace(MODEL, base_url="https://example.test/api?removed=1"), opening="none", tick_s=0.01)
    recorder = DecisionCallRecorder(decide, config.gate, journal)

    class ModelGate(GateStub):
        def decide(self, state: GateState, *, candidate_id: str, team_step: int) -> GateDecision:
            self.states.append(state)
            request = DecisionRequest(to_json(state), (DecisionQuestion("decision", "Answer", {"WAKE": "Wake", "SKIP": "Skip"}),),
                                      gate_prompt_id())
            try:
                result = recorder(request)
            except TimeoutError:
                return GateDecision(
                    candidate_id, state.candidate_kind, "SKIP", "HEARTBEAT_PATTERN", state.target, [], "fallback",
                    None, 0.5, "liquid/d1.2-ultra", 3.0, None, None, request.prompt_id, state.budget["remaining"], "timeout",
                )
            return GateDecision(
                candidate_id, state.candidate_kind, "WAKE", "TEST_REGRESSION", state.target, [HARD_SIGNAL.signal_id], "model",
                result.probabilities["decision"], 0.5, "liquid/d1.2-ultra", result.latency_ms, result.input_tokens,
                result.completion_tokens, request.prompt_id, state.budget["remaining"] - 1,
            )

    gate = ModelGate()
    complete: AgentComplete = CompletionStub([call("end_wake", note="Done")])
    coordinator = coordinators(gate=gate, complete=complete, config=config, trajectory=journal, decision_recorder=recorder)
    coordinator.observe("agent1", [step()], PROBE)
    coordinator.enqueue("agent1", "signal", [HARD_SIGNAL])
    coordinator.start()
    wait_for(lambda: coordinator.metrics()["wakes"]["total"] == 1)
    coordinator.enqueue("agent2", "heartbeat")
    wait_for(lambda: coordinator.metrics()["gate"]["candidates"] == 2)
    coordinator.finish()
    first, fallback = [row["data"] for row in events(journal, "gate_decision")]
    assert first["state"] == asdict(gate.states[0])
    assert first["probabilities"] == {"WAKE": 0.78, "SKIP": 0.22}
    assert first["threshold"] == 0.5 and first["model"] == "liquid/d1.2-ultra"
    assert fallback["error"] == "timeout"
    rows = [row for row in events(journal, "request") if row["actor"] == "coordinator_gate"]
    assert len(rows) == 2
    assert all(row["data"]["request"]["tool_calls"] == [] for row in rows)
    assert rows[0]["data"]["request"]["base_url"] == "https://example.test/api"
    assert rows[0]["data"]["request"]["state_text"] == to_json(gate.states[0])
    response, = [row for row in events(journal, "response") if row["actor"] == "coordinator_gate"]
    assert response["data"]["response"]["provider"] == "Liquid"
    assert response["data"]["response"]["tool_calls"] == []
    assert replay(journal.path)["pending_calls"] == {}
    metrics = coordinator.metrics()
    assert metrics["schema"] == "agent-coordinator-metrics/1"
    assert metrics["gate"]["model_calls"] == 2
    assert metrics["gate"]["fallbacks"] == {"timeout": 1, "transport": 0, "invalid_response": 0}
    assert metrics["gate"]["p_wake"] == {"n": 1, "mean": 0.78, "p50": 0.78, "p90": 0.78}
    assert metrics["usage"]["coordinator_gate"] == {
        "calls": 2, "prompt_tokens": 734, "cached_tokens": 0, "completion_tokens": 0, "reported_cost_usd": 0.00003,
    }


def test_default_completion_uses_sdk_and_records_usage(coordinators, journal, monkeypatch):
    requests = []
    clients = []

    class Response:
        choices = [SimpleNamespace(message=SimpleNamespace(tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name="spec_verdict", arguments='{"verdict":"ACCEPT","findings":[]}')),
        ]))]
        usage = SimpleNamespace(prompt_tokens=17, completion_tokens=5, prompt_tokens_details=SimpleNamespace(cached_tokens=8))

        def model_dump(self, *, mode: str) -> dict:
            return {"usage": {"prompt_tokens": 17, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 8}}}

    def create(**request):
        requests.append(request)
        return Response()

    def client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client))
    coordinator = coordinators(trajectory=journal, model="test-model", base_url="https://example.test", api_key="test-key")
    repo = coordinator.env
    repo.write_file("src/core.py", "different\n")
    assert coordinator.spec_check("agent1", repo) is None
    assert clients[0]["max_retries"] == 0 and requests[0]["model"] == "test-model"
    assert requests[0]["timeout"] <= 60
    assert coordinator.metrics()["usage"]["coordinator"] == {"calls": 1, "prompt_tokens": 17, "cached_tokens": 8, "completion_tokens": 5}
    row, = events(journal, "request")
    assert row["actor"] == "coordinator" and row["data"]["phase"] == "spec-check"
    assert "test-key" not in journal.path.read_text()
