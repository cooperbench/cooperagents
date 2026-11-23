"""Frozen data contracts for the agent coordinator and its decision gate."""
from __future__ import annotations

import json
import os
import types
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, Callable, Literal, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

GATE_STATE_SCHEMA = "gate-state/1"
GATE_DECISION_SCHEMA = "gate-decision/1"
OPENING_PLAN_SCHEMA = "opening-plan/1"
AGENT_COORDINATOR_METRICS_SCHEMA = "agent-coordinator-metrics/1"

SignalKind = Literal["NO_PROGRESS_FAILING", "HUNK_CHURN", "TEST_REGRESSION", "PEER_OVERLAP", "SUBMIT_REJECTED", "SUBMIT_ATTEMPT", "PEER_MESSAGE", "WORKER_FINISHED", "PROBE_ERROR"]
Severity = Literal["hard", "warn", "info"]
Decision = Literal["WAKE", "SKIP"]
ReasonCode = Literal["NO_PROGRESS", "HUNK_CHURN", "TEST_REGRESSION", "PEER_OVERLAP", "SUBMIT_REJECTED", "PEER_MESSAGE", "HEARTBEAT_PATTERN", "RANDOM_SCHEDULE", "NONE"]
DecisionSource = Literal["rule", "model", "fallback", "random", "budget"]
CandidateKind = Literal["signal", "peer_message", "heartbeat"]

@dataclass(frozen=True)
class DiffProbe:
    fingerprint: str | None
    hunks: dict[str, list[tuple[int, int]]]
    added: int
    deleted: int
    latency_ms: float
    error: str | None = None

@dataclass(frozen=True)
class StepRecord:
    worker: str
    step: int
    command: str
    returncode: int
    output_sha256: str
    is_test: bool
    fingerprint: str | None = None
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    diff_added: int = 0
    diff_deleted: int = 0

@dataclass(frozen=True)
class Signal:
    signal_id: str
    kind: SignalKind
    severity: Severity
    payload: dict[str, Any]

@dataclass(frozen=True)
class GateState:
    target: str
    candidate_kind: CandidateKind
    signals: list[Signal]
    recent_steps: list[dict[str, Any]]
    diff: dict[str, Any]
    since_last_intervention: dict[str, Any]
    budget: dict[str, Any]
    peers: list[dict[str, Any]]
    schema: str = GATE_STATE_SCHEMA

@dataclass(frozen=True)
class GateDecision:
    candidate_id: str
    candidate_kind: CandidateKind
    decision: Decision
    reason_code: ReasonCode
    target: str
    signal_ids: list[str]
    source: DecisionSource
    probabilities: dict[str, float] | None
    threshold: float | None
    model: str | None
    latency_ms: float
    input_tokens: int | None
    completion_tokens: int | None
    prompt_id: str
    budget_remaining_after: int
    error: str | None = None
    schema: str = GATE_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if self.source == "model" and self.probabilities is None:
            raise ValueError("model decision requires probabilities")
        if self.source != "model" and self.probabilities is not None:
            raise ValueError("probabilities are only valid for model decisions")
        if self.source not in ("model", "fallback") and (self.threshold is not None or self.model is not None):
            raise ValueError("threshold and model require model or fallback source")
        if (self.source == "fallback") != (self.error is not None):
            raise ValueError("error is required only for fallback decisions")

@dataclass(frozen=True)
class DecisionQuestion:
    id: str
    instructions: str
    criteria: dict[str, str]

@dataclass(frozen=True)
class DecisionRequest:
    state_text: str
    questions: tuple[DecisionQuestion, ...]
    prompt_id: str

@dataclass(frozen=True)
class DecisionResult:
    probabilities: dict[str, dict[str, float]]
    chosen: dict[str, str]
    latency_ms: float
    input_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    raw_model: str | None
    provider: str | None
    response_id: str | None

DecisionComplete = Callable[[DecisionRequest], DecisionResult]
AgentComplete = Callable[[list[dict], list[dict]], list[dict]]

@dataclass(frozen=True)
class WorkerPlan:
    owns: list[str]
    avoid: list[str]
    interfaces: list[str]
    notes: str

@dataclass(frozen=True)
class OpeningPlan:
    version: int
    workers: dict[str, WorkerPlan]
    shared_interfaces: list[dict[str, str]]
    risks: list[dict[str, str]]
    evidence: list[dict[str, str]]
    schema: str = OPENING_PLAN_SCHEMA

@dataclass(frozen=True)
class SpecFinding:
    kind: Literal["missing_requirement", "api_mismatch", "unrelated_edit", "lost_teammate_change", "empty_diff", "conflict_marker"]
    feature_id: str
    quote: str
    path: str | None = None

@dataclass(frozen=True)
class SpecVerdict:
    verdict: Literal["ACCEPT", "REJECT", "ERROR"]
    findings: list[SpecFinding]

TEST_COMMAND_PREFIXES = ("pytest", "python -m pytest", "python3 -m pytest", "python -m unittest", "tox", "nox", "go test", "cargo test", "npm test", "npm run test", "yarn test", "pnpm test", "npx jest", "jest", "vitest", "mvn test", "gradle test", "./gradlew test", "make test", "ctest", "rspec", "bundle exec rspec", "phpunit", "dotnet test")

@dataclass(frozen=True)
class SignalConfig:
    history_steps: int = 24
    no_progress_window: int = 5
    no_progress_min_failures: int = 3
    churn_min_steps: int = 3
    churn_window: int = 10
    cooldown_steps: int = 6
    probe_timeout_s: float = 10.0
    test_command_prefixes: tuple[str, ...] = TEST_COMMAND_PREFIXES

@dataclass(frozen=True)
class GateConfig:
    policy: Literal["model", "rule", "random"] = "model"
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    wake_threshold: float | None = None
    timeout_s: float | None = None
    wake_budget: int = 8
    per_worker_wake_cap: int = 4
    min_steps_between_wakes: int = 4
    min_seconds_between_wakes: float = 30.0
    heartbeat_steps: int = 15
    random_wakes: int | None = None
    random_step_horizon: int | None = None
    random_seed: int = 0

    def __post_init__(self) -> None:
        if self.policy == "model":
            for name, default in (("model", "liquid/d1.2-ultra"), ("base_url", "https://openrouter.ai/api/alpha"), ("api_key_env", "OPENROUTER_API_KEY"), ("wake_threshold", 0.5), ("timeout_s", 3.0)):
                if getattr(self, name) is None:
                    object.__setattr__(self, name, default)

    def validate(self) -> None:
        if self.policy not in ("model", "rule", "random"):
            raise ValueError("invalid gate policy")
        values = (self.model, self.base_url, self.api_key_env, self.wake_threshold, self.timeout_s)
        if self.policy != "model":
            if any(v is not None for v in values):
                raise ValueError("decision model fields require model policy")
            return
        if not self.model or not self.base_url or self.base_url.endswith('/'):
            raise ValueError("invalid model or base URL")
        url = urlsplit(self.base_url)
        if not url.hostname or url.username or url.password or url.query or url.fragment or not (url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1"))):
            raise ValueError("invalid gate base URL")
        if not self.api_key_env or not os.environ.get(self.api_key_env):
            raise ValueError("gate API key environment variable is empty")
        if self.wake_threshold is None or not 0 <= self.wake_threshold <= 1:
            raise ValueError("invalid wake threshold")
        if self.timeout_s is None or not 0 < self.timeout_s <= 10:
            raise ValueError("invalid gate timeout")

@dataclass(frozen=True)
class AgentCoordinatorConfig:
    gate: GateConfig = field(default_factory=GateConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    opening: Literal["draft", "none"] = "draft"
    spec_check: bool = True
    opening_max_tool_calls: int = 16
    opening_time_limit_s: float = 180.0
    wake_max_tool_calls: int = 6
    wake_time_limit_s: float = 90.0
    spec_gate_max_rejections: int = 1
    tick_s: float = 1.0
    prompts: dict[str, str] | None = None

# OpenAI function-tool schemas.  Phase-specific callers select the applicable tools.
def _tool(name: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": name.replace("_", " "), "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}

_STRING = {"type": "string"}
LIST_DIR_TOOL = _tool("list_dir", {"path": _STRING}, ["path"])
READ_FILE_TOOL = _tool("read_file", {"path": _STRING, "start": {"type": "integer", "minimum": 1}, "end": {"type": "integer", "minimum": 1}}, ["path", "start", "end"])
SEARCH_CODE_TOOL = _tool("search_code", {"pattern": _STRING, "path_glob": _STRING}, ["pattern", "path_glob"])
GIT_SHOW_TOOL = _tool("git_show", {"rev": _STRING, "path": _STRING}, ["rev"])
SEND_MESSAGE_TOOL = _tool("send_message", {"recipient": _STRING, "content": {"type": "string", "maxLength": 1500}}, ["recipient", "content"])
_STRING_LIST = {"type": "array", "items": _STRING}
_WORKER_PLAN = {"type": "object", "properties": {"owns": _STRING_LIST, "avoid": _STRING_LIST, "interfaces": _STRING_LIST, "notes": _STRING}, "required": ["owns", "avoid", "interfaces", "notes"], "additionalProperties": False}
def _record(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
SUBMIT_PLAN_TOOL = _tool("submit_plan", {
    "schema": {"type": "string", "const": OPENING_PLAN_SCHEMA}, "version": {"type": "integer", "const": 1},
    "workers": {"type": "object", "additionalProperties": _WORKER_PLAN},
    "shared_interfaces": {"type": "array", "items": _record({"name": _STRING, "owner": _STRING, "definition": _STRING, "location": _STRING}, ["name", "owner", "definition", "location"])},
    "risks": {"type": "array", "items": _record({"path": _STRING, "reason": _STRING}, ["path", "reason"])},
    "evidence": {"type": "array", "items": _record({"tool": _STRING, "path": _STRING, "lines": _STRING}, ["tool", "path", "lines"])},
}, ["schema", "version", "workers", "shared_interfaces", "risks", "evidence"])
SPEC_VERDICT_TOOL = _tool("spec_verdict", {
    "verdict": {"type": "string", "enum": ["ACCEPT", "REJECT"]},
    "findings": {"type": "array", "maxItems": 3, "items": _record({"kind": {"type": "string", "enum": ["missing_requirement", "api_mismatch", "unrelated_edit", "lost_teammate_change", "empty_diff", "conflict_marker"]}, "feature_id": _STRING, "quote": {"type": "string", "maxLength": 300}, "path": _STRING}, ["kind", "feature_id", "quote"])},
}, ["verdict", "findings"])
END_WAKE_TOOL = _tool("end_wake", {"note": _STRING}, ["note"])
TOOL_SCHEMAS = (LIST_DIR_TOOL, READ_FILE_TOOL, SEARCH_CODE_TOOL, GIT_SHOW_TOOL, SEND_MESSAGE_TOOL, SUBMIT_PLAN_TOOL, SPEC_VERDICT_TOOL, END_WAKE_TOOL)

_SCHEMAS = {GateState: GATE_STATE_SCHEMA, GateDecision: GATE_DECISION_SCHEMA, OpeningPlan: OPENING_PLAN_SCHEMA}

def to_json(value: Any) -> str:
    data = asdict(value)
    if isinstance(value, GateDecision) and value.error is None:
        del data["error"]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)

def _decode(tp: Any, value: Any) -> Any:
    origin, args = get_origin(tp), get_args(tp)
    if origin in (types.UnionType, __import__('typing').Union):
        for arg in args:
            try:
                return _decode(arg, value)
            except (ValueError, TypeError):
                pass
        raise ValueError("invalid union value")
    if tp is Any:
        return value
    if origin is Literal:
        if value not in args:
            raise ValueError("invalid literal")
        return value
    if is_dataclass(tp):
        if not isinstance(value, dict):
            raise ValueError("expected object")
        names = {f.name for f in fields(tp)}
        if set(value) - names:
            raise ValueError("unknown keys")
        if tp in _SCHEMAS and value.get("schema") != _SCHEMAS[tp]:
            raise ValueError("invalid schema version")
        hints = get_type_hints(tp)
        try:
            return tp(**{k: _decode(hints[k], v) for k, v in value.items()})
        except TypeError as exc:
            raise ValueError("missing or invalid fields") from exc
    if origin in (list, tuple):
        if not isinstance(value, list):
            raise ValueError("expected array")
        result = [_decode(args[0], v) for v in value]
        return tuple(result) if origin is tuple else result
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return {_decode(args[0], k): _decode(args[1], v) for k, v in value.items()}
    if tp is type(None):
        if value is not None:
            raise ValueError("expected null")
    elif tp is float:
        if type(value) not in (int, float):
            raise ValueError("expected number")
    elif not isinstance(value, tp) or (tp is int and isinstance(value, bool)):
        raise ValueError("invalid value type")
    return value

def from_json(cls: type, text: str) -> Any:
    try:
        return _decode(cls, json.loads(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON contract") from exc
