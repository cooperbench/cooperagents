"""Read-only, event-driven coordinator for workers in separate environments."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from functools import cached_property, partial
from hashlib import sha256
from queue import Empty, Queue
from statistics import quantiles
from typing import Any, Protocol, TypeVar, cast
from urllib.parse import urlsplit, urlunsplit

from cooperagents.agent_coordination_types import (
    AGENT_COORDINATOR_METRICS_SCHEMA,
    END_WAKE_TOOL,
    GIT_SHOW_TOOL,
    LIST_DIR_TOOL,
    READ_FILE_TOOL,
    SEARCH_CODE_TOOL,
    SEND_MESSAGE_TOOL,
    SPEC_VERDICT_TOOL,
    SUBMIT_PLAN_TOOL,
    AgentComplete,
    AgentCoordinatorConfig,
    CandidateKind,
    DecisionComplete,
    DecisionRequest,
    DecisionResult,
    DiffProbe,
    GateConfig,
    GateDecision,
    GateState,
    OpeningPlan,
    Signal,
    SignalConfig,
    SpecFinding,
    SpecVerdict,
    StepRecord,
    from_json,
    to_json,
)
from cooperagents.agent_coordinator_prompts import (
    GATE_INSTRUCTIONS,
    OPENING_PLAN_PROMPT,
    SPEC_CHECK_PROMPT,
    WAKE_PROMPT,
    gate_prompt_id,
    prompt_id,
)
from cooperagents.bus.base import TeamBus
from cooperagents.checkpoint import complete_output
from cooperagents.env.base import Environment
from cooperagents.git_snapshots import GitEvidence
from cooperagents.patching import strip_test_sections
from cooperagents.sampling import sampling_kwargs
from cooperagents.trajectory import Trajectory, record_call
from cooperagents.types import Assignment

_COORDINATOR = "coordinator"
_GATE = "coordinator_gate"
_SNAPSHOT_REPO = "/cbshared/repo.git"
_READ_TOOLS = [LIST_DIR_TOOL, READ_FILE_TOOL, SEARCH_CODE_TOOL, GIT_SHOW_TOOL]
_WAKING_SIGNALS = {"NO_PROGRESS_FAILING", "HUNK_CHURN", "TEST_REGRESSION", "PEER_OVERLAP", "SUBMIT_REJECTED"}
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONFLICT = re.compile(r"^\+(?:<<<<<<< |=======\r?$|>>>>>>> )", re.MULTILINE)
_CLIP = "\n...[clipped]...\n"
_T = TypeVar("_T")


class Tracker(Protocol):
    def observe(self, records: list[StepRecord]) -> list[Signal]: ...

    def submit_attempt(self, aid: str, error: str | None) -> list[Signal]: ...


class GatePolicy(Protocol):
    def decide(self, state: GateState, *, candidate_id: str, team_step: int) -> GateDecision: ...


class TeammatePoller(Protocol):
    def poll(self) -> str: ...


class WorkerMessages(Protocol):
    messages: list[dict]


class ParseSteps(Protocol):
    def __call__(self, worker: str, messages: list[dict], *, start: int, config: SignalConfig) -> list[StepRecord]: ...


def _bounded_call(function: Callable[[], _T], deadline: float, stop: threading.Event | None = None) -> _T:
    """Keep the caller interruptible even if an injected transport ignores its timeout."""
    if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
        raise TimeoutError("coordinator call cancelled or out of time")
    result: Queue[tuple[_T | None, Exception | None]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put_nowait((function(), None))
        except Exception as exc:
            result.put_nowait((None, exc))

    threading.Thread(target=invoke, daemon=True, name="agent-coordinator-io").start()
    while (remaining := deadline - time.monotonic()) > 0 and (stop is None or not stop.is_set()):
        try:
            value, error = result.get(timeout=min(remaining, 0.05) if stop is not None else remaining)
        except Empty:
            continue
        if error is not None:
            raise error
        # The success arm of the queue always holds the function's value.
        return cast(_T, value)
    raise TimeoutError("coordinator call cancelled or out of time")


def _path(value: object, *, root: bool = False) -> str:
    if (
        not isinstance(value, str)
        or value.startswith(("/", "\\", ".cb_"))
        or re.match(r"^[a-zA-Z]:", value)
        or any(part in value for part in ("..", "\x00", "\r", "\n"))
        or any(part.startswith(".cb_") for part in value.split("/"))
    ):
        raise ValueError("invalid repository path")
    if root and value in ("", ".", "./"):
        return ""
    value = value.removeprefix("./")
    if not value or value == ".":
        raise ValueError("repository path is required")
    return value


def _clip(text: str, size: int) -> str:
    if len(text) <= size:
        return text
    half = (size - len(_CLIP)) // 2
    return text[:half] + _CLIP + text[-(size - len(_CLIP) - half):]


def _safe_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    url = urlsplit(base_url)
    return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))


class ReadOnlyGit:
    """Translate validated function arguments into fixed Git object queries."""

    def __init__(self, env: Environment, base_sha: str, *, snapshot_repo: str = _SNAPSHOT_REPO) -> None:
        if not _SHA.fullmatch(base_sha):
            raise ValueError("invalid base SHA")
        self.env = env
        self.base_sha = base_sha
        self.snapshot_repo = snapshot_repo

    def read(
        self, name: str, args: dict, *, evidence: GitEvidence, worker: str | None = None, timeout: int = 60,
    ) -> str:
        snapshot = evidence.workers.get(worker) if worker is not None else None
        rev = args.get("rev") if name == "git_show" else (snapshot.sha if snapshot else self.base_sha)
        if not isinstance(rev, str) or not _SHA.fullmatch(rev) or (
            rev != self.base_sha and rev not in {item.sha for item in evidence.workers.values()}
        ):
            raise ValueError("revision is outside the available snapshots")

        def git(*parts: str, literal: bool = True) -> str:
            options = ["--no-pager"]
            if literal:
                options.append("--literal-pathspecs")
            if rev != self.base_sha or (snapshot is not None and name != "git_show"):
                options.append(f"--git-dir={self.snapshot_repo}")
            return " ".join(shlex.quote(part) for part in ["git", *options, *parts])

        accepted: tuple[int, ...] = (0,)
        limit = ""
        chars = None
        if name == "list_dir":
            path = _path(args.get("path"), root=True).rstrip("/")
            tree = f"{rev}:{path}" if path else rev
            command = git("ls-tree", "--name-only", tree, "--")
            limit = " | sed -n '1,400p'"
        elif name == "read_file":
            path = _path(args.get("path"))
            start, end = args.get("start"), args.get("end")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 2_147_483_647:
                raise ValueError("invalid line range")
            command = git("show", "--no-ext-diff", "--no-textconv", f"{rev}:{path}")
            limit = " | sed -n " + shlex.quote(f"{start},{min(end, start + 399)}p")
            chars = 16000
        elif name == "search_code":
            pattern, glob = args.get("pattern"), _path(args.get("path_glob"), root=True)
            if not isinstance(pattern, str) or any(char in pattern for char in "\x00\r\n"):
                raise ValueError("invalid fixed search pattern")
            # Prefix the pathspec explicitly, so a model cannot select pathspec magic.
            paths = (f":(glob){glob}",) if glob else ()
            command = git("grep", "-n", "-F", "-I", "--max-count=20", "-e", pattern, rev, "--", *paths, literal=False)
            accepted = (0, 1)
            limit = " | sed -n '1,200p'"
        elif name == "git_show":
            if "path" in args:
                path = _path(args["path"])
                command = git("diff", "--no-ext-diff", "--no-textconv", "--no-color", self.base_sha, rev, "--", path)
            else:
                command = git("show", "--stat", "--no-ext-diff", "--no-textconv", "--no-color", rev, "--")
        else:
            raise ValueError("unknown read-only tool")
        if limit:
            # Preserve Git's status instead of returning sed/head's exit code.
            command = "bash -o pipefail -c " + shlex.quote(command + limit)
        result = self.env.execute(command, timeout=timeout)
        if result.exit_code not in accepted:
            raise ValueError("Git object query failed")
        return result.stdout[:chars]


class DecisionCallRecorder:
    """Wrap the injected DecisionComplete before giving it to the gate policy."""

    def __init__(self, complete: DecisionComplete, config: GateConfig, trajectory: Trajectory | None = None) -> None:
        self.complete = complete
        self.config = config
        self.trajectory = trajectory
        self._lock = threading.Lock()
        self._reported_cost = 0.0
        self._closed = False

    @property
    def reported_cost_usd(self) -> float:
        with self._lock:
            return self._reported_cost

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _trace(self, event: str, **data: object) -> int:
        with self._lock:
            return self.trajectory.emit(_GATE, event, **data) if self.trajectory and not self._closed else 0

    def __call__(self, request: DecisionRequest) -> DecisionResult:
        result: DecisionResult

        def invoke(**_: Any) -> dict:
            nonlocal result
            result = self.complete(request)
            with self._lock:
                if not self._closed:
                    self._reported_cost += result.cost_usd or 0.0
            return {
                "id": result.response_id,
                "model": result.raw_model,
                "provider": result.provider,
                "answers": {
                    qid: {"probabilities": values, "choice": result.chosen.get(qid)}
                    for qid, values in result.probabilities.items()
                },
                "input_tokens": result.input_tokens,
                "completion_tokens": result.completion_tokens,
                "cost_usd": result.cost_usd,
                "latency_ms": result.latency_ms,
                "tool_calls": [],
            }

        record_call(
            self._trace if self.trajectory else None,
            invoke,
            model=self.config.model,
            base_url=_safe_base_url(self.config.base_url),
            prompt_id=request.prompt_id,
            state_text=request.state_text,
            questions=[asdict(question) for question in request.questions],
            tool_calls=[],
        )
        return result


class _OpenAIComplete:
    def __init__(self, model: str | None, base_url: str | None, api_key: str | None) -> None:
        self.model = model or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao")
        self.base_url = base_url or os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or ""
        self.api_key = api_key or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or ""

    @cached_property
    def client(self) -> Any:
        from openai import OpenAI

        return OpenAI(base_url=self.base_url, api_key=self.api_key, max_retries=0)

    def request(self, messages: list[dict], tools: list[dict]) -> dict:
        options = sampling_kwargs()
        if "max_tokens" not in options:
            options.setdefault("max_completion_tokens", 2000)
        return dict(model=self.model, messages=messages, tools=tools, tool_choice="auto", **options)


@dataclass(frozen=True)
class _Candidate:
    id: str
    target: str
    kind: CandidateKind
    signals: list[Signal]
    team_step: int


@dataclass
class _Wake:
    candidate: _Candidate
    decisions: list[str]
    signal_ids: list[str]
    queued_at: float


@dataclass
class _Worker:
    records: deque[StepRecord]
    status: str = "running"
    steps: int = 0
    last_candidate_step: int = 0
    last_wake_step: int | None = None
    last_wake_at: float | None = None
    wake_count: int = 0
    probe: DiffProbe | None = None
    unchanged: int = 0
    signals: deque[tuple[int, Signal]] = field(default_factory=deque)


@dataclass
class _ToolLoop:
    calls: int = 0
    sent: int = 0
    send_calls: int = 0
    usage: Counter[str] = field(default_factory=Counter)
    turns: list[dict] = field(default_factory=list)
    summaries: list[tuple[int, str]] = field(default_factory=list)


def _recent_steps(worker: _Worker) -> list[dict]:
    return [
        {
            "step": row.step, "command": row.command[:160], "returncode": row.returncode, "is_test": row.is_test,
            "diff_added": row.diff_added, "diff_deleted": row.diff_deleted,
        }
        for row in list(worker.records)[-6:]
    ]


def _percentiles(values: list[float], *points: int) -> dict[str, float | None]:
    if len(values) < 2:
        return {f"p{point}": values[0] if values else None for point in points}
    cuts = quantiles(values, n=100, method="inclusive")
    return {f"p{point}": cuts[point - 1] for point in points}


def _usage() -> dict[str, int]:
    return dict(calls=0, prompt_tokens=0, cached_tokens=0, completion_tokens=0)


class AgentCoordinator:
    """Run the opening phase, asynchronous gates and wakes, and frozen submit checks."""

    def __init__(
        self,
        env: Environment,
        bus: TeamBus,
        assignments: Sequence[Assignment],
        *,
        trackers: dict[str, Tracker],
        gate: GatePolicy,
        config: AgentCoordinatorConfig = AgentCoordinatorConfig(),
        evidence: Callable[[], GitEvidence] = GitEvidence,
        overlap_signals: Callable[[GitEvidence, set[tuple[str, str, str, str]]], list[Signal]] | None = None,
        decision_recorder: DecisionCallRecorder | None = None,
        complete: AgentComplete | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        trajectory: Trajectory | None = None,
        presub_merge: bool = False,
        step_limit: int | None = None,
        time_limit_s: float | None = None,
        snapshot_repo: str = _SNAPSHOT_REPO,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.env, self.bus, self.config = env, bus, config
        self.trackers, self.gate = trackers, gate
        self.evidence, self.overlap_signals = evidence, overlap_signals
        self.decision_recorder = decision_recorder
        self.complete, self.trajectory = complete, trajectory
        self.assignments = {assignment.agent_id: assignment for assignment in assignments}
        self.presub_merge, self.step_limit, self.time_limit_s = presub_merge, step_limit, time_limit_s
        self._snapshot_repo = snapshot_repo
        self._openai = _OpenAIComplete(model, base_url, api_key)
        self._prompts = {
            "opening-plan": OPENING_PLAN_PROMPT, "wake": WAKE_PROMPT, "spec-check": SPEC_CHECK_PROMPT,
            "gate": GATE_INSTRUCTIONS,
        } | (config.prompts or {})
        self._prompt_ids = {name: prompt_id(name, self._prompts[name]) for name in ("opening-plan", "wake", "spec-check")}
        self._prompt_ids["gate"] = gate_prompt_id(self._prompts["gate"])
        self._clock = clock
        self._started = clock()
        self._stop = threading.Event()
        self._ready = threading.Condition()
        self._workers = {aid: _Worker(deque(maxlen=config.signals.history_steps)) for aid in self.assignments}
        self._candidates: dict[str, _Candidate] = {}
        self._wakes: dict[str, _Wake] = {}
        self._threads: tuple[threading.Thread, ...] = ()
        self._candidate_number = 0
        self._team_steps = 0
        self._decisions: list[GateDecision] = []
        self._wake_count = 0
        self._last_wake_at: float | None = None
        self._last_started_wake_at: float | None = None
        self._last_started_worker: dict[str, tuple[int, float]] = {}
        self._wake_results: list[tuple[int, int, float]] = []
        self._probes: list[tuple[float, bool]] = []
        self._signal_kinds: Counter[str] = Counter()
        self._signal_workers: Counter[str] = Counter()
        self._seen_overlaps: set[tuple[str, str, str, str]] = set()
        self._spec_outcomes: Counter[str] = Counter()
        self._spec_rejections: Counter[str] = Counter()
        self._spec_locks = {aid: threading.Lock() for aid in self.assignments}
        self._system2_usage = Counter(_usage())
        self._opening: dict[str, Any] = dict(outcome="disabled", tool_calls=0, seconds=0.0, plan_sha256=None)
        self._opening_end: float | None = None
        self._opening_done = False
        self.opening_plan: OpeningPlan | None = None
        self._first_edit: dict[str, float] = {}
        self._worker_end: dict[str, float] = {}
        self._inbox: list[dict] = []
        self._sent: list[dict] = []
        self._history: list[dict] = []
        self._summaries: list[tuple[int, str]] = []
        self._git: ReadOnlyGit | None = None

    def _emit(self, actor: str, event: str, **data: object) -> int:
        return self.trajectory.emit(actor, event, **data) if self.trajectory else 0

    def _git_tools(self, evidence: GitEvidence) -> ReadOnlyGit:
        if self._git is None:
            base = evidence.base_sha or complete_output(self.env.execute("git rev-parse HEAD", timeout=5), "Read base SHA").strip()
            self._git = ReadOnlyGit(self.env, base, snapshot_repo=self._snapshot_repo)
        return self._git

    def _read_tool(self, name: str, args: dict, evidence: GitEvidence, deadline: float, *, worker: str | None = None) -> str:
        git = self._git_tools(evidence)
        timeout = max(1, math.ceil(deadline - time.monotonic()))
        return _bounded_call(
            partial(git.read, name, args, evidence=evidence, worker=worker, timeout=timeout),
            deadline, self._stop,
        )

    def _call(self, messages: list[dict], tools: list[dict], phase: str, deadline: float, usage: Counter[str]) -> list[dict]:
        trace = partial(self._emit, _COORDINATOR, prompt_id=self._prompt_ids[phase], phase=phase) if self.trajectory else None
        usage["calls"] += 1
        with self._ready:
            self._system2_usage["calls"] += 1
        completion = self.complete

        def invoke(**request: Any) -> Any:
            if completion is not None:
                return {"tool_calls": _bounded_call(lambda: completion(messages, tools), deadline, self._stop)}
            return _bounded_call(
                lambda: self._openai.client.chat.completions.create(
                    **request, timeout=max(0.01, deadline - time.monotonic())
                ),
                deadline, self._stop,
            )

        if completion is not None:
            response = record_call(trace, invoke, messages=messages, tools=tools)
            return response["tool_calls"]
        response = record_call(trace, invoke, **self._openai.request(messages, tools))
        reported = response.usage
        if reported:
            tokens = dict(
                prompt_tokens=reported.prompt_tokens or 0, completion_tokens=reported.completion_tokens or 0,
                cached_tokens=getattr(reported.prompt_tokens_details, "cached_tokens", None) or 0,
            )
            usage.update(tokens)
            with self._ready:
                self._system2_usage.update(tokens)
        calls = response.choices[0].message.tool_calls or []
        return [{"name": call.function.name, "arguments": call.function.arguments} for call in calls]

    def _user(self, data: object) -> dict:
        return {"role": "user", "content": json.dumps(data, ensure_ascii=False)}

    @staticmethod
    def _tool_args(call: object) -> tuple[str, str, dict]:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("arguments"), str):
            raise ValueError("malformed tool call")
        raw = call["arguments"]
        args = json.loads(raw)
        if not isinstance(args, dict):
            raise ValueError("expected a JSON object")
        return call["name"], raw, args

    def _run_tools(
        self,
        phase: str,
        messages: list[dict],
        run: _ToolLoop,
        *,
        tools: list[dict],
        deadline: float,
        max_calls: int,
        execute: Callable[[str, dict], tuple[str, bool]],
    ) -> None:
        while run.calls < max_calls and not self._stop.is_set() and time.monotonic() < deadline:
            calls = self._call(messages + run.turns, tools, phase, deadline, run.usage)
            if not isinstance(calls, list) or not calls:
                return
            if self._stop.is_set() or time.monotonic() >= deadline:
                return
            assistant: dict = {"role": "assistant", "tool_calls": []}
            run.turns.append(assistant)
            for call in calls[:max_calls - run.calls]:
                if self._stop.is_set() or time.monotonic() >= deadline:
                    if not assistant["tool_calls"]:
                        run.turns.pop()
                    return
                run.calls += 1
                call_id = f"call_{phase}_{len(messages)}_{run.calls}"
                name, raw = "invalid", "{}"
                done = False
                try:
                    name, raw, args = self._tool_args(call)
                    result, done = execute(name, args)
                except (ValueError, RuntimeError) as exc:
                    result = f"Error: {exc}"
                    if phase == "opening-plan" and isinstance(call, dict) and call.get("name") == "submit_plan":
                        done = True
                assistant["tool_calls"].append({
                    "id": call_id, "type": "function", "function": {"name": name, "arguments": raw},
                })
                summary_args = " ".join(raw.splitlines())[:300]
                run.summaries.append((len(run.turns), f"{name} {summary_args} -> {len(result)} chars"))
                run.turns.append({"role": "tool", "tool_call_id": call_id, "content": result})
                if done:
                    return

    def run_opening(self) -> OpeningPlan | None:
        """Deliver a single proposal to every worker before any worker starts."""
        if self._opening_done:
            return self.opening_plan
        self._opening_done = True
        self._emit(_COORDINATOR, "agent_coordinator_start", run_id=self.bus.run_id, prompt_ids=self._prompt_ids)
        start = self._clock()
        run = _ToolLoop()
        outcome = "disabled"
        if self.config.opening == "draft":
            outcome = "failed"
            deadline = time.monotonic() + self.config.opening_time_limit_s
            try:
                evidence = self.evidence()
                git = self._git_tools(evidence)

                def execute(name: str, args: dict) -> tuple[str, bool]:
                    if name == "submit_plan":
                        plan: OpeningPlan = from_json(OpeningPlan, json.dumps(args))
                        if plan.version != 1 or plan.workers.keys() != self.assignments.keys():
                            raise ValueError("opening plan must cover the worker roster")
                        for aid in self.assignments:
                            self.bus.send(
                                sender=_COORDINATOR, to=aid,
                                content=(
                                    "Proposed opening plan. Verify it against the code and send corrections to your teammate "
                                    f"and to coordinator.\n{to_json(plan)}"
                                ),
                            )
                        self.opening_plan = plan
                        return "Plan delivered", True
                    return self._read_tool(name, args, GitEvidence(git.base_sha), deadline), False

                self._run_tools(
                    "opening-plan",
                    [
                        {"role": "system", "content": self._prompts["opening-plan"]},
                        self._user({"roster": [asdict(a) for a in self.assignments.values()], "base_sha": git.base_sha}),
                    ],
                    run, tools=[*_READ_TOOLS, SUBMIT_PLAN_TOOL], deadline=deadline,
                    max_calls=self.config.opening_max_tool_calls, execute=execute,
                )
                if self.opening_plan is not None:
                    outcome = "delivered"
            except Exception:
                pass
        self._opening_end = self._clock() - self._started
        self._opening = dict(
            outcome=outcome, tool_calls=run.calls, seconds=self._clock() - start,
            plan_sha256=sha256(to_json(self.opening_plan).encode()).hexdigest() if self.opening_plan else None,
        )
        self._emit(
            _COORDINATOR, "opening_plan", **self._opening, prompt_id=self._prompt_ids["opening-plan"],
            plan=self.opening_plan, usage=dict(run.usage),
        )
        return self.opening_plan

    def start(self) -> None:
        """Run the synchronous opening, then start the two background loops."""
        if self._threads or self._stop.is_set():
            return
        self.run_opening()
        self._threads = (
            threading.Thread(target=self._gate_loop, name="agent-coordinator-gate", daemon=True),
            threading.Thread(target=self._wake_loop, name="agent-coordinator-wake", daemon=True),
        )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._ready:
            self._ready.notify_all()

    def finish(self) -> None:
        self.stop()
        deadline = time.monotonic() + 10
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if self.decision_recorder is not None:
            self.decision_recorder.close()
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError("Agent coordinator did not stop within 10 seconds")

    def _enqueue(self, aid: str, kind: CandidateKind, signals: list[Signal]) -> None:
        """Called with the condition held. Keep one pending candidate per target."""
        if self._stop.is_set():
            return
        self._candidate_number += 1
        self._workers[aid].last_candidate_step = self._workers[aid].steps
        self._candidates[aid] = _Candidate(f"c-{self._candidate_number:04d}", aid, kind, signals, self._team_steps)
        self._ready.notify_all()

    def enqueue(self, aid: str, kind: CandidateKind, signals: Sequence[Signal] = ()) -> None:
        with self._ready:
            self._enqueue(aid, kind, list(signals))

    def _signals(self, aid: str, signals: list[Signal], *, kind: CandidateKind = "signal") -> None:
        worker = self._workers[aid]
        for signal in signals:
            self._signal_kinds[signal.kind] += 1
            self._signal_workers[aid] += 1
            worker.signals.append((worker.steps, signal))
            self._emit(_GATE, "signal", target=aid, **asdict(signal))
        waking = [signal for signal in signals if signal.kind in _WAKING_SIGNALS or signal.kind == "PEER_MESSAGE"]
        if waking:
            self._enqueue(aid, kind, waking)

    def observe(self, aid: str, records: list[StepRecord], probe: DiffProbe) -> None:
        signals = self.trackers[aid].observe(records)
        with self._ready:
            worker = self._workers[aid]
            self._probes.append((probe.latency_ms, probe.error is not None))
            if probe.error:
                signals = [*signals, Signal(f"{aid}:PROBE_ERROR:{len(self._probes)}", "PROBE_ERROR", "info", {"error": probe.error})]
            if probe.fingerprint is not None:
                previous = worker.probe.fingerprint if worker.probe else None
                worker.unchanged = worker.unchanged + len(records) if previous == probe.fingerprint else 0
                if aid not in self._first_edit and (probe.hunks or (previous is not None and previous != probe.fingerprint)):
                    self._first_edit[aid] = self._clock() - self._started
            worker.probe = probe
            worker.records.extend(records)
            worker.steps += len(records)
            self._team_steps += len(records)
            self._signals(aid, signals)
            if (records and aid not in self._candidates
                and (self.config.gate.policy == "random" or worker.steps - worker.last_candidate_step >= self.config.gate.heartbeat_steps)
            ):
                self._enqueue(aid, "heartbeat", [])
            self._prune_signals(worker)

    def _prune_signals(self, worker: _Worker) -> None:
        while worker.signals and worker.steps - worker.signals[0][0] >= self.config.signals.history_steps:
            worker.signals.popleft()

    def worker_finished(self, aid: str, status: str = "finished") -> None:
        with self._ready:
            worker = self._workers[aid]
            worker.status = status
            self._worker_end[aid] = self._clock() - self._started
            signals = [Signal(f"{aid}:WORKER_FINISHED:{worker.steps}", "WORKER_FINISHED", "info", {"status": status})]
            self._signals(aid, signals)

    def _refresh_external_signals(self) -> None:
        inbox = self.bus.receive(_COORDINATOR)
        with self._ready:
            self._inbox.extend(inbox)
            for message in inbox:
                aid = message.get("from")
                if aid in self.assignments:
                    signals = [Signal(
                        f"{aid}:PEER_MESSAGE:{self._signal_kinds['PEER_MESSAGE'] + 1}", "PEER_MESSAGE", "info",
                        {"sender": aid, "bytes": len(message.get("content", "").encode())},
                    )]
                    self._signals(aid, signals, kind="peer_message")
        if self.overlap_signals is not None:
            for signal in self.overlap_signals(self.evidence(), self._seen_overlaps):
                for aid in signal.payload.get("pair", []):
                    if aid in self.assignments:
                        with self._ready:
                            self._signals(aid, [signal])

    def _gate_state(self, candidate: _Candidate, *, reserved: bool = False) -> tuple[GateState, bool]:
        now = self._clock()
        aid = candidate.target
        worker = self._workers[aid]
        saved = self._last_started_worker.get(aid) if reserved else None
        last_step, last_time = saved or (
            (None, None) if reserved else (worker.last_wake_step, worker.last_wake_at)
        )
        last_global = self._last_started_wake_at if reserved else self._last_wake_at
        step_gap = worker.steps - last_step if last_step is not None else None
        global_gap = now - last_global if last_global is not None else None
        remaining = self.config.gate.wake_budget - self._wake_count + int(reserved)
        target_remaining = self.config.gate.per_worker_wake_cap - worker.wake_count + int(reserved)
        eligible = (
            remaining > 0 and target_remaining > 0 and worker.status == "running"
            and (step_gap is None or step_gap >= self.config.gate.min_steps_between_wakes)
            and (global_gap is None or global_gap >= self.config.gate.min_seconds_between_wakes)
        )
        probe = worker.probe
        state = GateState(
            target=aid, candidate_kind=candidate.kind, signals=candidate.signals, recent_steps=_recent_steps(worker),
            diff=dict(files=len(probe.hunks) if probe else 0, added=probe.added if probe else 0,
                      deleted=probe.deleted if probe else 0, unchanged_for_steps=worker.unchanged),
            since_last_intervention=dict(steps=step_gap, seconds=now - last_time if last_time is not None else None),
            budget=dict(remaining=remaining, target_remaining=target_remaining,
                        steps_remaining=None if self.step_limit is None else max(0, self.step_limit - worker.steps),
                        seconds_remaining=None if self.time_limit_s is None else max(0.0, self.time_limit_s - (now - self._started))),
            peers=[
                dict(id=peer, status=info.status, steps=info.steps, active_signals=sorted({s.kind for _, s in info.signals}))
                for peer, info in self._workers.items() if peer != aid
            ],
        )
        return state, eligible

    def _budget_skip(self, candidate: _Candidate, state: GateState) -> GateDecision:
        return GateDecision(
            candidate.id, candidate.kind, "SKIP", "NONE", candidate.target, [s.signal_id for s in candidate.signals],
            "budget", None, None, None, 0.0, None, None, self._prompt_ids["gate"], state.budget["remaining"],
        )

    def _gate_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_external_signals()
            except Exception as exc:
                self._emit(_GATE, "gate_error", stage="external_signals", error_type=type(exc).__name__)
            with self._ready:
                if not self._candidates and not self._stop.is_set():
                    self._ready.wait(self.config.tick_s)
                if self._stop.is_set() or not self._candidates:
                    continue
                candidate = self._candidates.pop(next(iter(self._candidates)))
                state, allowed = self._gate_state(candidate)
            if allowed:
                try:
                    decision = _bounded_call(
                        partial(self.gate.decide, state, candidate_id=candidate.id, team_step=candidate.team_step),
                        time.monotonic() + 11.0, self._stop,
                    )
                except Exception as exc:
                    if self._stop.is_set():
                        return
                    self._emit(_GATE, "gate_error", candidate_id=candidate.id, error_type=type(exc).__name__)
                    continue
            else:
                decision = self._budget_skip(candidate, state)
            with self._ready:
                if self._stop.is_set():
                    return
                if decision.decision == "WAKE":
                    self._queue_wake(candidate, decision)
                decision = replace(decision, budget_remaining_after=self.config.gate.wake_budget - self._wake_count)
                self._decisions.append(decision)
                self._emit(_GATE, "gate_decision", run_id=self.bus.run_id, state=asdict(state), **json.loads(to_json(decision)))
                self._ready.notify_all()

    def _queue_wake(self, candidate: _Candidate, decision: GateDecision) -> None:
        aid = candidate.target
        if aid in self._wakes:
            wake = self._wakes[aid]
            wake.signal_ids = list(dict.fromkeys(wake.signal_ids + decision.signal_ids))
            wake.decisions.append(candidate.id)
            wake.candidate = candidate
            return
        now = self._clock()
        self._last_wake_at = now
        worker = self._workers[aid]
        worker.last_wake_at, worker.last_wake_step = now, worker.steps
        worker.wake_count += 1
        self._wake_count += 1
        self._wakes[aid] = _Wake(candidate, [candidate.id], list(decision.signal_ids), now)

    def _wake_loop(self) -> None:
        while not self._stop.is_set():
            with self._ready:
                self._ready.wait_for(lambda: self._wakes or self._stop.is_set())
                if self._stop.is_set():
                    return
                aid = next(iter(self._wakes))
                wake = self._wakes.pop(aid)
                worker = self._workers[aid]
                now = self._clock()
                stale = now - wake.queued_at > 60
                eligible = self._gate_state(wake.candidate, reserved=True)[1] if stale else worker.status == "running"
                if not eligible:
                    self._emit(
                        _COORDINATOR, "wake_end", run_id=self.bus.run_id, candidate_id=wake.candidate.id,
                        candidate_ids=wake.decisions, signal_ids=wake.signal_ids, target=aid,
                        outcome="stale" if stale else "finished", tool_calls=0, messages_sent=0, seconds=0.0,
                        usage=_usage(), prompt_id=self._prompt_ids["wake"],
                    )
                    continue
                self._last_started_worker[aid] = (worker.steps, now)
                self._last_started_wake_at = now
            self._run_wake(wake)

    def _wake_observation(self, evidence: GitEvidence) -> dict:
        with self._ready:
            inbox, self._inbox = self._inbox, []
            return {
                "roster": [asdict(a) for a in self.assignments.values()],
                "opening_plan": asdict(self.opening_plan) if self.opening_plan else None,
                "workers": {
                    aid: dict(status=worker.status, recent_steps=_recent_steps(worker),
                              active_signals=[asdict(signal) for _, signal in worker.signals])
                    for aid, worker in self._workers.items()
                },
                "git_evidence": evidence.metadata(),
                "unread_messages": inbox,
                "previous_messages": list(self._sent),
            }

    def _run_wake(self, wake: _Wake) -> None:
        target = wake.candidate.target
        run = _ToolLoop()
        started = self._clock()
        deadline = time.monotonic() + self.config.wake_time_limit_s
        outcome = "exhausted"
        event = dict(
            run_id=self.bus.run_id, target=target, candidate_id=wake.candidate.id, candidate_ids=wake.decisions,
            signal_ids=wake.signal_ids, prompt_id=self._prompt_ids["wake"],
        )

        def execute(name: str, args: dict) -> tuple[str, bool]:
            nonlocal outcome
            if name == "end_wake":
                if not isinstance(args.get("note"), str):
                    raise ValueError("end_wake requires a note")
                outcome = "ended"
                return "Wake ended", True
            if name == "send_message":
                if run.send_calls >= 2:
                    raise ValueError("message limit reached")
                run.send_calls += 1
                recipient, content = args.get("recipient"), args.get("content")
                if not isinstance(content, str) or len(content) > 1500:
                    raise ValueError("message exceeds the 1500-character limit or is invalid")
                if not isinstance(recipient, str) or (recipient != "group" and recipient not in self.assignments):
                    raise ValueError("unknown message recipient")
                for aid in self.assignments if recipient == "group" else (recipient,):
                    self.bus.send(sender=_COORDINATOR, to=aid, content=content)
                    self._sent.append(dict(to=aid, content=content))
                    run.sent += 1
                return "Message sent", False
            return self._read_tool(name, args, evidence, deadline, worker=target), False

        try:
            evidence = self.evidence()
            observation = self._wake_observation(evidence)
            self._emit(_COORDINATOR, "wake_start", **event, observation=observation)
            for index, summary in self._summaries:
                self._history[index]["content"] = summary
            messages = [
                {"role": "system", "content": self._prompts["wake"]},
                self._user({"opening_plan": asdict(self.opening_plan) if self.opening_plan else None}),
                *self._history,
                self._user(observation),
            ]
            self._run_tools(
                "wake", messages, run, tools=[*_READ_TOOLS, SEND_MESSAGE_TOOL, END_WAKE_TOOL], deadline=deadline,
                max_calls=self.config.wake_max_tool_calls, execute=execute,
            )
        except TimeoutError:
            outcome = "stopped" if self._stop.is_set() else "timeout"
        except Exception:
            outcome = "error"
        finally:
            self._summaries = [(len(self._history) + index, summary) for index, summary in run.summaries]
            self._history.extend(run.turns)
            seconds = self._clock() - started
            with self._ready:
                self._wake_results.append((run.calls, run.sent, seconds))
            self._emit(_COORDINATOR, "wake_end", **event, outcome=outcome, tool_calls=run.calls, messages_sent=run.sent, seconds=seconds,
                       usage=_usage() | dict(run.usage))

    def _freeze_diff(self, env: Environment) -> str:
        base = getattr(env, "_base_commit", "") or "HEAD"
        diff = "git diff --binary --no-ext-diff --no-textconv --no-color"
        emit_untracked = (
            f'for path do {diff} --no-index -- /dev/null "$path"; status=$?; '
            'if [ "$status" -gt 1 ]; then exit "$status"; fi; done'
        )
        command = (
            f"{diff} {shlex.quote(base)} -- && "
            'untracked=$(git ls-files --others --exclude-standard -z | '
            f'xargs -0 sh -c {shlex.quote(emit_untracked)} sh) && '
            'if [ -n "$untracked" ]; then printf "%s\\n" "$untracked"; fi'
        )
        result = env.execute("bash -o pipefail -c " + shlex.quote(command), timeout=60)
        return strip_test_sections(complete_output(result, "Freeze submission diff"))

    @staticmethod
    def _feature(assignment: Assignment) -> dict[str, str]:
        feature_id = str(assignment.feature_id) if assignment.feature_id is not None else assignment.agent_id
        return dict(feature_id=feature_id, task=assignment.task)

    def _check_diff(self, aid: str, frozen: str) -> tuple[SpecVerdict, Counter[str]]:
        feature = self._feature(self.assignments[aid])
        used: Counter[str] = Counter()
        if not frozen.strip():
            return SpecVerdict("REJECT", [SpecFinding("empty_diff", feature["feature_id"], "")]), used
        if _CONFLICT.search(frozen):
            return SpecVerdict("REJECT", [SpecFinding("conflict_marker", feature["feature_id"], "")]), used

        peers = [self._feature(assign) for peer, assign in self.assignments.items() if peer != aid] if self.presub_merge else []
        observation = dict(
            feature=feature, teammate_features=peers, frozen_diff=_clip(frozen, 12000),
            opening_plan=asdict(self.opening_plan) if self.opening_plan else None,
        )
        try:
            calls = self._call(
                [{"role": "system", "content": self._prompts["spec-check"]}, self._user(observation)],
                [SPEC_VERDICT_TOOL], "spec-check", time.monotonic() + 60, used,
            )
            if len(calls) != 1:
                raise ValueError("expected exactly one spec verdict")
            name, raw, _ = self._tool_args(calls[0])
            verdict: SpecVerdict = from_json(SpecVerdict, raw)
            if name != "spec_verdict" or verdict.verdict == "ERROR" or len(verdict.findings) > 3:
                raise ValueError("invalid spec verdict")
            if verdict.verdict == "REJECT" and not verdict.findings:
                raise ValueError("rejection requires a finding")
            features = {row["feature_id"]: row["task"] for row in [feature, *peers]}
            for finding in verdict.findings:
                if not 0 < len(finding.quote) <= 300 or finding.quote not in features.get(finding.feature_id, ""):
                    raise ValueError("finding must quote a supplied feature")
            return verdict, used
        except Exception:
            return SpecVerdict("ERROR", []), used

    def spec_check(self, aid: str, env: Environment) -> str | None:
        if not self.config.spec_check:
            return None
        with self._spec_locks[aid]:
            if self._spec_rejections[aid] >= self.config.spec_gate_max_rejections:
                return None
            frozen_sha = None
            try:
                frozen = self._freeze_diff(env)
                frozen_sha = sha256(frozen.encode()).hexdigest()
                verdict, used = self._check_diff(aid, frozen)
            except Exception:
                verdict, used = SpecVerdict("ERROR", []), Counter()
            with self._ready:
                self._spec_outcomes[verdict.verdict] += 1
                if verdict.verdict == "REJECT":
                    self._spec_rejections[aid] += 1
            self._emit(
                _COORDINATOR, "spec_check", target=aid, frozen_diff_sha256=frozen_sha, prompt_id=self._prompt_ids["spec-check"],
                **asdict(verdict), usage=_usage() | dict(used),
            )
            if verdict.verdict != "REJECT":
                return None
            return "\n".join([
                "SPEC_CHECK_REJECTED",
                *(f"- {f.kind} (feature {f.feature_id})" + (f" {f.path}" if f.path else "") + (f": {f.quote}" if f.quote else "")
                  for f in verdict.findings),
            ])

    def submit_attempt(self, aid: str, error: str | None) -> None:
        signals = self.trackers[aid].submit_attempt(aid, error)
        with self._ready:
            self._signals(aid, signals)

    def metrics(self) -> dict:
        with self._ready:
            gate = self.config.gate
            decisions = list(self._decisions)
            sources = Counter(row.source for row in decisions)
            model_calls = sources["model"] + sources["fallback"]
            fallbacks = Counter(dict(timeout=0, transport=0, invalid_response=0))
            fallbacks.update(row.error for row in decisions if row.error is not None)
            p_wake = [row.probabilities["WAKE"] for row in decisions if row.probabilities is not None]
            random_wakes = sum(row.source == "random" and row.decision == "WAKE" for row in decisions)
            return {
                "schema": AGENT_COORDINATOR_METRICS_SCHEMA,
                "config": {
                    "gate": {
                        "policy": gate.policy, "model": gate.model, "base_url": _safe_base_url(gate.base_url),
                        "wake_threshold": gate.wake_threshold, "timeout_s": gate.timeout_s,
                        "wake_budget": gate.wake_budget, "per_worker_wake_cap": gate.per_worker_wake_cap,
                    },
                    "opening": self.config.opening, "spec_check": self.config.spec_check, "prompt_ids": dict(self._prompt_ids),
                },
                "opening": dict(self._opening),
                "signals": {"by_kind": dict(self._signal_kinds), "by_worker": dict(self._signal_workers)},
                "gate": {
                    "candidates": len(decisions), "wake": sum(row.decision == "WAKE" for row in decisions),
                    "skip": sum(row.decision == "SKIP" for row in decisions),
                    "by_reason": dict(Counter(row.reason_code for row in decisions)), "by_source": dict(sources),
                    "by_candidate_kind": dict(Counter(row.candidate_kind for row in decisions)),
                    "model_calls": model_calls, "fallbacks": dict(fallbacks),
                    "p_wake": dict(n=len(p_wake), mean=sum(p_wake) / len(p_wake) if p_wake else None, **_percentiles(p_wake, 50, 90)),
                    "latency_ms": _percentiles([row.latency_ms for row in decisions], 50, 95),
                    "budget": gate.wake_budget, "budget_remaining": gate.wake_budget - self._wake_count,
                    "random_planned": gate.random_wakes if gate.policy == "random" else None,
                    "random_unused": max(0, (gate.random_wakes or 0) - random_wakes) if gate.policy == "random" else None,
                },
                "wakes": {
                    "total": len(self._wake_results), "tool_calls": sum(row[0] for row in self._wake_results),
                    "messages_sent": sum(row[1] for row in self._wake_results),
                    "seconds": _percentiles([row[2] for row in self._wake_results], 50, 95),
                },
                "spec_check": {
                    "checked": self._spec_outcomes.total(), "accepted": self._spec_outcomes["ACCEPT"],
                    "rejected": self._spec_outcomes["REJECT"], "errors": self._spec_outcomes["ERROR"],
                },
                "probe": dict(calls=len(self._probes), errors=sum(row[1] for row in self._probes),
                              latency_ms=_percentiles([row[0] for row in self._probes], 50, 95)),
                "usage": {
                    _COORDINATOR: dict(self._system2_usage),
                    _GATE: {
                        "calls": model_calls, "prompt_tokens": sum(row.input_tokens or 0 for row in decisions),
                        "cached_tokens": 0, "completion_tokens": sum(row.completion_tokens or 0 for row in decisions),
                        "reported_cost_usd": self.decision_recorder.reported_cost_usd if self.decision_recorder else 0.0,
                    },
                },
                "team_worker_steps": self._team_steps,
                "timeline": {
                    "opening_end_s": self._opening_end, "first_worker_edit_s": dict(self._first_edit),
                    "worker_end_s": dict(self._worker_end),
                },
            }


class AgentCoordinatorPoller:
    """Compose the harness poller with step parsing and one worker-side probe."""

    def __init__(
        self,
        coordinator: AgentCoordinator,
        aid: str,
        env: Environment,
        teammate: TeammatePoller,
        *,
        parse_steps: ParseSteps,
        probe: Callable[[Environment], DiffProbe],
    ) -> None:
        self.coordinator, self.aid, self.env, self.teammate = coordinator, aid, env, teammate
        self.parse_steps, self.probe = parse_steps, probe
        self.agent: WorkerMessages | None = None
        self._messages: list[dict] = []
        self._cursor = 0
        self._steps = 0

    def attach(self, agent: WorkerMessages) -> None:
        """Use as the worker's on_start hook."""
        self.agent = agent

    @staticmethod
    def _message_key(message: dict) -> tuple[str, object]:
        # Recorded tool results retain their IDs and timestamps through compaction.
        if tool_id := message.get("tool_call_id"):
            return "tool", tool_id
        if timestamp := message.get("extra", {}).get("timestamp"):
            return "timestamp", timestamp
        return "object", id(message)

    def poll(self) -> str:
        notes = self.teammate.poll()
        messages = self.agent.messages if self.agent is not None else []
        start = self._cursor
        if messages is not self._messages or len(messages) < start:
            previous = {self._message_key(message) for message in self._messages[2:start]}
            start = next((index + 1 for index in range(len(messages) - 1, 1, -1) if self._message_key(messages[index]) in previous), 0)
        records = self.parse_steps(self.aid, messages[start:], start=self._steps, config=self.coordinator.config.signals)
        self._messages = messages
        self._cursor = len(messages)
        self._steps += len(records)
        try:
            probe = self.probe(self.env)
        except Exception as exc:
            probe = DiffProbe(None, {}, 0, 0, 0.0, type(exc).__name__)
        if records:
            records[-1] = replace(records[-1], fingerprint=probe.fingerprint, hunks=probe.hunks,
                                  diff_added=probe.added, diff_deleted=probe.deleted)
        self.coordinator.observe(self.aid, records, probe)
        return notes


def composed_completion_gate(
    coordinator: AgentCoordinator, aid: str, base: Callable[[Environment], str | None] | None = None,
) -> Callable[[Environment], str | None]:
    def composed(env: Environment) -> str | None:
        error = base(env) if base is not None else None
        coordinator.submit_attempt(aid, error)
        if error is not None:
            return error
        return coordinator.spec_check(aid, env)

    return composed
