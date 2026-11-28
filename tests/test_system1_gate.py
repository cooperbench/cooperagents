import ast
import json
import socket
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest

from cooperagents import system1_gate
from cooperagents.agent_coordination_types import DecisionResult, GateConfig, Signal, SignalConfig, to_json
from cooperagents.agent_coordinator_prompts import GATE_INSTRUCTIONS, GATE_PROMPT_ID, gate_prompt_id
from cooperagents.structural_signals import parse_steps
from cooperagents.system1_gate import DecisionGate, HttpDecisionClient, RandomGate, RuleGate, WakeBudget, build_gate_state, reason_code

FIXTURES = Path(__file__).parent / "fixtures" / "agent_coordination"
KEY = "test-stub-secret"
RULE = GateConfig(policy="rule")


def load(name):
    return json.loads((FIXTURES / "gate" / f"{name}.json").read_text())


def running_budget(config=RULE):
    budget = WakeBudget(config, started_at=50.0)
    budget.update_worker("agent1", step=15, status="running")
    budget.update_worker("agent2", step=12, status="running")
    return budget


def signal(kind, severity="warn"):
    return Signal(f"agent1:{kind}:15", kind, severity, {})


def state(budget, kind="signal", signals=(), target="agent1"):
    return build_gate_state(target, kind, list(signals), [], budget=budget, now=100.0, target_step=15)


def result(wake=0.5, *, chosen="SKIP"):
    return DecisionResult(
        probabilities={"decision": {"WAKE": wake, "SKIP": 1 - wake}},
        chosen={"decision": chosen},
        latency_ms=10.0,
        input_tokens=734,
        completion_tokens=0,
        cost_usd=0.000030828,
        raw_model="liquid/d1.2-ultra",
        provider="Liquid",
        response_id="test-123",
    )


@pytest.mark.parametrize(
    ("kind", "signals", "decision", "reason"),
    [
        ("signal", [signal("NO_PROGRESS_FAILING", "hard")], "WAKE", "NO_PROGRESS"),
        ("signal", [signal("NO_PROGRESS_FAILING")], "SKIP", "NO_PROGRESS"),
        ("signal", [signal("HUNK_CHURN")], "WAKE", "HUNK_CHURN"),
        ("signal", [signal("TEST_REGRESSION", "hard")], "WAKE", "TEST_REGRESSION"),
        ("signal", [signal("PEER_OVERLAP", "info")], "SKIP", "PEER_OVERLAP"),
        ("signal", [signal("PEER_OVERLAP", "hard")], "WAKE", "PEER_OVERLAP"),
        ("signal", [signal("SUBMIT_REJECTED")], "SKIP", "SUBMIT_REJECTED"),
        ("signal", [signal("SUBMIT_REJECTED", "hard")], "WAKE", "SUBMIT_REJECTED"),
        ("signal", [signal("PEER_MESSAGE", "info")], "WAKE", "PEER_MESSAGE"),
        ("signal", [signal("HUNK_CHURN"), signal("TEST_REGRESSION", "hard")], "WAKE", "TEST_REGRESSION"),
        ("signal", [signal("SUBMIT_REJECTED", "hard"), signal("NO_PROGRESS_FAILING", "hard")], "WAKE", "NO_PROGRESS"),
        ("peer_message", [], "WAKE", "PEER_MESSAGE"),
        ("heartbeat", [], "SKIP", "HEARTBEAT_PATTERN"),
        ("heartbeat", [signal("NO_PROGRESS_FAILING", "hard")], "SKIP", "HEARTBEAT_PATTERN"),
    ],
)
def test_rule_table_and_reason_for_each_source(kind, signals, decision, reason):
    budget = running_budget()
    request_state = state(budget, kind, signals)
    assert reason_code(request_state) == reason
    rule = RuleGate(RULE, budget)
    decided = rule.decide(request_state, 100)
    assert (decided.decision, decided.reason_code, decided.source) == (decision, reason, "rule")
    assert decided.budget_remaining_after == 8 - (decision == "WAKE")
    assert decided.signal_ids == [s.signal_id for s in signals]
    assert (decided.probabilities, decided.threshold, decided.model, decided.error) == (None, None, None, None)
    model_config = GateConfig()
    model = DecisionGate(model_config, lambda request: result(0.3), budget=running_budget(model_config))
    assert (scored := model.decide(request_state, 100)).source == "model"
    assert (scored.decision, scored.reason_code) == ("SKIP", reason)

    def fail(request):
        raise RuntimeError("internal client diagnostics")

    fallback = DecisionGate(model_config, fail, budget=running_budget(model_config))
    scored = fallback.decide(request_state, 100)
    assert (scored.decision, scored.reason_code, scored.source, scored.error) == (decision, reason, "fallback", "transport")


@pytest.mark.parametrize("constraint", ["run_budget", "target_cap", "step_interval", "time_interval", "finished", "unregistered"])
def test_budget_precedes_model_call(constraint):
    config = GateConfig(
        wake_budget=1 if constraint == "run_budget" else 8,
        per_worker_wake_cap=1 if constraint == "target_cap" else 4,
    )
    budget = running_budget(config)
    if constraint == "run_budget":
        assert budget.record_wake("agent2", 10)
    if constraint in ("target_cap", "step_interval"):
        assert budget.record_wake("agent1", 10, step=12)
        if constraint == "target_cap":
            budget.update_worker("agent1", step=16, status="running")
    if constraint == "time_interval":
        assert budget.record_wake("agent2", 71)
    if constraint == "finished":
        budget.update_worker("agent1", step=15, status="submitted")
    target = "agent3" if constraint == "unregistered" else "agent1"
    calls = []
    gate = DecisionGate(config, lambda request: calls.append(request) or result(), budget=budget)
    remaining = budget.remaining
    decided = gate.decide(state(budget, signals=[signal("NO_PROGRESS_FAILING", "hard")], target=target), 100)
    assert (decided.decision, decided.source, decided.reason_code) == ("SKIP", "budget", "NONE")
    assert decided.budget_remaining_after == remaining
    assert decided.model is None
    assert calls == []


def test_budget_allows_exact_step_and_second_boundaries():
    budget = running_budget()
    assert budget.record_wake("agent1", 60, step=9)
    assert budget.record_wake("agent1", 90, step=13)
    assert budget.allows("agent2", 120)
    assert not budget.allows("agent1", 120, step=16)
    assert budget.allows("agent1", 120, step=17)
    assert budget.since_last_intervention("agent1", step=17, now=120) == {"steps": 4, "seconds": 30}


def test_model_receives_all_candidate_kinds_without_rule_filter_or_cache():
    config = GateConfig()
    budget = running_budget(config)
    calls = []
    gate = DecisionGate(config, lambda request: calls.append(request) or result(0.49, chosen="WAKE"), budget=budget)
    states = [
        state(budget, signals=[signal("NO_PROGRESS_FAILING")]),
        state(budget, "peer_message"),
        state(budget, "heartbeat"),
    ]
    for item in [*states, states[2]]:
        score = gate.decide(item, 100)
        assert score.decision == "SKIP"
        assert score.source == "model"
        assert score.probabilities == {"WAKE": 0.49, "SKIP": 0.51}
    assert [request.state_text for request in calls] == [f"{GATE_INSTRUCTIONS}\n\n{to_json(item)}" for item in [*states, states[2]]]
    assert all(request.prompt_id == GATE_PROMPT_ID for request in calls)


def test_custom_gate_texts_reach_decision_request_and_prompt_ids():
    config = GateConfig()
    budget = running_budget(config)
    calls = []
    texts = dict(instructions="Gate fixture", question="Question fixture", wake="Wake fixture", skip="Skip fixture")
    gate = DecisionGate(config, lambda request: calls.append(request) or result(0.49), budget=budget, **texts)
    candidate = state(budget, "heartbeat")
    decision = gate.decide(candidate, 100)
    (request,) = calls
    (question,) = request.questions
    assert request.state_text == f"Gate fixture\n\n{to_json(candidate)}"
    assert question.instructions == "Question fixture"
    assert question.criteria == {"WAKE": "Wake fixture", "SKIP": "Skip fixture"}
    assert request.prompt_id == decision.prompt_id == gate_prompt_id(**texts)
    assert RuleGate(RULE, **texts).prompt_id == request.prompt_id
    assert RuleGate(RULE).prompt_id == GATE_PROMPT_ID


@pytest.mark.parametrize(("threshold", "probability", "decision"), [(0.5, 0.49, "SKIP"), (0.5, 0.50, "WAKE"), (0.0, 0.0, "WAKE")])
def test_probability_threshold_ignores_endpoint_choice(threshold, probability, decision):
    config = GateConfig(wake_threshold=threshold)
    budget = running_budget(config)
    gate = DecisionGate(config, lambda request: result(probability), budget=budget)
    scored = gate.decide(state(budget, "heartbeat"), 100, candidate_id="candidate-7")
    assert (scored.decision, scored.source, scored.candidate_id) == (decision, "model", "candidate-7")
    assert (scored.threshold, scored.model, scored.input_tokens, scored.completion_tokens) == (threshold, config.model, 734, 0)


@pytest.mark.parametrize(
    "probabilities",
    [
        {},
        {"decision": {"WAKE": 1}},
        {"decision": {"WAKE": 0.5, "SKIP": 0.5, "HOLD": 0}},
        {"decision": {"WAKE": 1.1, "SKIP": 0}},
        {"decision": {"WAKE": -0.1, "SKIP": 1}},
        {"decision": {"WAKE": 0.6, "SKIP": 0.6}},
        {"decision": {"WAKE": float("nan"), "SKIP": 0.5}},
        {"decision": {"WAKE": float("inf"), "SKIP": 0.5}},
        {"decision": {"WAKE": True, "SKIP": 0}},
        {"decision": {"WAKE": "0.5", "SKIP": 0.5}},
    ],
)
def test_invalid_model_probabilities_use_rule_fallback(probabilities):
    config = GateConfig()
    budget = running_budget(config)
    gate = DecisionGate(config, lambda request: replace(result(), probabilities=probabilities), budget=budget)
    scored = gate.decide(state(budget, signals=[signal("HUNK_CHURN")]), 100)
    assert (scored.decision, scored.source, scored.error) == ("WAKE", "fallback", "invalid_response")
    assert scored.probabilities is None


@pytest.mark.parametrize("probabilities", [{"WAKE": 0.50, "SKIP": 0.501}, {"WAKE": 0.50, "SKIP": 0.499}])
def test_valid_probabilities_are_renormalized_before_threshold(probabilities):
    config = GateConfig()
    budget = running_budget(config)
    gate = DecisionGate(config, lambda request: replace(result(), probabilities={"decision": probabilities}), budget=budget)
    scored = gate.decide(state(budget, "heartbeat"), 100)
    assert scored.source == "model"
    assert scored.probabilities == pytest.approx({k: v / sum(probabilities.values()) for k, v in probabilities.items()})
    assert scored.decision == ("WAKE" if probabilities["SKIP"] < 0.5 else "SKIP")


def test_gate_does_not_wait_for_late_completion_or_charge_late_wake():
    started = Event()
    release = Event()

    def complete(request):
        if not started.is_set():
            started.set()
            release.wait(timeout=2)
            return result(1)
        return result(0)

    config = GateConfig(timeout_s=0.05)
    budget = running_budget(config)
    gate = DecisionGate(config, complete, budget=budget)
    try:
        before = time.monotonic()
        scored = gate.decide(state(budget, "heartbeat"), 100)
        assert time.monotonic() - before < 1
        assert started.is_set()
        assert (scored.decision, scored.source, scored.error) == ("SKIP", "fallback", "timeout")
        next_score = gate.decide(state(budget, "heartbeat"), 101)
        assert (next_score.decision, next_score.source, next_score.error) == ("SKIP", "model", None)
    finally:
        release.set()
    assert budget.remaining == config.wake_budget


def test_budget_rechecks_worker_status_after_model_call():
    config = GateConfig()
    budget = running_budget(config)

    def complete(request):
        budget.update_worker("agent1", step=15, status="finished")
        return result(1)

    gate = DecisionGate(config, complete, budget=budget)
    scored = gate.decide(state(budget, "heartbeat"), 100)
    assert (scored.decision, scored.source, scored.reason_code) == ("SKIP", "budget", "NONE")
    assert budget.remaining == config.wake_budget


@contextmanager
def decision_server(body, *, status=200, delay=0):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            received.append(
                {
                    "path": self.path,
                    "auth_matches": self.headers["Authorization"] == f"Bearer {KEY}",
                    "content_type": self.headers["Content-Type"],
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            time.sleep(delay)
            self.send_response(status)
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:  # client timed out
                pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as srv:
        thread = Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{srv.server_port}/api/alpha", received
        finally:
            srv.shutdown()
            thread.join()


def test_http_decisions_wire_format_and_result_mapping(monkeypatch):
    monkeypatch.setenv("STUB_GATE_KEY", KEY)
    with decision_server(json.dumps(load("http_response")).encode()) as (url, received):
        config = GateConfig(base_url=url, api_key_env="STUB_GATE_KEY")
        budget = running_budget(config)
        client = HttpDecisionClient(config)
        results, request_rows = [], []

        def complete(request):
            request_rows.append(request)
            results.append(client(request))
            return results[-1]

        gate = DecisionGate(config, complete, budget=budget)
        request_state = state(budget, "heartbeat")
        scored = gate.decide(request_state, 100, candidate_id="c-42")
    expected = load("http_request")
    expected["body"]["state"] = f"{GATE_INSTRUCTIONS}\n\n{to_json(request_state)}"
    assert received == [expected]
    assert KEY not in json.dumps(received)
    assert KEY not in repr(request_rows)
    assert KEY not in to_json(scored)
    answer = results[0]
    assert answer == replace(
        result(0.78, chosen="WAKE"),
        latency_ms=answer.latency_ms,
        response_id="gen-dec-example",
        probabilities={"decision": {"WAKE": 0.78, "SKIP": 0.22}},
    )
    assert (scored.decision, scored.source, scored.reason_code) == ("WAKE", "model", "HEARTBEAT_PATTERN")
    assert scored.prompt_id == GATE_PROMPT_ID


@pytest.mark.parametrize(
    ("status", "response", "delay", "expected"),
    [
        (200, b"{}", 0.25, "timeout"),
        (302, b"{}", 0, "http_302"),
        (429, b"{}", 0, "http_429"),
        (500, b"{}", 0, "http_500"),
        (201, b"{}", 0, "http_201"),
        (200, b"not JSON", 0, "invalid_response"),
        (200, {}, 0, "invalid_response"),
        (200, {"answers": {}}, 0, "invalid_response"),
        (200, {"answers": {"decision": {"type": "refusal"}}}, 0, "invalid_response"),
        (200, {"answers": {"decision": {"type": "text"}}}, 0, "invalid_response"),
        (200, {"answers": {"decision": {"type": "choice", "choice": "HOLD"}}}, 0, "invalid_response"),
        (200, {"answers": {"decision": {"type": "choice", "choice": "WAKE", "probabilities": {"WAKE": 1}}}}, 0, "invalid_response"),
    ],
)
def test_http_decisions_errors(monkeypatch, status, response, delay, expected):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    body = response if isinstance(response, bytes) else json.dumps(response).encode()
    with decision_server(body, status=status, delay=delay) as (url, received):
        config = GateConfig(base_url=url, timeout_s=0.1 if delay else 1)
        budget = running_budget(config)
        gate = DecisionGate(config, budget=budget)
        scored = gate.decide(state(budget, "peer_message"), 100)
        assert len(received) == 1
    assert (scored.decision, scored.source, scored.error) == ("WAKE", "fallback", expected)


def test_http_connection_failure_maps_to_transport(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = GateConfig(base_url=f"http://127.0.0.1:{port}/api/alpha")
    budget = running_budget(config)
    gate = DecisionGate(config, budget=budget)
    scored = gate.decide(state(budget, "heartbeat"), 100)
    assert (scored.decision, scored.source, scored.error) == ("SKIP", "fallback", "transport")


def test_random_gate_schedule_and_deferred_wakes():
    fixture = load("random_schedule")
    config = GateConfig(policy="random", **fixture["config"])

    def play(until, unavailable_steps):
        budget = running_budget(config)
        gate = RandomGate(config, **fixture["pair"], budget=budget)
        assert list(gate.schedule) == fixture["draws"]
        wakes = []
        for step in range(1, until + 1):
            budget.update_worker("agent1", step=step, status="paused" if step in unavailable_steps else "running")
            decision = gate.decide(state(budget, "heartbeat"), now=100 + step, team_step=step)
            if decision.decision == "WAKE":
                assert (decision.source, decision.reason_code) == ("random", "RANDOM_SCHEDULE")
                wakes.append(step)
            elif step in unavailable_steps:
                assert (decision.source, decision.reason_code) == ("budget", "NONE")
        return wakes, gate.random_unused

    assert play(config.random_step_horizon, []) == (fixture["draws"], 0)
    assert play(config.random_step_horizon, []) == (fixture["draws"], 0)
    assert play(fixture["partial_stop"], fixture["unavailable_steps"]) == (fixture["deferred"], fixture["unused"])
    with pytest.raises(ValueError, match="requires a matched agent_main result"):
        RandomGate(GateConfig(policy="random"), **fixture["pair"])


def test_gate_state_from_recorded_det_main_keeps_six_commands_and_omits_raw_outputs():
    recorded = json.loads((FIXTURES / "signals" / "messages_det_main.json").read_text())
    records = {worker: parse_steps(worker, messages, start=0, config=SignalConfig()) for worker, messages in recorded["workers"].items()}
    budget = running_budget()
    gate_state = build_gate_state(
        "agent1",
        "heartbeat",
        [],
        records["agent1"],
        budget=budget,
        now=100,
        steps_remaining=99,
        seconds_remaining=240.0,
        peers=[{"id": "agent2", "status": "running", "steps": len(records["agent2"]), "active_signals": [], "raw_output": KEY}],
    )
    serialized = to_json(gate_state)
    assert serialized == (FIXTURES / "gate" / "det_main_state.json").read_text().strip()
    assert len(serialized.encode()) <= 3000
    assert len(gate_state.recent_steps) == 6
    assert any(len(step["command"]) == 160 for step in gate_state.recent_steps)
    assert KEY not in serialized and "raw_output" not in serialized and "output_sha256" not in serialized
    output = next(m["extra"]["raw_output"] for m in recorded["workers"]["agent1"] if len(m["extra"].get("raw_output", "")) > 100)
    assert json.dumps(output)[1:-1] not in serialized


def test_gate_module_does_not_import_harness():
    tree = ast.parse(Path(system1_gate.__file__).read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and "harness" in (node.module or "")
        or isinstance(node, ast.Import)
        and any("harness" in name.name for name in node.names)
        for node in ast.walk(tree)
    )
