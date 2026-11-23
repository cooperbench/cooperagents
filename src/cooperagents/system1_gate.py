"""System-1 policies, shared wake constraints, and the Decisions API client."""

from __future__ import annotations

import json
import math
import os
import random
from concurrent.futures import Future
from hashlib import sha256
from http.client import HTTPException
from threading import Lock, Thread
from time import monotonic
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cooperagents.agent_coordination_types import (
    CandidateKind,
    Decision,
    DecisionComplete,
    DecisionQuestion,
    DecisionRequest,
    DecisionResult,
    DecisionSource,
    DiffProbe,
    GateConfig,
    GateDecision,
    GateState,
    ReasonCode,
    Signal,
    SignalKind,
    StepRecord,
    to_json,
)
from cooperagents.agent_coordinator_prompts import (
    DECISION_QUESTION_INSTRUCTIONS,
    GATE_INSTRUCTIONS,
    SKIP_CRITERION,
    WAKE_CRITERION,
    gate_prompt_id,
)
from cooperagents.structural_signals import signal_priority

_REASON_CODES: dict[SignalKind, ReasonCode] = {
    "NO_PROGRESS_FAILING": "NO_PROGRESS",
    "HUNK_CHURN": "HUNK_CHURN",
    "TEST_REGRESSION": "TEST_REGRESSION",
    "PEER_OVERLAP": "PEER_OVERLAP",
    "SUBMIT_REJECTED": "SUBMIT_REJECTED",
    "PEER_MESSAGE": "PEER_MESSAGE",
}


def reason_code(state: GateState) -> ReasonCode:
    if state.candidate_kind == "peer_message":
        return "PEER_MESSAGE"
    if state.candidate_kind == "heartbeat":
        return "HEARTBEAT_PATTERN"
    candidates = [signal for signal in state.signals if signal.kind in _REASON_CODES]
    return _REASON_CODES[min(candidates, key=signal_priority).kind] if candidates else "NONE"


class WakeBudget:
    """Share one wake budget between gate decisions for all workers in a run."""

    def __init__(self, config: GateConfig, *, started_at: float | None = None) -> None:
        self.config = config
        self.started_at = started_at
        self._lock = Lock()
        self._worker_progress: dict[str, tuple[int, str]] = {}
        self._target_wakes: dict[str, int] = {}
        self._target_last_wake: dict[str, tuple[int, float]] = {}
        self._total = 0
        self._last_wake: float | None = None

    def update_worker(self, target: str, *, step: int, status: str) -> None:
        with self._lock:
            self._worker_progress[target] = (step, status)

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.config.wake_budget - self._total)

    def target_remaining(self, target: str) -> int:
        with self._lock:
            return max(0, self.config.per_worker_wake_cap - self._target_wakes.get(target, 0))

    def since_last_intervention(self, target: str, *, step: int, now: float) -> dict[str, int | float]:
        with self._lock:
            previous = self._target_last_wake.get(target)
        if previous is None:
            return {"steps": step, "seconds": max(0.0, now - self.started_at) if self.started_at is not None else 0.0}
        last_step, last_time = previous
        return {"steps": max(0, step - last_step), "seconds": max(0.0, now - last_time)}

    def _check(self, target: str, step: int | None, status: str | None, now: float) -> tuple[bool, int]:
        registered_step, registered_status = self._worker_progress.get(target, (0, "unknown"))
        target_step = registered_step if step is None else step
        target_status = registered_status if status is None else status
        last_target = self._target_last_wake.get(target)
        allowed = (
            target_status == "running"
            and self._total < self.config.wake_budget
            and self._target_wakes.get(target, 0) < self.config.per_worker_wake_cap
            and (last_target is None or target_step - last_target[0] >= self.config.min_steps_between_wakes)
            and (self._last_wake is None or now - self._last_wake >= self.config.min_seconds_between_wakes)
        )
        return allowed, target_step

    def allows(self, target: str, now: float, *, step: int | None = None, status: str | None = None) -> bool:
        with self._lock:
            return self._check(target, step, status, now)[0]

    def record_wake(self, target: str, now: float, *, step: int | None = None, status: str | None = None) -> bool:
        """Consume one wake if the target is still eligible."""
        with self._lock:
            allowed, target_step = self._check(target, step, status, now)
            if allowed:
                self._target_wakes[target] = self._target_wakes.get(target, 0) + 1
                self._target_last_wake[target] = (target_step, now)
                self._total += 1
                self._last_wake = now
            return allowed


def build_gate_state(
    target: str,
    candidate_kind: CandidateKind,
    signals: list[Signal],
    records: list[StepRecord],
    *,
    budget: WakeBudget,
    now: float,
    target_step: int | None = None,
    steps_remaining: int = 0,
    seconds_remaining: float = 0.0,
    peers: list[dict[str, Any]] | None = None,
    probe: DiffProbe | None = None,
) -> GateState:
    """Build the canonical model input from chronological records for the target."""
    latest = records[-1] if records else None
    step = target_step if target_step is not None else latest.step if latest else 0
    fingerprint = probe.fingerprint if probe is not None else latest.fingerprint if latest else None
    unchanged = 0
    previous_step = step
    prior_records = records[:-1] if latest is not None and latest.step == step else records
    for record in reversed(prior_records):
        if fingerprint is None or record.fingerprint != fingerprint or record.step != previous_step - 1:
            break
        unchanged += 1
        previous_step = record.step
    return GateState(
        target=target,
        candidate_kind=candidate_kind,
        signals=list(signals),
        recent_steps=[
            {
                "step": record.step,
                "command": record.command[:160],
                "returncode": record.returncode,
                "is_test": record.is_test,
                "diff_added": record.diff_added,
                "diff_deleted": record.diff_deleted,
            }
            for record in records[-6:]
        ],
        diff={
            "files": len(probe.hunks) if probe is not None else len(latest.hunks) if latest else 0,
            "added": probe.added if probe is not None else latest.diff_added if latest else 0,
            "deleted": probe.deleted if probe is not None else latest.diff_deleted if latest else 0,
            "unchanged_for_steps": unchanged,
        },
        since_last_intervention=budget.since_last_intervention(target, step=step, now=now),
        budget={
            "remaining": budget.remaining,
            "target_remaining": budget.target_remaining(target),
            "steps_remaining": steps_remaining,
            "seconds_remaining": seconds_remaining,
        },
        peers=[
            {"id": peer["id"], "status": peer["status"], "steps": peer["steps"], "active_signals": list(peer.get("active_signals", []))}
            for peer in sorted(peers or [], key=lambda peer: peer["id"])
        ],
    )


class DecisionClientError(Exception):
    """A stable error code suitable for recording without client diagnostics."""


def _probabilities(raw: Any, question: DecisionQuestion) -> dict[str, float]:
    if not isinstance(raw, dict) or raw.keys() != question.criteria.keys():
        raise DecisionClientError("invalid_response")
    # The range comparison also excludes infinities and NaN.
    if any(type(p) not in (int, float) or not 0 <= p <= 1 for p in raw.values()):
        raise DecisionClientError("invalid_response")
    total = math.fsum(raw.values())
    # Account for the rounding of float subtraction at the tolerance boundary.
    if not 1 - 1e-3 <= total <= 1 + 1e-3:
        raise DecisionClientError("invalid_response")
    return {option: raw[option] / total for option in question.criteria}


def _choice(value: Any, question: DecisionQuestion) -> str:
    if not isinstance(value, str) or value not in question.criteria:
        raise DecisionClientError("invalid_response")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an authenticated request to a different endpoint.
        return None


class HttpDecisionClient:
    """Call OpenRouter's choice-only Decisions API with no retries."""

    def __init__(self, config: GateConfig) -> None:
        if not config.model or not config.base_url or not config.api_key_env or config.timeout_s is None:
            raise ValueError("decision client requires a model, base URL, API key variable, and timeout")
        self.model = config.model
        self.url = f"{config.base_url}/decisions"
        self.api_key_env = config.api_key_env
        self.timeout_s = config.timeout_s
        self._opener = build_opener(_NoRedirect())
        self._opener.addheaders = []

    def __call__(self, request: DecisionRequest) -> DecisionResult:
        started = monotonic()
        body = {
            "model": self.model,
            "state": request.state_text,
            "questions": {
                question.id: {"type": "choice", "instructions": question.instructions, "criteria": question.criteria}
                for question in request.questions
            },
        }
        try:
            key = os.environ[self.api_key_env]
            if not key:
                raise KeyError(self.api_key_env)
            http_request = Request(
                self.url,
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with self._opener.open(http_request, timeout=self.timeout_s) as response:
                if response.status != 200:
                    raise DecisionClientError(f"http_{response.status}")
                raw = response.read()
        except HTTPError as error:
            error.close()
            raise DecisionClientError(f"http_{error.code}") from None
        except URLError as error:
            raise DecisionClientError("timeout" if isinstance(error.reason, TimeoutError) else "transport") from None
        except TimeoutError:
            raise DecisionClientError("timeout") from None
        except (OSError, HTTPException, KeyError, ValueError):
            raise DecisionClientError("transport") from None

        try:
            payload = json.loads(raw)
            answers = payload["answers"]
            probabilities, chosen = {}, {}
            for question in request.questions:
                answer = answers[question.id]
                if answer["type"] != "choice":
                    raise DecisionClientError("invalid_response")
                chosen[question.id] = _choice(answer["choice"], question)
                _probabilities(answer["probabilities"], question)
                probabilities[question.id] = answer["probabilities"]
            usage = payload.get("usage") or {}
            input_tokens = usage.get("input_tokens")
            completion_tokens = usage.get("output_tokens")
            cost = usage.get("cost")
            if any(value is not None and (type(value) is not int or value < 0) for value in (input_tokens, completion_tokens)):
                raise DecisionClientError("invalid_response")
            if cost is not None and (type(cost) not in (float, int) or not math.isfinite(cost) or cost < 0):
                raise DecisionClientError("invalid_response")
            model, provider, response_id = (payload.get(field) for field in ("model", "provider", "id"))
            if any(value is not None and not isinstance(value, str) for value in (model, provider, response_id)):
                raise DecisionClientError("invalid_response")
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
            raise DecisionClientError("invalid_response") from None

        return DecisionResult(
            probabilities=probabilities,
            chosen=chosen,
            latency_ms=(monotonic() - started) * 1000,
            input_tokens=input_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            raw_model=model,
            provider=provider,
            response_id=response_id,
        )


def _rule_decision(state: GateState) -> Decision:
    if state.candidate_kind != "heartbeat" and (
        state.candidate_kind == "peer_message"
        or any(signal.severity == "hard" or signal.kind in ("HUNK_CHURN", "PEER_MESSAGE") for signal in state.signals)
    ):
        return "WAKE"
    return "SKIP"


class RuleGate:
    """Decide from the structural signals after applying the shared constraints."""

    def __init__(self, config: GateConfig, budget: WakeBudget | None = None, *, instructions: str = GATE_INSTRUCTIONS) -> None:
        self.budget = budget if budget is not None else WakeBudget(config)
        self.prompt_id = gate_prompt_id(instructions)
        self._candidate_number = 0

    def _decision(
        self,
        state: GateState,
        now: float,
        *,
        candidate_id: str | None,
        target_step: int | None,
        target_status: str | None,
        decision: Decision,
        source: DecisionSource,
        started: float,
        result: DecisionResult | None = None,
        probabilities: dict[str, float] | None = None,
        threshold: float | None = None,
        model: str | None = None,
        error: str | None = None,
    ) -> GateDecision:
        if decision == "WAKE" and not self.budget.record_wake(state.target, now, step=target_step, status=target_status):
            # The worker can finish while the model call is in flight.
            decision, source = "SKIP", "budget"
            result = probabilities = threshold = model = error = None
        self._candidate_number += 1
        reason: ReasonCode = reason_code(state)
        if source == "budget":
            reason = "NONE"
        elif source == "random":
            reason = "RANDOM_SCHEDULE"
        return GateDecision(
            candidate_id=candidate_id if candidate_id is not None else f"c-{self._candidate_number:04d}",
            candidate_kind=state.candidate_kind,
            decision=decision,
            reason_code=reason,
            target=state.target,
            signal_ids=[signal.signal_id for signal in state.signals],
            source=source,
            probabilities=probabilities,
            threshold=threshold,
            model=model,
            latency_ms=(monotonic() - started) * 1000,
            input_tokens=result.input_tokens if result is not None else None,
            completion_tokens=result.completion_tokens if result is not None else None,
            prompt_id=self.prompt_id,
            budget_remaining_after=self.budget.remaining,
            error=error,
        )

    def decide(
        self,
        state: GateState,
        now: float,
        *,
        candidate_id: str | None = None,
        target_step: int | None = None,
        target_status: str | None = None,
    ) -> GateDecision:
        started = monotonic()
        allowed = self.budget.allows(state.target, now, step=target_step, status=target_status)
        return self._decision(
            state,
            now,
            candidate_id=candidate_id,
            target_step=target_step,
            target_status=target_status,
            decision=_rule_decision(state) if allowed else "SKIP",
            source="rule" if allowed else "budget",
            started=started,
        )


class DecisionGate(RuleGate):
    """Score each eligible candidate, with the rule policy as a fail-safe."""

    def __init__(
        self,
        config: GateConfig,
        complete: DecisionComplete | None = None,
        *,
        budget: WakeBudget | None = None,
        instructions: str = GATE_INSTRUCTIONS,
    ) -> None:
        super().__init__(config, budget, instructions=instructions)
        if not config.model or config.wake_threshold is None or config.timeout_s is None:
            raise ValueError("model gate requires a model, threshold, and timeout")
        self.model = config.model
        self.threshold = config.wake_threshold
        self.timeout_s = config.timeout_s
        self.instructions = instructions
        self.complete = complete if complete is not None else HttpDecisionClient(config)

    def _complete_before_timeout(self, request: DecisionRequest) -> DecisionResult:
        future: Future[DecisionResult] = Future()

        def invoke() -> None:
            try:
                future.set_result(self.complete(request))
            except BaseException as exc:
                future.set_exception(exc)

        # A timed-out call must not block later candidates or process shutdown.
        Thread(target=invoke, daemon=True, name="system1-decision").start()
        return future.result(timeout=self.timeout_s)

    def decide(
        self,
        state: GateState,
        now: float,
        *,
        candidate_id: str | None = None,
        target_step: int | None = None,
        target_status: str | None = None,
    ) -> GateDecision:
        started = monotonic()
        if not self.budget.allows(state.target, now, step=target_step, status=target_status):
            return self._decision(
                state,
                now,
                candidate_id=candidate_id,
                target_step=target_step,
                target_status=target_status,
                decision="SKIP",
                source="budget",
                started=started,
            )
        question = DecisionQuestion("decision", DECISION_QUESTION_INSTRUCTIONS, {"WAKE": WAKE_CRITERION, "SKIP": SKIP_CRITERION})
        request = DecisionRequest(f"{self.instructions}\n\n{to_json(state)}", (question,), self.prompt_id)
        result: DecisionResult | None = None
        probabilities: dict[str, float] | None = None
        error: str | None = None
        try:
            result = self._complete_before_timeout(request)
        except TimeoutError:
            error = "timeout"
        except DecisionClientError as exc:
            code = str(exc)
            is_http_error = code.startswith("http_") and code[5:].isdigit()
            error = code if is_http_error or code in ("timeout", "transport", "invalid_response") else "transport"
        except Exception:
            error = "transport"
        if error is None:
            try:
                if not isinstance(result, DecisionResult):
                    result = None
                    raise DecisionClientError("invalid_response")
                _choice(result.chosen[question.id], question)
                probabilities = _probabilities(result.probabilities[question.id], question)
            except (AttributeError, TypeError, KeyError, DecisionClientError):
                error = "invalid_response"

        source: DecisionSource
        if probabilities is None:
            decision, source = _rule_decision(state), "fallback"
        else:
            decision, source = ("WAKE" if probabilities["WAKE"] >= self.threshold else "SKIP"), "model"
        return self._decision(
            state,
            now,
            candidate_id=candidate_id,
            target_step=target_step,
            target_status=target_status,
            decision=decision,
            source=source,
            started=started,
            result=result,
            probabilities=probabilities,
            threshold=self.threshold,
            model=self.model,
            error=error,
        )


class RandomGate(RuleGate):
    """Draw a matched schedule and defer each blocked wake to an eligible team step."""

    def __init__(
        self,
        config: GateConfig,
        *,
        repo: str,
        task_id: str | int,
        features: str | tuple[int, ...] | list[int],
        rollout_index: int,
        budget: WakeBudget | None = None,
    ) -> None:
        super().__init__(config, budget)
        wakes, horizon = config.random_wakes, config.random_step_horizon
        if wakes is None or horizon is None:
            raise ValueError("random gate requires a matched agent_main result")
        if not 0 <= wakes <= horizon:
            raise ValueError("random wakes must be between zero and the matched step horizon")
        seed = sha256(f"{config.random_seed}:{repo}:{task_id}:{features}:{rollout_index}".encode()).digest()
        self.schedule = tuple(sorted(random.Random(int.from_bytes(seed, "big")).sample(range(1, horizon + 1), wakes)))
        self._used = 0
        self._team_step = 0

    @property
    def random_unused(self) -> int:
        return len(self.schedule) - self._used

    def decide(
        self,
        state: GateState,
        now: float,
        *,
        candidate_id: str | None = None,
        target_step: int | None = None,
        target_status: str | None = None,
        team_step: int | None = None,
    ) -> GateDecision:
        started = monotonic()
        step = self._team_step + 1 if team_step is None else team_step
        due = step > self._team_step and self.random_unused > 0 and self.schedule[self._used] <= step
        self._team_step = max(self._team_step, step)
        allowed = self.budget.allows(state.target, now, step=target_step, status=target_status)
        decision = self._decision(
            state,
            now,
            candidate_id=candidate_id,
            target_step=target_step,
            target_status=target_status,
            decision="WAKE" if allowed and due else "SKIP",
            source="random" if allowed else "budget",
            started=started,
        )
        if decision.decision == "WAKE":
            self._used += 1
        return decision
