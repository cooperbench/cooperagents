"""The unified harness orchestrator.

**Hard constraint: every agent runs in its OWN container/environment** — agents
never share a live workspace. The coordinated team path (``_run_isolated``)
seeds each fresh container with teammates' cumulative diff so an agent still
builds on prior work, but execution is always isolated per agent.

One ``run`` call drives a whole team on one problem.  Unlike a two-level
design (a team harness that wraps an opaque agent harness), the orchestrator
and every agent share one :class:`TeamBus`, so the team can reshape itself
while it works:

  1. **Seed** the team from the spec — one agent per assignment
     (``N tasks for N agents``) or a lead + members on a shared objective
     (``one task for the whole team``).
  2. Run every seed agent concurrently, each in its own environment.
  3. A **supervisor** drains the spawn queue: when an agent calls
     ``spawn_helper``, it launches a fresh helper agent on that sub-task —
     up to ``max_agents`` — which itself may recruit further help.
  4. When the pool goes idle, harvest patches + coordination/spawn metrics.

The result is eval-ready: per-feature seed patches are what CooperBench
scores; helper output reaches the score through the agent that integrates
it (the conventional lead-merges-the-team pattern).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cooperagents.agent import Agent
from cooperagents.bus.base import TeamBus
from cooperagents.bus.memory import InMemoryBus
from cooperagents.coordination_prompts import (
    COORDINATOR_PLANNING,
    COORDINATOR_WARNING_RULES,
    FORCED_PLANNING,
    GROUP_CHAT,
    HUMAN_COORDINATOR_BASE,
    HUMAN_COORDINATOR_INITIAL,
    HUMAN_WORKER_WORKFLOW,
    IMPLEMENTATION_MODE,
    PLANNING_MODE,
    PLANNING_SYSTEM,
)
from cooperagents.env.base import Environment
from cooperagents.git_snapshots import GitEvidence
from cooperagents.git_snapshots import GitShareSync as _GitShareSync
from cooperagents.initial_plan import InitialPlan
from cooperagents.llm import LLMClient
from cooperagents.metrics import coordination_metrics, spawn_metrics
from cooperagents.patching import strip_test_sections
from cooperagents.planner import ancestors, plan_decomposition, topo_levels
from cooperagents.types import AgentResult, Assignment, RunResult, SubTask, TeamSpec

if TYPE_CHECKING:
    from cooperagents.completion import CompletionBinding

Planner = Callable[[list[tuple[int, str]], "str | None"], "tuple[list[SubTask], str]"]


def _seed_patch(env: Environment, patch: str) -> None:
    """Apply a teammate/ancestor delta into a fresh container and commit it, so
    the agent starts from a clean coherent base (a dirty tree confuses it)."""
    if not patch.strip():
        return
    env.write_file(".cb_prior.patch", patch)
    env.execute(
        "git apply --whitespace=nowarn .cb_prior.patch 2>/dev/null "
        "|| git apply --3way .cb_prior.patch 2>/dev/null "
        "|| git apply --reject .cb_prior.patch 2>/dev/null || true"
    )
    env.execute(
        "rm -f .cb_prior.patch && git add -A && "
        "git -c user.email=team@cooperagents.local -c user.name=cooperagents commit -q -m 'seed' || true"
    )


_GIT_ID = "-c user.email=team@cooperagents.local -c user.name=cooperagents"


_HEALTH_CMD = """
if [ -f go.mod ] && command -v go >/dev/null 2>&1; then
  go build ./... >/dev/null 2>&1 || exit 1
fi
if command -v python3 >/dev/null 2>&1; then
python3 - <<'CB_HEALTH_EOF'
import ast, pathlib, sys
bad = []
for p in pathlib.Path(".").rglob("*.py"):
    s = str(p)
    if ".git/" in s or s.startswith(".cb_"):
        continue
    try:
        ast.parse(p.read_bytes(), filename=s)
    except SyntaxError:
        bad.append(s)
    except Exception:
        pass
sys.exit(1 if bad else 0)
CB_HEALTH_EOF
fi
"""


def _tree_health(env: Environment) -> bool:
    """Mechanical repo health check for the Q1 do-no-harm gate: build (go) or
    AST-parse every source file (python — no bytecode, so no __pycache__ noise
    in the diff). Only a DEFINITE defect (exit 1) counts as broken; timeouts or
    missing toolchains read as healthy — the gate must never discard work it
    cannot judge."""
    res = env.execute(_HEALTH_CMD, timeout=180)
    return res.exit_code != 1


def _apply_commit(env: Environment, patch: str, msg: str) -> None:
    env.write_file(".cb_d.patch", patch)
    env.execute(
        "git apply --whitespace=nowarn .cb_d.patch 2>/dev/null "
        "|| git apply --3way .cb_d.patch 2>/dev/null || true; rm -f .cb_d.patch"
    )
    env.execute(f"git add -A && git {_GIT_ID} commit -q -m '{msg}' || true")


def _threeway_merge(env: Environment, deltas: list[str]) -> tuple[bool, str]:
    """Real 3-way merge of independent branch deltas against the base commit.

    Each delta becomes a branch off the base; they are merged one by one into an
    accumulator. A true 3-way merge (unlike ``git apply --check``) resolves
    non-overlapping edits to the same file and only conflicts on genuine region
    overlaps — the correct separability test. Returns (conflict, merged_diff).
    """
    nz = [d for d in deltas if d.strip()]
    if not nz:
        return False, ""
    base = (env.execute("git rev-parse HEAD").stdout or "").strip() or "HEAD"
    env.execute(f"git checkout -q -B _acc {base}")
    _apply_commit(env, nz[0], "d0")
    for i, d in enumerate(nz[1:], 1):
        env.execute(f"git checkout -q -B _b{i} {base}")
        _apply_commit(env, d, f"d{i}")
        env.execute("git checkout -q _acc")
        m = env.execute(f"git {_GIT_ID} merge --no-edit _b{i} 2>&1")
        if m.exit_code != 0:  # genuine region overlap → coupled
            env.execute("git merge --abort 2>/dev/null || true")
            return True, ""
    return False, strip_test_sections(env.git_diff())

EnvFactory = Callable[[str], Environment]
LLMFactory = Callable[[str, str], LLMClient]


_SPEC_FIDELITY = (
    "IMPORTANT — API fidelity: a hidden automated test suite grades your work by "
    "referencing the EXACT public names and signatures the spec describes "
    "(types, functions, methods, struct fields, parameters, constants). Implement "
    "them verbatim as named or strongly implied by the spec — do not rename, "
    "abbreviate, pluralize differently, or invent API surface. Match the spec's "
    "wording when choosing identifiers.\n\n"
)


_TDD_PREAMBLE = (
    "WORKFLOW — verify as you go: BEFORE writing code, derive from the spec a short "
    "checklist of concrete acceptance criteria (exact public API names, expected "
    "behaviors, edge cases). For each, write a THROWAWAY local check you can run "
    "(a `python -c ...` one-liner or a scratch script OUTSIDE any tests/ directory) "
    "— never add or edit files in the project's real test suite. Implement until "
    "every check passes, then DELETE your scratch checks before you finish. Do not "
    "submit until your own checks confirm each acceptance criterion holds.\n\n"
)


_MINE_CONVENTIONS = (
    "WORKFLOW — mine conventions first: BEFORE editing, inspect how this repo "
    "already does things in the area you will touch. Grep for the public symbols "
    "the spec mentions and read the neighbouring code, existing tests, and similar "
    "features to learn the naming, signatures, error handling, and patterns in use. "
    "Mirror those existing conventions in your implementation rather than inventing "
    "new ones, so your feature integrates cleanly with what is already there.\n\n"
)


_CONTRACT_PROMPT = (
    "Two features below will be implemented IN PARALLEL by separate engineers on "
    "separate copies of the same repository; their diffs are merged afterwards. "
    "Write the SHARED INTERFACE CONTRACT they must both follow so the merge is "
    "clean: the exact public names, signatures, and file locations of anything "
    "both features touch or one provides for the other (new helpers, config "
    "fields, registration points). Be concrete and minimal — a short bulleted "
    "list, no prose. If the features are fully independent, list the files each "
    "should keep to.\n\nFEATURES:\n\n{specs}"
)


def _build_contract(assignments: list[Assignment], model: str | None = None) -> str:
    """TK1/Q6: one planner call producing the shared-interface contract.

    Offline-safe: no creds / any error → empty string (feature silently off)."""
    from cooperagents.planner import _default_planner_complete

    fn = _default_planner_complete(model, None, None)
    if fn is None:
        return ""
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(f"### Feature {i + 1}\n{t[:3000]}" for i, t in enumerate(specs))
    try:
        out = (fn(_CONTRACT_PROMPT.format(specs=bundle)) or "").strip()
    except Exception:  # noqa: BLE001 - contract is best-effort
        return ""
    return out[:1800]


_COORDINATOR_PATH = "/coordination/notebook.md"
_COORDINATION_WORKFLOW = (
    "\n\nCOORDINATOR ACKNOWLEDGMENT — when new coordinator notices arrive, begin your next "
    "assistant response with one short ordinary-text acknowledgment naming the request and your next action. "
    "Do not use send_message for the acknowledgment, claim completion or agreement, or repeat it without "
    "a new notice. Then continue work with normal tools in that same response. Example: assistant text "
    "'I received the coordinator's scope request; I'll inspect the relevant files now,' followed by "
    "bash {\"command\":\"ls src\"}.\n\n"
    "COORDINATION — after acknowledging, inspect the relevant code, then send your proposed files, regions and "
    "shared interfaces to your teammates and to the coordinator using send_message. Confirm disputed "
    "shared responsibilities before editing those regions. While waiting, continue read-only exploration, "
    "verification or clearly independent work; do not use wait:true. Report changed plans, blockers and "
    "verification results. A proposal is not an agreement; silence is not confirmation."
)
_COORDINATOR_PROMPT = """You coordinate software workers implementing features in separate copies of one repository.
You make one decision from the current OBSERVATION. You cannot inspect code, execute tools or wait for replies.
The harness executes your returned actions; workers inspect code and may report back in a later observation.
Their patches will be merged. Help them agree on responsibilities, file regions and shared interfaces before
contested edits, resolve concrete overlaps or repeated failures during implementation, and decide when remaining
step/time budgets warrant a specific handoff or verification reminder. Same-file edits are only potential overlap.
Check the underlying actions and results before deciding whether to message anyone.
Use the supplied observations and replies; do not invent inspected code, agreement, completed tests or token budgets.
Use task_id and each worker's id/feature_id exactly as supplied; a feature is not a separate task ID.
Task requirements describe desired behavior, not proof of existing files or interfaces. Missing or truncated
evidence is unknown. When needed, ask a worker to inspect and report; do not fill gaps with plausible code details.
Your earlier notebook and advice are proposals, not independent evidence. Cite the supporting worker reply or
observed result briefly when recording agreement or verification. Silence and one worker claiming peer agreement
are not confirmation by both workers. Preserve conflicting reports as unresolved until clarified.
For a shared definition, propose one writer and have affected workers confirm the interface and ownership;
do not direct multiple workers to implement the same shared definition. Do not invent an API to make a plan concrete.
Allow independent work while unresolved shared decisions are clarified. Prefer no action without useful new advice.
Use remaining steps/time to prioritize feasible verification or handoff; do not start new work for an exhausted worker.
Use the provided tools to act. Do not describe an intended action in prose instead of calling its tool.
Call send_message at most once per worker. Keep each message under 500 characters: state one observed
fact and one concrete next action or question. Name the check and its scope; an import or partial
test does not verify a complete feature. Never prefix content with [Message from ...] or speak as another worker.
When there is no useful action, make no tool call.
"""

_SEND_MESSAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": "Send one coordination message to a worker in the supplied roster.",
        "parameters": {
            "type": "object",
            "properties": {"recipient": {"type": "string"}, "content": {"type": "string"}},
            "required": ["recipient", "content"],
            "additionalProperties": False,
        },
    },
}
_UPDATE_NOTEBOOK_TOOL = {
    "type": "function",
    "function": {
        "name": "update_notebook",
        "description": "Replace the complete shared Markdown notebook; workers receive its path and version.",
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class _FinishPlanning:
    version: int


@dataclass(frozen=True)
class _NotebookUpdate:
    content: str


@dataclass(frozen=True)
class _CoordinatorMessage:
    recipient: str
    content: str


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate tool argument: {key}")
        result[key] = value
    return result


class _Coordinator:
    """One serial decision stream, an optional shared notebook, and per-worker inboxes."""

    def __init__(
        self,
        envs: dict[str, Environment],
        model: str | None = None,
        *,
        assignments: list[Assignment],
        bus: TeamBus,
        notebook_path: Path | None = None,
        coordination_variant: str = "auto-prompt",
        coordination_mode: str = "direct",
        coordinator_implementation_enabled: bool = True,
        initial_plan: InitialPlan | None = None,
        planning_step_limit: int = 200,
        planning_time_limit_s: int = 600,
        worker_reminder_interval: int = 10,
        repo: str = "",
        task_id: int = 0,
        step_limit: int = 0,
        time_limit_s: int | None = None,
        trace=None,
        complete: Callable[[str], list[dict[str, str]]] | None = None,
    ) -> None:
        from cooperagents.planner import _default_planner_complete

        if coordination_variant not in ("auto-prompt", "human-prompt"):
            raise ValueError(f"Unknown coordination variant: {coordination_variant}")
        if coordination_variant == "human-prompt" and notebook_path is None:
            raise ValueError("Human-prompt coordination requires a notebook")
        if coordination_mode not in ("direct", "group-chat", "group-chat-planning"):
            raise ValueError(f"Unknown coordination mode: {coordination_mode}")
        if planning_step_limit < 1 or planning_time_limit_s < 1:
            raise ValueError("Planning budgets must be positive")
        if worker_reminder_interval < 0:
            raise ValueError("Worker reminder interval must be nonnegative")
        self.worker_reminder_interval = worker_reminder_interval
        self.planning_step_limit = planning_step_limit
        self.planning_time_limit_s = planning_time_limit_s
        self.coordination_mode = coordination_mode
        self.coordinator_implementation_enabled = coordinator_implementation_enabled
        self.initial_plan = initial_plan
        self._summary_delivered: set[str] = set()
        if initial_plan is not None and coordination_mode != "group-chat":
            raise ValueError("Initial plan injection requires group-chat")
        self.coordination_variant = coordination_variant
        self.trace = trace
        self.error: Exception | None = None
        self._envs = envs
        self._assignments = {a.agent_id: a for a in assignments}
        self._bus = bus
        self._repo, self._task_id = repo, task_id
        self._step_limit, self._time_limit_s = step_limit, time_limit_s
        self._agents: dict[str, Any] = {}
        self._finished: dict[str, str] = {}
        self._queues: dict[str, list[str]] = {aid: [] for aid in self._assignments}
        self._notified: dict[str, tuple[int, int]] = {}
        self._lock = threading.RLock()
        self._chat: list[dict] = []
        self._chat_positions = {aid: 0 for aid in (*self._assignments, "coordinator")}
        self._condition = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._planning = coordination_mode == "group-chat-planning" or initial_plan is not None
        self.phase = "planning" if self._planning else "implementation"
        self._planning_started: float | None = None
        self._planning_deadline: float | None = None
        self._planning_steps = {aid: 0 for aid in self._assignments}
        self._waiting_seconds = {aid: 0.0 for aid in self._assignments}
        self._coordinator_calls = 0
        self._participants: set[str] = set()
        self._confirmations: dict[str, int] = {}
        self._candidate_version: int | None = None
        self._requests: dict[str, dict] = {}
        self._plan_context: dict[str, tuple] = {}
        self._delivered: set[str] = set()
        self._final_plan: dict | None = None
        self._planning_outcome: str | None = None
        self._planning_reason: str | None = None
        self._frozen: dict | None = None
        self._finalizing_started: float | None = None
        self._planning_duration = 0.0
        self._finalizing_duration = 0.0
        self._stop = threading.Event()
        self._fired: list[dict] = []
        self._pending: list[dict] = []
        self._processed: tuple = ()
        self._last_error = ""
        self._git_sync: _GitShareSync | None = None
        self._notebook_path = notebook_path
        self._notebook = ""
        self._version = -1
        self._injected = complete is not None
        self._tools = [
            dict(
                _SEND_MESSAGE_TOOL,
                function={
                    **_SEND_MESSAGE_TOOL["function"],
                    "description": "Publish to everyone; recipient=group or a roster ID addresses the public message.",
                },
            )
            if coordination_mode != "direct"
            else _SEND_MESSAGE_TOOL,
            *([_UPDATE_NOTEBOOK_TOOL] if notebook_path is not None else []),
        ]
        if self._planning:
            self._tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "finish_planning",
                        "description": "Finish agreed opening planning; must be the only action.",
                        "parameters": {
                            "type": "object",
                            "properties": {"version": {"type": "integer"}},
                            "required": ["version"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        self._normal_tools = list(self._tools)
        self._complete = (
            complete
            if complete is not None
            else _default_planner_complete(
                model,
                None,
                None,
                trace=trace,
                timeout=60,
                max_retries=0,
                tools=self._tools,
            )
        )
        self._emit(
            "coordinator_start", model=model, interval_seconds=20, protocol="tool-calls-v1", notebook_enabled=notebook_path is not None
        )
        if notebook_path is not None:
            notebook_path.parent.mkdir(parents=True, exist_ok=True)
            notebook_path.parent.chmod(0o755)
            self.update_notebook(
                "# Coordination\n\n## Pending\n"
                + "\n".join(
                    f"- {aid}: inspect assigned feature {a.feature_id}; responsibilities are not yet confirmed."
                    for aid, a in self._assignments.items()
                )
            )

        self._initial_notebook = self._notebook

    def _emit(self, event: str, **data) -> None:
        if self.trace is not None:
            self.trace(event, **data)

    def worker_instructions(self) -> str:
        phase_protocol = PLANNING_SYSTEM if self.coordination_mode == "group-chat-planning" else ""
        if self.coordination_variant == "human-prompt":
            return "\n\n" + HUMAN_WORKER_WORKFLOW + (GROUP_CHAT if self.coordination_mode != "direct" else "") + phase_protocol
        note = ""
        if self._notebook_path is not None:
            note = (
                f"\nAfter acknowledging a new notice, read the coordinator's read-only notebook at {_COORDINATOR_PATH} "
                "before starting affected work, after an update notification, and after context compaction. "
                f"Use `cat {_COORDINATOR_PATH}`; the file is outside your code repository. "
                "Read the version in the file header; proposals still require confirmation."
            )
        return _COORDINATION_WORKFLOW + note + (GROUP_CHAT if self.coordination_mode != "direct" else "") + phase_protocol

    def publish(self, sender: str, recipient: str, content: str, *, synthetic: bool = False) -> None:
        """Canonical public record; callers bind sender rather than trusting model arguments."""
        with self._lock:
            if sender not in self._chat_positions:
                raise ValueError("Unknown group participant")
            recipients = ("group", *self._chat_positions)
            if recipient not in recipients:
                raise ValueError(f"Unsupported recipient {recipient!r}. Supported values: {', '.join(recipients)}.")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Group message must be nonempty text")
            if self._stop.is_set() or sender in self._finished:
                raise ValueError("Group participant has finished")
            if len(content) > 1200:
                suffix = "\n[truncated: message exceeded 1200 characters]"
                content = content[: 1200 - len(suffix)] + suffix
            row = dict(message_id=len(self._chat) + 1, sender=sender, addressed_to=recipient, content=content)
            self._chat.append(row)
            if self._planning and self.phase == "planning" and sender in self._assignments and not synthetic:
                self._participants.add(sender)
                if sender in self._confirmations:
                    del self._confirmations[sender]
                    self._emit("plan_confirmation", target=sender, accepted=False, reason="new_discussion", version=self._version)
                self._condition.notify_all()
                self._wake.set()
            provenance = {"synthetic": True} if synthetic else {}
            self._emit("group_message", **row, phase=self.phase, **provenance)

    @staticmethod
    def _chat_text(row: dict) -> str:
        return f"[group #{row['message_id']}; {row['sender']} -> {row['addressed_to']}] {row['content']}"

    def _chat_page(self, position: int) -> list[dict]:
        page, size = [], 0
        for row in self._chat[position:]:
            length = len(self._chat_text(row)) + 1
            if size + length > 8000:
                break
            page.append(row)
            size += length
        return page

    def _recent_chat(self, end: int | None = None) -> list[dict]:
        page, size = [], 0
        for row in reversed(self._chat[:end]):
            length = len(self._chat_text(row)) + 1
            if size + length > 8000:
                break
            page.append(row)
            size += length
        return list(reversed(page))

    def restore_context(self, agent_id: str, agent, *, request: bool = False) -> None:
        with self._lock:
            # Overflow retries remain the same logical request, even after finalization.
            text = self._requests.get(agent_id, {}).get("phase_text") if request else None
            if text:
                agent.add_messages(agent.model.format_message(role="user", content=text))
        if self.coordination_mode == "direct":
            return
        with self._lock:
            recent = self._recent_chat(self._chat_positions[agent_id])
        if recent:
            agent.add_messages(
                agent.model.format_message(
                    role="user", content="Recent public conversation (restored):\n" + "\n".join(self._chat_text(row) for row in recent)
                )
            )
            self._emit("group_delivery", target=agent_id, message_ids=[r["message_id"] for r in recent], restored=True)

    def inject_initial_plan(self) -> None:
        """Skip model planning, retain honest provenance and the native delivery barrier."""
        plan = self.initial_plan
        if plan is None or self._agents or self._chat or self._final_plan is not None:
            raise ValueError("Initial plan can only be injected once before workers start")
        plan.validate_assignments(self._repo, self._task_id, list(self._assignments.values()))
        for aid, env in self._envs.items():
            result = env.execute("git rev-parse HEAD", timeout=10)
            if result.exit_code or result.stdout.strip() != plan.source_base_commit:
                raise ValueError(f"Initial plan base source mismatch for {aid}")
            if env.execute("git diff --quiet HEAD", timeout=10).exit_code:
                raise ValueError(f"Initial plan requires unchanged base code for {aid}")
        with self._condition:
            self._planning_started = time.time()
            self._planning_deadline = self._planning_started  # Synthetic preparation has no rollout budget.
            for message in plan.history:
                self.publish(message.sender, message.addressed_to, message.content, synthetic=True)
            self.update_notebook(plan.content)
            self._emit(
                "initial_plan_injected", pair=plan.pair, source_base_commit=plan.source_base_commit,
                bundle_sha256=plan.bundle_sha256, synthetic=True, history_message_ids=list(range(1, len(plan.history) + 1)),
                feature_sha256=dict(plan.feature_sha256),
            )
            self._set_final_plan_locked("injected")

    def start_planning(self) -> None:
        with self._condition:
            if self._planning and self._planning_started is None:
                self._planning_started = time.time()
                self._planning_deadline = self._planning_started + self.planning_time_limit_s
                self._emit("planning_start", phase=self.phase, step_limit=self.planning_step_limit, time_limit_s=self.planning_time_limit_s)

    def _planning_snapshot_locked(self) -> dict:
        return dict(
            phase=self.phase,
            planning_steps=dict(self._planning_steps),
            step_limit=self.planning_step_limit,
            seconds_remaining=max(0, self._planning_deadline - time.time())
            if self._planning_deadline is not None
            else self.planning_time_limit_s,
            time_limit_s=self.planning_time_limit_s,
            participants=sorted(self._participants),
            confirmations=dict(self._confirmations),
            candidate_version=self._candidate_version,
            reason=self._planning_reason,
            outcome=self._planning_outcome,
            final_plan=copy.deepcopy(self._final_plan),
        )

    def _fail_planning_locked(self, reason: str) -> None:
        if self.phase == "failed":
            return
        now = time.time()
        if self.phase == "planning" and self._planning_started is not None:
            self._planning_duration = now - self._planning_started
        elif self.phase == "finalizing" and self._finalizing_started is not None:
            self._finalizing_duration = now - self._finalizing_started
        self.phase = "failed"
        self._planning_outcome, self._planning_reason = "error", reason
        self._emit("planning_failed", reason=reason, phase=self.phase, budget=self._planning_snapshot_locked())
        self._condition.notify_all()
        self._wake.set()

    def _check_planning_limits_locked(self) -> None:
        if not self._planning or self.phase not in ("planning", "finalizing", "implementation"):
            return
        if self.phase == "implementation" and len(self._delivered) == len(self._assignments):
            return
        if self._planning_started is None:
            return
        now = time.time()
        for aid, agent in self._agents.items():
            if (
                agent.config.wall_deadline is not None
                and now >= agent.config.wall_deadline
                or 0 < agent.config.step_limit <= agent.n_calls
                or 0 < agent.config.cost_limit <= agent.cost
            ):
                self._fail_planning_locked(f"total_budget:{aid}")
                return
        if self.phase != "planning":
            return
        reason = (
            "time_limit"
            if now >= self._planning_deadline
            else "step_limit"
            if any(n >= self.planning_step_limit for n in self._planning_steps.values())
            else None
        )
        if reason:
            cursor = self._chat_positions["coordinator"]
            selected = {row["message_id"]: row for row in self._recent_chat()}
            for aid in self._assignments:
                report = next((row for row in reversed(self._chat) if row["sender"] == aid), None)
                if report is not None:
                    selected[report["message_id"]] = report
            chat = [dict(selected[mid]) for mid in sorted(selected)]
            self.phase = "finalizing"
            self._planning_reason = reason
            self._frozen = dict(
                notebook={"version": self._version, "content": self._notebook},
                group_chat={
                    "unread": [row for row in chat if row["message_id"] > cursor],
                    "recent": [row for row in chat if row["message_id"] <= cursor],
                    "backlog": len(self._chat) - cursor,
                },
                workers=[dict(id=aid, feature_id=a.feature_id, task=self._clip(a.task, 6000)) for aid, a in self._assignments.items()],
                planning=self._planning_snapshot_locked(),
            )
            self._finalizing_started = now
            self._planning_duration = now - self._planning_started
            self._emit("planning_finalizing", reason=reason, frozen=self._frozen)
            self._condition.notify_all()
            self._wake.set()

    def _set_final_plan_locked(self, outcome: str) -> None:
        import hashlib

        now = time.time()
        self._planning_duration = (self._finalizing_started or now) - self._planning_started
        self._finalizing_duration = now - self._finalizing_started if self._finalizing_started is not None else 0
        self._final_plan = dict(
            version=self._version,
            content=self._notebook,
            outcome=outcome,
            confirmations=dict(self._confirmations),
            sha256=hashlib.sha256(self._notebook.encode()).hexdigest(),
        )
        self._planning_outcome = outcome
        self.phase = "implementation"
        self._tools[:] = [tool for tool in self._tools if tool["function"]["name"] != "finish_planning"]
        self._emit("planning_end", **self._final_plan, reason=self._planning_reason, planning_steps=dict(self._planning_steps))
        self._condition.notify_all()

    def finish_planning(self, version: int) -> None:
        with self._condition:
            self._check_planning_limits_locked()
            if (
                self.phase != "planning"
                or self._finished
                or version != self._candidate_version
                or version != self._version
                or set(self._confirmations) != set(self._assignments)
                or any(v != version for v in self._confirmations.values())
                or getattr(self, "_observed_confirmations", {}) != self._confirmations
            ):
                raise ValueError("finish_planning requires current observed confirmations from both live workers")
            self._set_final_plan_locked("agreed")

    def confirm_plan(self, aid: str, action: dict, *, sole: bool) -> dict:
        with self._condition:
            version = action.get("version")
            request = self._requests.get(aid, {})
            reason = "accepted"
            if not sole:
                reason = "confirm_plan must be the only tool action"
            elif self.phase != "planning" or request.get("phase") != "planning" or aid in self._finished:
                reason = "expired planning request"
            elif (
                type(version) is not int
                or version != self._version
                or version != self._candidate_version
                or request.get("notebook_version") != version
                or not self._notebook.strip()
            ):
                reason = "version was not the current candidate delivered in this request"
            accepted = reason == "accepted"
            if accepted:
                self._confirmations[aid] = version
                self._wake.set()
            self._emit("plan_confirmation", target=aid, version=version, accepted=accepted, reason=reason)
            self._condition.notify_all()
            return {"output": f"Plan confirmation: {reason}", "returncode": 0 if accepted else 1, "exception_info": ""}

    def _implementation_text(self) -> str:
        text = IMPLEMENTATION_MODE.format(**self._final_plan)
        if self.initial_plan is not None:
            text = text.replace("Planning outcome: injected\n", "")
        return text

    def _restore_plan_locked(self, aid: str, agent) -> None:
        if not self._planning or self.phase in ("finalizing", "failed"):
            return
        marker = (self.phase, self._version if self.phase == "planning" else self._final_plan["version"], agent._compaction_count)
        if self._plan_context.get(aid) != marker:
            if self.phase == "planning":
                text = (
                    PLANNING_MODE
                    + f"\nCurrent notebook version: {self._version}\n<planning_notebook>\n{self._notebook}\n</planning_notebook>"
                )
            else:
                if self.initial_plan is not None and aid not in self._summary_delivered:
                    summary = dict(self.initial_plan.worker_summaries).get(aid)
                    if summary is not None:
                        agent.add_messages(agent.model.format_message(
                            role="assistant", content=summary, extra={"summary": True},
                        ))
                        self._summary_delivered.add(aid)
                        self._emit("worker_summary_delivery", target=aid, content=summary, synthetic=True)
                text = self._implementation_text()
            agent.add_messages(agent.model.format_message(role="user", content=text))
            self._plan_context[aid] = marker
            if self.phase == "implementation" and aid not in self._delivered:
                self._delivered.add(aid)
                self._emit(
                    "plan_delivery",
                    target=aid,
                    version=self._final_plan["version"],
                    sha256=self._final_plan["sha256"],
                    content=self._final_plan["content"],
                )
                self._condition.notify_all()

    def worker_reminder(self, aid: str, decision: int) -> str | None:
        """Fixed cadence over worker decisions, including both execution phases."""
        if not self.worker_reminder_interval or decision % self.worker_reminder_interval:
            return None
        with self._lock:
            assignment = self._assignments[aid]
            task = (
                f"Your assigned task is feature {assignment.feature_id}."
                if assignment.feature_id is not None
                else "Your assigned task is the original task provided at the start."
            )
            notebook = (
                "Latest coordination notebook:\n"
                f"- Version: {self._version}\n"
                "- Path: /coordination/notebook.md\n\n"
                f"Follow the notebook entries addressed to {aid}.\n"
                "If you have not read this version, or no longer remember your\n"
                "assignment, read the notebook before continuing.\n"
                if self._notebook_path is not None
                else "No coordination notebook is enabled.\n"
            )
            return (
                f"[HARNESS REMINDER]\nYou are {aid}, a worker.\n{task}\n"
                f"Current phase: {self.phase}.\n\n{notebook}"
                "Your original feature requirements still define the task goal."
            )

    def before_query(self, aid: str, agent) -> dict:
        with self._condition:
            while self._planning:
                self._check_planning_limits_locked()
                if self.phase == "failed" or self._stop.is_set():
                    raise RuntimeError(f"planning_failed: {self._planning_reason}")
                self._restore_plan_locked(aid, agent)
                if self.phase == "implementation" and self._delivered == set(self._assignments):
                    break
                if self.phase == "planning":
                    previous_position = self._requests.get(aid, {}).get("chat_position", 0)
                    unread = self._chat_page(self._chat_positions[aid])
                    if unread and self._chat_positions[aid] == previous_position:
                        for text in self.drain(aid):
                            agent.add_messages(agent.model.format_message(role="user", content=text))
                    if self._confirmations.get(aid) != self._version or self._chat_positions[aid] > previous_position:
                        break
                deadlines = [agent.config.wall_deadline, self._planning_deadline if self.phase == "planning" else None]
                remaining = min([d - time.time() for d in deadlines if d is not None] or [1])
                waited_at = time.time()
                self._condition.wait(timeout=max(0.001, min(remaining, 1)))
                self._waiting_seconds[aid] += max(0, time.time() - waited_at)
            snapshot = dict(
                phase=self.phase,
                notebook_version=self._version,
                planning_steps=dict(self._planning_steps),
                steps_used=agent.n_calls,
                seconds_remaining=max(0, agent.config.wall_deadline - time.time()) if agent.config.wall_deadline is not None else None,
            )
            snapshot["chat_position"] = self._chat_positions[aid]
            if self._planning:
                snapshot["phase_text"] = (
                    PLANNING_MODE + f"\nCurrent notebook version: {self._version}\n"
                    f"<planning_notebook>\n{self._notebook}\n</planning_notebook>"
                    if self.phase == "planning"
                    else self._implementation_text()
                )
            self._requests[aid] = snapshot
            if self._planning and self.phase == "planning":
                self._planning_steps[aid] += 1
            self._emit("worker_request", target=aid, **{k: v for k, v in snapshot.items() if k != "phase_text"})
            return snapshot

    def planning_metrics(self) -> dict:
        with self._lock:
            return dict(
                coordination_mode=self.coordination_mode,
                coordinator_implementation_enabled=self.coordinator_implementation_enabled,
                group_messages=len(self._chat),
                coordinator_calls=self._coordinator_calls,
                group_delivery_positions=dict(self._chat_positions),
                **(
                    {
                        "planning": dict(
                            outcome=self._planning_outcome,
                            reason=self._planning_reason,
                            final_plan=copy.deepcopy(self._final_plan),
                            delivered=sorted(self._delivered),
                            steps=dict(self._planning_steps),
                            waiting_seconds=dict(self._waiting_seconds),
                            discussion_seconds=self._planning_duration,
                            finalizing_seconds=self._finalizing_duration,
                        )
                    }
                    if self._planning
                    else {}
                ),
            )

    def verify_mounts(self) -> None:
        if self._notebook_path is not None:
            expected = self._notebook_path.read_text(encoding="utf-8")
            for aid, env in self._envs.items():
                result = env.execute(f"cat {_COORDINATOR_PATH}", timeout=10)
                if result.exit_code or result.stdout != expected:
                    raise RuntimeError(f"Coordinator notebook is not mounted/readable for {aid}: {_COORDINATOR_PATH}")

    def register(self, agent_id: str, agent: Any) -> None:
        with self._lock:
            if agent_id not in self._assignments or agent_id in self._finished:
                raise ValueError(f"Invalid coordinator worker registration: {agent_id}")
            self._agents[agent_id] = agent
            self._condition.notify_all()
            self._emit("register", target=agent_id)

    def mark_finished(self, agent_id: str, status: str) -> None:
        with self._lock:
            if self._planning and (self.phase != "implementation" or len(self._delivered) != len(self._assignments)):
                self._fail_planning_locked(f"worker_exit:{agent_id}:{status}")
            self._finished[agent_id] = status
            dropped = self._queues[agent_id]
            self._queues[agent_id] = []
            self._emit("worker_finished", target=agent_id, status=status, dropped=dropped)
            if len(self._finished) == len(self._assignments):
                self._stop.set()

    def drain(self, agent_id: str) -> list[str]:
        with self._lock:
            if agent_id in self._finished or self._stop.is_set():
                return []
            out = self._queues[agent_id]
            if self.coordination_mode != "direct":
                page = self._chat_page(self._chat_positions[agent_id])
                if page:
                    self._chat_positions[agent_id] = page[-1]["message_id"]
                    out = [*out, *[self._chat_text(row) for row in page]]
                    self._emit("group_delivery", target=agent_id, message_ids=[r["message_id"] for r in page], restored=False)
            self._queues[agent_id] = []
            compactions = getattr(self._agents.get(agent_id), "_compaction_count", 0)
            marker = (self._version, compactions)
            if self._notebook_path is not None and self._notified.get(agent_id) != marker:
                self._notified[agent_id] = marker
                notice = (
                    f"[coordinator] Notebook v{self._version} is available. Read the latest file at "
                    f"{_COORDINATOR_PATH} (`cat {_COORDINATOR_PATH}`) before continuing affected work."
                )
                self._emit("notebook_delivery", target=agent_id, version=self._version, text=notice, compactions=compactions)
                out = [notice, *out]
            if out:
                self._emit("delivery", target=agent_id, messages=out)
            return out

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._fired)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            if self._planning and self.phase not in ("implementation", "failed"):
                self._fail_planning_locked("stopped")
            self._condition.notify_all()
            self._wake.set()

    def update_notebook(self, content: str) -> None:
        path = self._notebook_path
        if path is None:
            raise ValueError("Notebook updates are disabled")
        if content == self._notebook:
            return
        version = self._version + 1
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
                temporary = handle.name
                handle.write(f"<!-- coordinator notebook v{version} -->\n{content}\n")
                os.fchmod(handle.fileno(), 0o644)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        with self._lock:
            previous = self._version
            self._version, self._notebook = version, content
            if self._planning and self.phase == "planning":
                for aid in self._confirmations:
                    self._emit("plan_confirmation", target=aid, accepted=False, reason="notebook_revised", version=version)
                self._confirmations.clear()
                self._candidate_version = version if self._participants == set(self._assignments) else None
                self._condition.notify_all()
        self._emit("notebook_update", previous_version=previous, version=version, content=content)

    def _parse_actions(self, response: list[dict[str, str]]) -> list[_NotebookUpdate | _CoordinatorMessage | _FinishPlanning]:
        if not isinstance(response, list):
            raise ValueError("Expected tool calls")
        if len(response) > len(self._assignments) + 1:
            raise ValueError("Too many actions")
        parsed: list[_NotebookUpdate | _CoordinatorMessage | _FinishPlanning] = []
        recipients: set[str] = set()
        updated = False
        for call in response:
            if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
                raise ValueError("Expected a named tool call with arguments")
            kind, arguments = call["name"], call["arguments"]
            if not isinstance(arguments, str) or len(arguments) > 65536:
                raise ValueError("Tool arguments must be JSON text of at most 65536 characters")
            try:
                item = json.loads(arguments, object_pairs_hook=_unique_json_object)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid tool arguments") from exc
            if not isinstance(item, dict):
                raise ValueError("Tool arguments must be an object")
            if kind == "finish_planning":
                version = item.get("version")
                if not self._planning or len(response) != 1 or set(item) != {"version"} or type(version) is not int:
                    raise ValueError("finish_planning requires a sole integer version action")
                parsed.append(_FinishPlanning(version))
                continue
            content = item.get("content")
            limit = 8000 if kind == "update_notebook" else 1200
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Action content must be nonempty text")
            if len(content) > limit:
                label = "notebook" if kind == "update_notebook" else "message"
                notice = f"\n[truncated: {label} exceeded {limit} characters]"
                content = content[: limit - len(notice)] + notice
            if kind == "update_notebook" and set(item) == {"content"}:
                if updated or self._notebook_path is None:
                    raise ValueError("Notebook update is disabled or repeated")
                updated = True
                parsed.append(_NotebookUpdate(content))
            elif kind == "send_message" and set(item) == {"recipient", "content"}:
                if content.lstrip().startswith("[Message from "):
                    raise ValueError("Coordinator message impersonates a worker")
                recipient = item["recipient"]
                supported = ("group", *self._assignments, "coordinator") if self.coordination_mode != "direct" else tuple(self._assignments)
                if not isinstance(recipient, str) or recipient not in supported:
                    raise ValueError(f"Unsupported recipient {recipient!r}. Supported values: {', '.join(supported)}.")
                if recipient in recipients:
                    raise ValueError("Repeated message recipient")
                recipients.add(recipient)
                parsed.append(_CoordinatorMessage(recipient, content))
            else:
                raise ValueError("Unknown action or fields")
        return parsed

    def _apply_actions(self, actions: list[_NotebookUpdate | _CoordinatorMessage | _FinishPlanning]) -> None:
        if actions and isinstance(actions[0], _FinishPlanning):
            self.finish_planning(actions[0].version)
            return
        previous_version = self._version
        for action in actions:
            if isinstance(action, _NotebookUpdate):
                self.update_notebook(action.content)
        notebook_updated = self._version != previous_version
        with self._lock:
            for action in actions:
                if not isinstance(action, _CoordinatorMessage):
                    continue
                if self.coordination_mode != "direct":
                    if self._stop.is_set():
                        self._emit("message_dropped", target=action.recipient, reason="stopped", content=action.content)
                    else:
                        self.publish("coordinator", action.recipient, action.content)
                    continue
                if self._stop.is_set() or action.recipient in self._finished:
                    self._emit("message_dropped", target=action.recipient, reason="worker_finished_or_stopped", content=action.content)
                    continue
                version = self._version if self._notebook_path is not None else None
                if self.coordination_variant == "human-prompt":
                    instruction = (
                        "Notebook updated! Read /coordination/notebook.md before continuing affected work."
                        if notebook_updated else "Read /coordination/notebook.md if needed."
                    )
                    text = (
                        f"[COORDINATION NOTEBOOK]\nVersion: {version}\n"
                        f"{instruction}\n\n"
                        f"[FROM COORDINATOR]\n{action.content}\n[END COORDINATOR MESSAGE]"
                    )
                else:
                    label = f"[coordinator; notebook v{version}]" if version is not None else "[coordinator]"
                    text = f"{label} {action.content}"
                self._queues[action.recipient].append(text)
                self._fired.append({"agent": action.recipient, "kind": "message", "notebook_version": version})
                self._emit("nudge", target=action.recipient, kind="message", text=text, notebook_version=version)

    @staticmethod
    def _clip(value: str, limit: int = 1000) -> str:
        return value if len(value) <= limit else value[:limit] + " [truncated]"

    @classmethod
    def _recent_actions(cls, messages: list[dict]) -> list[dict]:
        batches: list[tuple[list[dict], list[dict]]] = []
        for message in messages:
            extra = message.get("extra") or {}
            if message.get("role") == "assistant":
                actions = extra.get("actions")
                if "actions" not in extra:
                    actions = []
                    for call in message.get("tool_calls") or []:
                        try:
                            fn = call["function"]
                            args = json.loads(fn["arguments"])
                            actions.append({**args, "tool_name": fn.get("name", "bash"), "tool_call_id": call.get("id")})
                        except (KeyError, TypeError, ValueError):
                            continue
                batches.append((actions if isinstance(actions, list) else [], []))
            elif batches and "raw_output" in extra:
                batches[-1][1].append(message)
        records = []
        for actions, outputs in batches:
            by_id = {o["tool_call_id"]: o for o in outputs if o.get("tool_call_id")}
            for index, action in enumerate(actions):
                if not isinstance(action, dict):
                    continue
                call_id = action.get("tool_call_id")
                output = (
                    by_id.get(call_id)
                    if call_id
                    else (outputs[index] if index < len(outputs) and not outputs[index].get("tool_call_id") else None)
                )
                record: dict[str, Any] = {
                    "action": {k: cls._clip(v) if isinstance(v, str) and k != "tool_call_id" else v for k, v in action.items()},
                    "result": None,
                }
                if output is not None:
                    extra = output["extra"]
                    raw = str(extra.get("raw_output", ""))
                    record["result"] = {
                        "returncode": extra.get("returncode"),
                        "output": raw if len(raw) <= 1000 else raw[:300] + "\n[truncated]\n" + raw[-700:],
                        "output_truncated": len(raw) > 1000,
                        "output_chars": len(raw),
                        "exception_info": cls._clip(str(extra.get("exception_info") or "")),
                    }
                    command = action.get("command")
                    code = extra.get("returncode")
                    if (
                        action.get("tool_name") == "bash" and isinstance(command, str) and command.strip()
                        and isinstance(code, int) and not isinstance(code, bool)
                        and extra.get("exception_info") != "action was not executed"
                        and "[output truncated after " not in raw
                    ):
                        record["_loop_signature"] = (command, code, raw)
                records.append(record)
        recent = records[-6:]
        for record in recent:
            signature = record.get("_loop_signature")
            if signature is not None:
                record["_loop_signature"] = (signature[0], signature[1], hashlib.sha256(signature[2].encode()).hexdigest())
        return recent

    def _dirty_files(self, aid: str) -> list[str] | None:
        from cooperagents.trajectory import record_call

        # --no-renames gives both paths for a rename without ambiguous quoted names.
        result = record_call(
            self.trace, self._envs[aid].execute, command="git -c core.quotepath=false status --porcelain=v1 -z --no-renames", timeout=5
        )
        if result.exit_code:
            return None
        return sorted(
            {
                entry[3:]
                for entry in result.stdout.split("\0")
                if len(entry) > 3 and not entry[3:].startswith(".cb_") and entry[3:] != "patch.txt"
            }
        )

    @staticmethod
    def _add_warnings(workers: list[dict], git_evidence: GitEvidence | None = None) -> None:
        for worker in workers:
            warnings = worker["warnings"] = []
            actions = worker["recent_actions"]
            signatures = [record.pop("_loop_signature", None) for record in actions]
            counts = Counter(signature for signature in signatures if signature is not None)
            if counts and max(counts.values()) >= 4:
                warnings.append("LOOP: repeated action with unchanged result")
            results = [record["result"] for record in actions if record["result"] is not None]
            if len(results) >= 4:
                recent = results[-4:]
                output = str(recent[-1]["output"])[:120]
                if ("rror" in output or recent[-1]["returncode"] not in (None, 0)) and all(
                    str(result["output"])[:120] == output for result in recent
                ):
                    warnings.append("STALL: the last 4 tool results repeat the same error")
            files = set(worker["modified_files"] or [])
            for peer in workers:
                if peer is worker:
                    continue
                current_common = files.intersection(peer["modified_files"] or [])
                pair = tuple(sorted((worker["id"], peer["id"])))
                comparison = git_evidence.comparisons.get(pair) if git_evidence else None
                overlaps = comparison.overlaps if comparison and comparison.error is None else {}
                unknown_snapshot = set(comparison.unknown) if comparison else set()
                covered = comparison.covered if comparison and comparison.error is None else frozenset()
                unknown_current = current_common.difference(covered)
                for path in sorted(set(overlaps) | unknown_snapshot | unknown_current)[:3]:
                    quoted = json.dumps(path, ensure_ascii=False)
                    if path in overlaps:
                        warnings.append(
                            f"POTENTIAL_CONFLICT: possible overlapping edits with {peer['id']}: {quoted} "
                            f"({pair[0]}/{pair[1]}: {overlaps[path]}; source=snapshot)"
                        )
                    else:
                        source = "snapshot" if path in unknown_snapshot else "current path observation"
                        warnings.append(
                            f"POTENTIAL_CONFLICT: same file modified; regions unknown with {peer['id']}: {quoted} (source={source})"
                        )

    def decide(self, *, initial: bool = False) -> None:
        with self._lock:
            if self.phase == "implementation" and not self.coordinator_implementation_enabled:
                return
        replies = self._bus.receive("coordinator")
        self._pending.extend(replies)
        if replies:
            self._emit("replies", messages=replies)
        with self._lock:
            if self._stop.is_set():
                return
            self._check_planning_limits_locked()
            if self.phase == "failed":
                return
            forced = self._planning and self.phase == "finalizing"
            decision_phase = self.phase
            decision_version = self._version
            planning = self._planning_snapshot_locked() if self._planning else None
            if self.initial_plan is not None:
                planning = {
                    "phase": self.phase,
                    "final_plan": {key: self._final_plan[key] for key in ("version", "content", "sha256")},
                }
            self._observed_confirmations = dict(self._confirmations)
            agents, finished = dict(self._agents), dict(self._finished)
            version, notebook = self._version, self._notebook
            chat_page = self._chat_page(self._chat_positions["coordinator"]) if self.coordination_mode != "direct" else []
            recent_chat = self._recent_chat(self._chat_positions["coordinator"])
            chat_backlog = len(self._chat) - self._chat_positions["coordinator"]
        progress = tuple(
            (
                aid,
                getattr(agents.get(aid), "n_calls", 0),
                len(getattr(agents.get(aid), "messages", [])),
                getattr(agents.get(aid), "_compaction_count", 0),
                finished.get(aid),
            )
            for aid in self._assignments
        )
        planning_marker = (decision_phase, decision_version, tuple(sorted(self._confirmations.items())))
        if (
            not initial
            and not forced
            and not self._pending
            and not chat_page
            and progress == self._processed
            and (not self._planning or planning_marker == getattr(self, "_processed_planning", None))
        ):
            return
        workers = []
        for aid, assignment in self._assignments.items():
            agent = agents.get(aid)
            config = getattr(agent, "config", None)
            used = getattr(agent, "n_calls", 0)
            limit = getattr(config, "step_limit", self._step_limit)
            deadline = getattr(config, "wall_deadline", None)
            remaining = max(0, deadline - time.time()) if deadline is not None else (self._time_limit_s if agent is None else None)
            files = None
            if aid not in finished:
                try:
                    files = self._dirty_files(aid)
                except Exception as exc:  # observations may be unavailable; do not invent an empty edit set
                    self._emit("observation_error", target=aid, error=str(exc))
            workers.append(
                {
                    "id": aid,
                    "feature_id": assignment.feature_id,
                    "task": self._clip(assignment.task, 6000),
                    "status": finished.get(aid, "running" if agent is not None else "not_started"),
                    "steps_used": used,
                    "steps_remaining": max(0, limit - used) if limit else None,
                    "seconds_remaining": remaining,
                    "modified_files": files,
                    "recent_actions": self._recent_actions(list(getattr(agent, "messages", []))),
                }
            )
        git_evidence = self._git_sync.evidence() if self._git_sync is not None else GitEvidence()
        self._add_warnings(workers, git_evidence)
        observation = {
            "repo": self._repo,
            "task_id": self._task_id,
            "initial": initial,
            "workers": workers,
        }
        if not forced:
            observation["git_snapshots"] = git_evidence.metadata()
        if self.coordination_mode == "direct":
            observation["replies"] = list(self._pending)
        observation["previous_error"] = self._last_error
        if self.coordination_variant == "human-prompt":
            protocol = HUMAN_COORDINATOR_BASE + "\n"
            if initial:
                protocol += "\n" + HUMAN_COORDINATOR_INITIAL + "\n"
        else:
            protocol = _COORDINATOR_PROMPT
            if initial:
                protocol += (
                    "\nINITIAL DECISION: Workers have not started inspecting code. Request inspection and a report of "
                    "proposed files, regions, shared interfaces and dependencies to peers and coordinator. "
                    "You may suggest provisional responsibilities based on the supplied feature requirements, "
                    "but leave concrete edit boundaries and ownership pending worker evidence and confirmation. "
                    "Do not guess filenames, existing symbols or new shared APIs, or declare coordination complete.\n"
                )
        protocol += "\n" + COORDINATOR_WARNING_RULES
        if self._notebook_path is not None:
            observation["notebook"] = {"version": version, "content": notebook}
            if self.coordination_variant == "auto-prompt":
                protocol += (
                    "Call update_notebook at most once, with the complete Markdown (at most 8000 characters).\n"
                    "Prefer a short notebook (about 1000-2000 characters) of current responsibilities/regions, "
                    "interfaces/dependencies, pending issues and handoff. Preserve unresolved questions when replacing it.\n"
                    "Distinguish proposed, worker-reported, confirmed (explicit replies) and verified (observed checks).\n"
                    f"Workers read {_COORDINATOR_PATH}; an update automatically sends a path/version reminder, not the full text.\n"
                )
        else:
            protocol += "Notebook is disabled; only send_message is available.\n"
        if self.coordination_mode != "direct":
            protocol += GROUP_CHAT
            observation["group_chat"] = {
                "unread": chat_page,
                "recent": recent_chat,
                "backlog": chat_backlog,
            }
        if self._planning:
            observation["planning"] = planning
            if forced:
                observation.update(copy.deepcopy(self._frozen))
                observation["initial"] = False
                protocol += FORCED_PLANNING
            elif self.phase == "planning":
                protocol += COORDINATOR_PLANNING
        prompt = protocol + "\nOBSERVATION:\n" + json.dumps(observation, ensure_ascii=False)
        count = len(self._pending)
        self._emit("observation", **observation)
        try:
            if self._complete is None:
                raise ConnectionError("Coordinator completion client unavailable")
            if forced:
                self._tools[:] = [_UPDATE_NOTEBOOK_TOOL]
            self._coordinator_calls += 1
            if forced and not self._injected:
                deadlines = [agent.config.wall_deadline for agent in agents.values() if agent.config.wall_deadline is not None]
                remaining = min([d - time.time() for d in deadlines] or [60])
                response = self._complete(prompt, timeout=max(0.001, min(60, remaining)))
            else:
                response = self._complete(prompt)
        except Exception as exc:
            self._last_error = f"Completion failed: {type(exc).__name__}"
            self._emit("decision", initial=initial, valid=False, outcome="transport_error", error=str(exc))
            if forced:
                with self._condition:
                    self._fail_planning_locked(self._last_error)
                return
            if self._injected and not self._planning:
                raise
            return
        try:
            if forced and (not isinstance(response, list) or len(response) != 1 or response[0].get("name") != "update_notebook"):
                raise ValueError("Finalization requires exactly one notebook update")
            actions = self._parse_actions(response)
            if forced and (
                len(actions[0].content) > 8000 or "[truncated:" in actions[0].content or actions[0].content == self._initial_notebook
            ):
                raise ValueError("Finalization requires a complete final notebook")
        except (ValueError, TypeError, RecursionError) as exc:
            self._last_error = str(exc)
            self._emit("decision", initial=initial, valid=False, outcome="invalid_response", response=response, error=str(exc))
            if forced:
                with self._condition:
                    self._fail_planning_locked(self._last_error)
                return
            if self._injected and not self._planning:
                raise
            return
        self._emit("decision", initial=initial, valid=True, outcome="actions" if actions else "no_op", response=response)
        with self._condition:
            self._check_planning_limits_locked()
            if self._planning and (self.phase != decision_phase or self._version != decision_version):
                self._emit("decision_expired", phase=decision_phase, current_phase=self.phase)
                return
            try:
                self._apply_actions(actions)
                if forced:
                    self._set_final_plan_locked("forced")
                    self._tools[:] = [tool for tool in self._normal_tools if tool["function"]["name"] != "finish_planning"]
            except Exception as exc:
                if self._planning and self.phase in ("planning", "finalizing", "failed"):
                    if isinstance(exc, ValueError) and not forced:
                        self._last_error = str(exc)
                        self._emit("decision", initial=initial, valid=False, outcome="invalid_action", error=str(exc))
                    else:
                        self._fail_planning_locked(f"plan_action_failed:{exc}")
                    return
                raise
        if chat_page and not forced:
            with self._lock:
                self._chat_positions["coordinator"] = chat_page[-1]["message_id"]
            self._emit("group_delivery", target="coordinator", message_ids=[r["message_id"] for r in chat_page], restored=False)
        del self._pending[:count]
        self._processed = progress
        self._processed_planning = (self.phase, self._version, tuple(sorted((planning or {}).get("confirmations", {}).items())))
        self._last_error = ""

    def run(self) -> None:
        next_decision = time.monotonic() + 20
        try:
            while not self._stop.is_set():
                if self._planning and self.phase in ("planning", "finalizing"):
                    triggered = self._wake.wait(0.1)
                    self._wake.clear()
                    with self._condition:
                        self._check_planning_limits_locked()
                    if triggered or self.phase == "finalizing" or time.monotonic() >= next_decision:
                        self.decide()
                        next_decision = time.monotonic() + 20
                elif not self._stop.wait(20):
                    self.decide()
        except Exception as exc:
            if self._planning and self.phase != "implementation":
                with self._condition:
                    self._fail_planning_locked(f"coordinator_error:{exc}")
            else:
                self.error = exc

    def finish(self, thread: threading.Thread) -> None:
        """Always join the started monitor before its environments are torn down."""
        self.stop()
        thread.join(timeout=600)
        if thread.is_alive():
            raise RuntimeError("Coordinator still running; episode is incomplete")
        if self.error is not None:
            raise RuntimeError("Coordinator failed; episode is incomplete") from self.error


_GITSHARE = "/cbshared/repo.git"


class _TeammatePoller:
    """TK2/Q9: pushed teammate awareness for parallel agents.

    poll() (called by the agent loop each step via the ``team_poller`` hook)
    reports which files each TEAMMATE is currently editing — but only when
    that set CHANGES, so the token cost stays near zero."""

    def __init__(self, self_id: str, envs: dict[str, Environment]) -> None:
        self._self = self_id
        self._envs = envs
        self._last: dict[str, str] = {}
        self._board_bus = None
        self._board_last = ""
        self._coordinator = None
        self._gitshare = False
        self._gitshare_last: dict[str, str] = {}

    def watch_board(self, bus) -> None:
        self._board_bus = bus

    def watch_coordinator(self, coord) -> None:
        self._coordinator = coord

    def watch_gitshare(self) -> None:
        self._gitshare = True

    def poll(self) -> str:
        notes: list[str] = []
        for aid, env in self._envs.items():
            if aid == self._self:
                continue
            try:
                out = env.execute("git status --porcelain 2>/dev/null | awk '{print $2}' | head -20").stdout
            except Exception:  # noqa: BLE001 - a dead teammate env must not kill this agent
                continue
            files = sorted(x for x in out.split() if x and not x.startswith(".cb_"))
            cur = ", ".join(files)
            if cur and cur != self._last.get(aid):
                self._last[aid] = cur
                notes.append(
                    f"[team] {aid} is currently editing: {cur}. Avoid colliding edits to these "
                    "files; if you must touch them, reuse that teammate's public names."
                )
        if self._gitshare:
            own_env = self._envs.get(self._self)
            for aid in self._envs:
                if aid == self._self or own_env is None:
                    continue
                try:
                    own_env.execute(f"git fetch -q shared {aid} 2>/dev/null || true", timeout=45)
                    files = own_env.execute(
                        f"git diff --name-only HEAD...shared/{aid} 2>/dev/null | grep -v '^.cb_' | head -8"
                    ).stdout.split()
                    cur = ", ".join(sorted(files))
                    if cur and cur != self._gitshare_last.get(aid):
                        self._gitshare_last[aid] = cur
                        notes.append(
                            f"[git] {aid}'s in-progress branch is fetched locally as shared/{aid} "
                            f"(changed files: {cur}). Inspect: `git diff HEAD...shared/{aid} -- <file>`. "
                            f"Take their version of a file: `git checkout shared/{aid} -- <file>`. Reuse "
                            "their public names instead of inventing parallel ones."
                        )
                except Exception:  # noqa: BLE001
                    pass
        if self._coordinator is not None:
            notes.extend(self._coordinator.drain(self._self))
        if self._board_bus is not None:
            try:
                rows = self._board_bus.list_tasks()
                cur = "; ".join(
                    f"{t.get('owner') or '?'}:{t.get('status','open')}:{t.get('title','')[:40]}"
                    for t in rows
                    if (t.get("owner") or "") != self._self
                )
                if cur and cur != self._board_last:
                    self._board_last = cur
                    notes.append(f"[board] teammate tasks: {cur}")
            except Exception:  # noqa: BLE001
                pass
        return "\n".join(notes)


def _tree_health_behavioral(env: Environment) -> bool:
    """Q10: BEHAVIORAL merge gate. The AST gate misses semantically-broken
    "clean" 3-way merges (q5g regression: 15.7 -> 13.3). Stages, cheap first:
    syntax/build, then the agents' own published checks (.cb_checks/*, present
    with preserve_invariants), then fail-fast repo tests. Any DEFINITE failure
    -> broken; missing tooling/timeouts read healthy (never repair blind)."""
    if not _tree_health(env):
        return False
    names = env.execute("ls .cb_checks/*.py 2>/dev/null").stdout.split()
    for n in names:
        r = env.execute(f"timeout 60 python3 {n} >/dev/null 2>&1; echo rc=$?")
        if r.stdout.strip().endswith("rc=1"):
            return False
    t = env.execute("python3 -m pytest -q -x --co -q >/dev/null 2>&1 && python3 -m pytest -q -x 2>&1 | tail -1", timeout=420)
    if "failed" in (t.stdout or "") or "error" in (t.stdout or "").lower():
        return False
    return True


def _gather_merge_evidence(env: Environment) -> str:
    """R2: collect concrete merge-damage evidence for the repair brief."""
    parts: list[str] = []
    rej = env.execute(
        "for f in $(find . -path ./.git -prune -o -name '*.rej' -print | head -6); do "
        "echo \"=== $f\"; head -40 $f; done"
    ).stdout.strip()
    if rej:
        parts.append("REJECTED HUNKS (apply these changes manually where they belong):\n" + rej[:3000])
    marks = env.execute("grep -rn '<<<<<<<' --include='*.py' --include='*.go' . 2>/dev/null | head -10").stdout.strip()
    if marks:
        parts.append("CONFLICT MARKERS at:\n" + marks[:1000])
    bad = env.execute(
        "python3 - <<'CB_EV_EOF'\n"
        "import ast, pathlib\n"
        "for p in pathlib.Path('.').rglob('*.py'):\n"
        "    s = str(p)\n"
        "    if '.git/' in s or s.startswith('.cb_'):\n"
        "        continue\n"
        "    try:\n"
        "        ast.parse(p.read_bytes(), filename=s)\n"
        "    except SyntaxError as e:\n"
        "        print(f'{s}:{e.lineno}: {e.msg}')\n"
        "    except Exception:\n"
        "        pass\n"
        "CB_EV_EOF"
    ).stdout.strip()
    if bad:
        parts.append("SYNTAX ERRORS:\n" + bad[:800])
    checks = env.execute(
        "for f in .cb_checks/*.py; do [ -f \"$f\" ] || continue; "
        "out=$(timeout 60 python3 $f 2>&1); rc=$?; "
        "if [ $rc -ne 0 ]; then echo \"=== $f FAILED (rc=$rc)\"; echo \"$out\" | tail -8; fi; done"
    ).stdout.strip()
    if checks:
        parts.append("FAILING ACCEPTANCE CHECKS:\n" + checks[:2000])
    return "\n\n".join(parts)[:7000]


_SCRATCHPAD = "/workspace/shared"


def _team_lead_block(members: list[Assignment]) -> str:
    """team_roles: lead-role prompt block (CooperBench team-mode analogue).

    The lead plans, assigns board tasks, implements its own feature, then
    MERGES member patches from the shared scratchpad — the lead's tree is
    what the team submits."""
    names = ", ".join(f"{m.agent_id} (feature {m.feature_id})" if m.feature_id is not None else m.agent_id for m in members)
    patch_list = ", ".join(f"{_SCRATCHPAD}/{m.agent_id}.patch" for m in members)
    return (
        f"\n\nROLE — TEAM LEAD. Teammates working RIGHT NOW in parallel copies of this repo: {names}. "
        f"A shared scratchpad directory {_SCRATCHPAD}/ is mounted in EVERY container; files there are "
        "not graded. The team is graded on YOUR container's final tree — it must contain EVERY feature.\n"
        f"1. FIRST: write a short plan to {_SCRATCHPAD}/PLAN.md dividing files/regions between the "
        "features, and post each teammate's task on the board (task_create). Then implement your own "
        "feature.\n"
        f"2. Each teammate exports finished work to its patch file ({patch_list}) and marks its board "
        "task done.\n"
        "3. Before you finish — MANDATORY integration: check `ls " + _SCRATCHPAD + "/*.patch`; apply each "
        "teammate patch with `git apply <patch>` (on failure try `git apply --3way <patch>`, then fix "
        "conflicts by hand until the code is consistent). If a patch has not appeared yet and you still "
        "have steps, keep checking between your own steps (`sleep 30` then `ls` is acceptable). "
        "Confirm with `git diff` that ALL features are present before finishing. Submitting only your "
        "own feature fails the whole team."
    )


def _team_member_block(a: Assignment, lead: Assignment) -> str:
    """team_roles: member-role prompt block (CooperBench team-mode analogue)."""
    lead_desc = f"{lead.agent_id} (feature {lead.feature_id})" if lead.feature_id is not None else lead.agent_id
    return (
        f"\n\nROLE — TEAM MEMBER. You are {a.agent_id}. The team lead {lead_desc} "
        "works in a parallel copy of this repo and merges the team result; "
        f"the team is graded on the LEAD's merged tree. A shared scratchpad directory {_SCRATCHPAD}/ is "
        "mounted in EVERY container; files there are not graded.\n"
        f"1. FIRST: read {_SCRATCHPAD}/PLAN.md if it exists and check the board (task_list) — they say "
        "which files/regions your feature owns. Claim your task (task_claim) and stay inside your "
        "regions.\n"
        "2. Implement YOUR feature.\n"
        f"3. When done — MANDATORY export: `git add -A && git diff --cached > {_SCRATCHPAD}/{a.agent_id}.patch`, "
        "then mark your board task done (task_update). Without this file your work cannot be merged and "
        "the team fails. Export a few steps BEFORE your step budget runs out."
    )


def _merge_repair_task(assignments: list[Assignment]) -> str:
    """Q5 integrator brief: the mechanical merge of parallel branches broke the
    tree; repair conflicts WITHOUT discarding either feature."""
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(f"### Feature {i + 1}\n{s}" for i, s in enumerate(specs))
    return (
        "You are the merge integrator. Teammates implemented the features below in PARALLEL "
        "copies of this repo and their diffs were just merged mechanically into THIS tree — "
        "the merge left it BROKEN (conflict markers like <<<<<<<, partially applied hunks, "
        "or syntax errors). Your job:\n"
        "1. Find the damage: search for conflict markers (`grep -rn '<<<<<<<' --include='*.py' .`), "
        "run a syntax check, look at `git diff` for incoherent hunks.\n"
        "2. Repair it so BOTH features work together — keep both implementations, reconciling "
        "names/regions where they collided. Do not delete a feature to make the tree build.\n"
        "3. Verify: syntax-check the files you touched and run any quick relevant tests.\n\n"
        "The features:\n\n" + bundle
    )


def _completeness_task(assignments: list[Assignment]) -> str:
    """T3 reviewer brief — verify EACH feature is fully implemented; fill gaps."""
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(f"### Feature {i + 1}\n{s}" for i, s in enumerate(specs))
    return (
        "You are a completeness reviewer. The team has implemented the features below in THIS repo "
        "(see `git diff`). Teams frequently implement one feature fully but OMIT or half-finish "
        "another. Go feature by feature:\n"
        "1. For EACH feature, confirm its required public API/behavior actually exists in the code "
        "(grep for the names/symbols the feature requires).\n"
        "2. If a feature is missing, partial, or only stubbed, IMPLEMENT it fully now.\n"
        "3. Ensure the project still builds. Do NOT create or edit test files.\n"
        "Submit only when every feature below is genuinely present and the code builds.\n\n"
        f"{bundle}"
    )


def _repair_task(assignments: list[Assignment]) -> str:
    """Integration/repair brief for the S5 verify-and-fix pass."""
    # Distinct feature specs (skip duplicates from shared-objective mode).
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(specs)
    return (
        "Your teammates have implemented the features below in THIS repository "
        "(see `git diff`). Your job is integration + repair, not new features:\n"
        "1. Make sure EVERY feature below is fully and correctly implemented.\n"
        "2. Make the project BUILD/COMPILE and its existing test suite pass — fix "
        "any compile errors, broken imports, or half-finished work you find.\n"
        "3. Resolve any inconsistencies between the features (naming, signatures).\n"
        "Do NOT create or edit test files. When it builds and is complete, submit.\n\n"
        f"## Features that must all work\n\n{bundle}"
    )


def repair_brief(env: Environment, spec: TeamSpec, assignments: list[Assignment]) -> str:
    task = _merge_repair_task(assignments)
    if spec.focused_repair:
        evidence = _gather_merge_evidence(env)
        if evidence:
            task += "\n\nEVIDENCE — the harness already located the damage; fix THESE directly instead of searching:\n\n" + evidence
    return task


def run_repair_tail(env: Environment, *, spec: TeamSpec, assignments: list[Assignment], seeds: dict[str, AgentResult],
                    run_agent: Callable, check_gate: Callable[[], bool], gate_checks: list[dict],
                    checkpoint_dir: Path | None = None,
        trace=None, first_task: str | None = None, checkpoint_bus: TeamBus | None = None) -> list[dict]:
    """Shared by full-team and checkpoint entrypoints; no initial health check here."""
    attempts = []
    for attempt in range(1, max(1, spec.repair_attempts) + 1):
        task = first_task if attempt == 1 and first_task is not None else repair_brief(env, spec, assignments)
        actor = f"integrator{attempt}"

        capture_seconds = 0.0

        def capture(agent, task=task, actor=actor, attempt=attempt):
            nonlocal capture_seconds
            capture_started = time.monotonic()
            from cooperagents.checkpoint import save_checkpoint
            from cooperagents.repair import capture_repair_input
            repair_input = capture_repair_input(
                agent, task=task, spec=spec, assignments=assignments, command_timeout=agent.env._timeout,
                guard_git=agent.env._guard_git, time_limit_s=spec.repair_time_limit,
                bus=checkpoint_bus,
            )
            save_checkpoint(env, checkpoint_dir / f"before-{actor}", metadata=dict(
                boundary="repair_agent_start", task=task, attempt=attempt, healthy=False, gate_checks=gate_checks,
            ), repair_input=repair_input, trace=partial(trace, actor) if trace else None)
            capture_seconds += time.monotonic() - capture_started

        started = time.monotonic()
        result = run_agent(actor, task, capture if checkpoint_dir is not None else None)
        seeds[actor] = result
        if first_task is not None and result.status == "error":
            from cooperagents.repair import RepairInfrastructureError

            raise RepairInfrastructureError(f"{actor} failed: {result.error}")
        cleanup = env.execute("find . -path ./.git -prune -o \\( -name '*.rej' -o -name '*.orig' \\) -exec rm -f {} +")
        if first_task is not None:
            from cooperagents.checkpoint import complete_output

            complete_output(cleanup, "Repair cleanup")
        healthy = check_gate()
        attempts.append(dict(actor=actor, healthy=healthy, duration_seconds=time.monotonic() - started - capture_seconds))
        if healthy:
            break
    return attempts


class UnifiedHarness:
    """Runs one team on one task, growing it on demand.

    ``coordinator_complete`` returns tool calls for one mini_swe
    coop-tools team. Its first call is synchronous on the harness caller's
    thread; later calls run serially on the monitor thread. The callback must
    support both threads and enforce its own request timeout (under 600s).
    The caller owns its client and SDK recording; worker environment variables
    are untouched. Invalid results or exceptions fail ``run`` after cleanup.
    Without a callback, failed model decisions are recorded and retried on a
    later tick. Notebook mode requires a run-specific ``notebook.md`` path;
    the environment factory must mount its parent read-only at /coordination.
    """

    def __init__(
        self,
        *,
        bus: TeamBus | None = None,
        step_limit: int = 40,
        cost_limit: float = 5.0,
        command_timeout: int = 60,
        quiet: bool = True,
        on_event: Callable[[str], None] | None = None,
        trajectory=None,
        langfuse: bool = False,
        coordinator_complete: Callable[[str], list[dict[str, str]]] | None = None,
        coordinator_notebook_path: Path | None = None,
        coordination_variant: str = "auto-prompt",
        coordination_mode: str = "direct",
        coordinator_implementation_enabled: bool = True,
        initial_plan: InitialPlan | None = None,
        planning_step_limit: int = 200,
        planning_time_limit_s: int = 600,
        worker_reminder_interval: int = 10,
        checkpoint_dir: Path | None = None,
        repair_completion: CompletionBinding | None = None,
    ) -> None:
        if coordinator_complete is not None and not callable(coordinator_complete):
            raise TypeError("coordinator_complete must be callable")
        self.repair_completion = repair_completion
        self.coordinator_complete = coordinator_complete
        if coordination_variant not in ("auto-prompt", "human-prompt"):
            raise ValueError(f"Unknown coordination variant: {coordination_variant}")
        if coordination_mode not in ("direct", "group-chat", "group-chat-planning"):
            raise ValueError(f"Unknown coordination mode: {coordination_mode}")
        if planning_step_limit < 1 or planning_time_limit_s < 1:
            raise ValueError("Planning budgets must be positive")
        if worker_reminder_interval < 0:
            raise ValueError("Worker reminder interval must be nonnegative")
        self.worker_reminder_interval = worker_reminder_interval
        self.planning_step_limit = planning_step_limit
        self.planning_time_limit_s = planning_time_limit_s
        self.coordination_mode = coordination_mode
        self.coordinator_implementation_enabled = coordinator_implementation_enabled
        self.initial_plan = initial_plan
        if initial_plan is not None and coordination_mode != "group-chat":
            raise ValueError("Initial plan injection requires group-chat")
        self.coordination_variant = coordination_variant
        self.coordinator_notebook_path = Path(coordinator_notebook_path).resolve() if coordinator_notebook_path is not None else None
        self.trajectory = trajectory
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir is not None else None
        self.langfuse = langfuse
        self.bus = bus
        self.step_limit = step_limit
        self.cost_limit = cost_limit
        self.command_timeout = command_timeout
        self.quiet = quiet
        self._on_event = on_event

    def _emit(self, msg: str) -> None:
        if self._on_event is not None:
            self._on_event(msg)

    def _build_assignments(self, spec: TeamSpec) -> list[Assignment]:
        """Resolve a spec into concrete seed assignments.

        ``assignments`` given → used verbatim.  Otherwise a single
        ``objective`` is fanned out to ``team_size`` agents (lead first).
        """
        if spec.assignments:
            return spec.assignments
        if spec.objective is None:
            raise ValueError("TeamSpec needs either assignments or an objective")
        seeds = []
        for i in range(max(1, spec.team_size)):
            seeds.append(
                Assignment(
                    agent_id=f"agent{i + 1}",
                    task=spec.objective,
                    role="lead" if i == 0 else "member",
                    feature_id=spec.features[i] if i < len(spec.features) else None,
                )
            )
        return seeds

    def _run_isolated(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
    ) -> RunResult:
        """Coordinated team where **every agent runs in its OWN container**
        (hard constraint — no shared live container).

        Agents run sequentially; each agent's fresh container is seeded with the
        cumulative diff of the teammates before it (applied via ``git apply``),
        so it builds on their committed work without sharing a live workspace.
        The last agent's cumulative diff is the integrated submission; an
        optional verify-fix integrator also runs in its own container.
        """

        def pick_llm(agent_id: str, role: str) -> LLMClient:
            if llm_factory is not None:
                return llm_factory(agent_id, role)
            if llm is None:
                raise ValueError("either llm or llm_factory must be provided")
            return llm

        def run_on_shared(
            env: Environment,
            agent_id: str,
            role: str,
            task: str,
            feature_id: int | None,
            step_limit: int | None = None,
            poller=None,
            time_limit_s: int | None = None,
            monitor=None,
            on_start=None,
        ) -> AgentResult:
            """Run one agent (mini-swe or builtin worker) on the shared tree."""
            if spec.spec_fidelity:  # S8: team injects spec-fidelity policy into the agent prompt
                task = _SPEC_FIDELITY + task
            if spec.tdd_preamble:  # T2: in-loop self-verification workflow
                task = _TDD_PREAMBLE + task
            if spec.mine_conventions:  # T4: in-loop convention-mining workflow
                task = _MINE_CONVENTIONS + task
            if spec.worker == "mini_swe":
                from cooperagents.workers.mini_swe_worker import BusComm, TaskBoard, run_mini_swe_agent

                return run_mini_swe_agent(
                    env,
                    task=task,
                    agent_id=agent_id,
                    role=role,
                    model_name=spec.model,
                    step_limit=step_limit or self.step_limit,
                    cost_limit=self.cost_limit,
                    feature_id=feature_id,
                    command_timeout=self.command_timeout,
                    guard_git=spec.guard_git,
                    temperature=spec.temperature,
                    comm=BusComm(bus, agent_id, group=monitor if monitor is not None and monitor.coordination_mode != "direct" else None)
                    if spec.coop_tools
                    else None,
                    poller=poller,
                    tool_protocol=spec.tool_protocol,
                    task_board=TaskBoard(bus, agent_id) if spec.task_board else None,
                    wait_protocol=spec.wait_protocol,
                    spawn_handler=make_spawn_handler(agent_id) if spec.allow_spawn_tool else None,
                    time_limit_s=time_limit_s,
                    monitor=monitor,
                    git_share=spec.git_share,
                    on_start=on_start,
                    completion=self.repair_completion if role == "integrator" else None,
                    completion_gate=spec.completion_gate,
                    trace=partial(self.trajectory.emit, agent_id) if self.trajectory else None,
                )
            agent = Agent(
                agent_id=agent_id,
                role=role,
                task=task,
                env=env,
                llm=pick_llm(agent_id, role),
                bus=bus,
                feature_id=feature_id,
                allow_spawn=False,
                step_limit=self.step_limit,
                cost_limit=self.cost_limit,
                command_timeout=self.command_timeout,
            )
            return agent.run()

        def seed_prior(env: Environment, patch: str) -> None:
            """Seed a fresh container with teammates' cumulative work via ``git apply``,
            then COMMIT it so the agent starts from a clean, coherent base (a dirty
            seeded tree confuses the agent and pollutes its own diff)."""
            if not patch.strip():
                return
            env.write_file(".cb_prior.patch", patch)
            env.execute(
                "git apply --whitespace=nowarn .cb_prior.patch 2>/dev/null "
                "|| git apply --3way .cb_prior.patch 2>/dev/null "
                "|| git apply --reject .cb_prior.patch 2>/dev/null || true"
            )
            env.execute(
                "rm -f .cb_prior.patch && git add -A && "
                "git -c user.email=team@cooperagents.local -c user.name=cooperagents commit -q -m 'teammate work' || true"
            )

        seeds: dict[str, AgentResult] = {}
        gate_discards: list[str] = []  # agents whose delta the do_no_harm gate rejected
        coordinator = None  # set in the coop branch when spec.coordinator
        start = time.time()
        integrated_patch = ""  # cumulative diff across the team so far (seed mode)
        raw_worker_patches: dict[str, str] = {}
        member_patches: list[str] = []  # each agent's own diff (no-seed mode)
        team_lead_patch: str | None = None  # team_roles: lead's merged tree is the submission
        prior: list[str] = []
        prior_fids: list[int] = []  # feature ids done so far (preserve_invariants)
        # HARD CONSTRAINT: every agent runs in its OWN container. With seed mode
        # (default) each fresh container is seeded by teammates' cumulative diff;
        # with no-seed each agent works independently and an integrator merges.
        assignments_all = list(assignments)
        if spec.coop_tools:
            # Q4 (qwen program): CooperBench-team-harness shape inside the unified
            # harness — agents run CONCURRENTLY from base (own containers), and
            # coordinate via the bus (`send_message` tool + inbox drained into
            # observations each step). Requires no-seed; the standard no-seed
            # integration tail below merges the member patches.
            from concurrent.futures import ThreadPoolExecutor

            roster = {a.agent_id: a for a in assignments}
            if spec.claim_mode:
                # TK6: seed one unclaimed board task per feature; agents divide
                # the work themselves via task_claim.
                for a in assignments:
                    if a.feature_id is not None:
                        bus.create_task(
                            title=f"Feature {a.feature_id} — see objective section 'Feature {a.feature_id}'",
                            created_by="harness",
                            owner="",
                        )
            spawn_lock = threading.Lock()
            spawn_count = [0]
            helper_patches: list[str] = []

            def make_spawn_handler(parent_id: str):
                def spawn(action: dict) -> dict:
                    cap = (spec.max_agents or len(assignments)) - len(assignments)
                    with spawn_lock:
                        if spawn_count[0] >= max(0, cap):
                            return {"output": f"spawn denied: helper cap ({cap}) reached", "returncode": 1, "exception_info": ""}
                        spawn_count[0] += 1
                        hid = f"helper{spawn_count[0]}"
                    brief = str(action.get("task", ""))[:4000]

                    def run_helper():
                        henv = env_factory(hid)
                        try:
                            hr = run_on_shared(henv, hid, "helper", brief, None)
                            seeds[hid] = hr
                            helper_patches.append(strip_test_sections(henv.git_diff()))
                        except Exception:  # noqa: BLE001 - a failed helper must not kill the parent
                            pass
                        finally:
                            henv.cleanup()

                    th = threading.Thread(target=run_helper, daemon=True)
                    th.start()
                    spawn_threads.append(th)
                    return {"output": f"{hid} spawned by {parent_id} on: {brief[:120]}", "returncode": 0, "exception_info": ""}

                return spawn

            spawn_threads: list[threading.Thread] = []
            # TK1/Q6: shared-interface contract, one planner call, pushed into
            # every brief. Empty (feature off) when offline or on any error.
            contract = _build_contract(assignments, spec.model) if spec.contract_first else ""
            # Envs are created up front (not per-thread) so the TK2 poller can
            # observe teammates' trees across containers.
            with ExitStack() as cleanup:
                coop_envs: dict[str, Environment] = {}
                coordinator = (
                    _Coordinator(
                        coop_envs,
                        spec.model,
                        assignments=assignments,
                        bus=bus,
                        notebook_path=self.coordinator_notebook_path if spec.coordinator_notebook else None,
                        coordination_variant=self.coordination_variant,
                        coordination_mode=self.coordination_mode,
                        coordinator_implementation_enabled=self.coordinator_implementation_enabled,
                        initial_plan=self.initial_plan,
                        planning_step_limit=self.planning_step_limit,
                        planning_time_limit_s=self.planning_time_limit_s,
                        worker_reminder_interval=self.worker_reminder_interval,
                        repo=spec.repo,
                        task_id=spec.task_id,
                        step_limit=self.step_limit,
                        time_limit_s=spec.agent_time_limit,
                        complete=self.coordinator_complete,
                        trace=partial(self.trajectory.emit, "coordinator") if self.trajectory else None,
                    )
                    if spec.coordinator
                    else None
                )
                for assignment in assignments:
                    env = env_factory(assignment.agent_id)
                    cleanup.callback(env.cleanup)
                    coop_envs[assignment.agent_id] = env
                gitsync = None
                if spec.git_share:
                    first = True
                    initial_heads, published = {}, set()
                    for _aid, _e in coop_envs.items():
                        if coordinator is not None:
                            try:
                                head = _e.execute("git rev-parse HEAD", timeout=5)
                                initial_heads[_aid] = head.stdout.strip() if head.exit_code == 0 else ""
                            except Exception:
                                initial_heads[_aid] = ""
                        if first:
                            _e.execute(f"git init -q --bare {_GITSHARE} 2>/dev/null || true")
                            first = False
                        _e.execute(f"git remote add shared {_GITSHARE} 2>/dev/null || true")
                        push = _e.execute(f"git push -q shared HEAD:refs/heads/{_aid} 2>/dev/null")
                        if push.exit_code == 0:
                            published.add(_aid)
                    gitsync = _GitShareSync(coop_envs, observe=coordinator is not None)
                    if coordinator is not None:
                        gitsync.initialize(initial_heads, published)
                        coordinator._git_sync = gitsync
                    gitsync_thread = threading.Thread(target=gitsync.run, daemon=True)
                    cleanup.callback(gitsync.finish, gitsync_thread)
                    gitsync_thread.start()
                coordinator_thread = None
                if coordinator is not None:
                    coordinator.verify_mounts()
                    if self.initial_plan is not None:
                        coordinator.inject_initial_plan()
                    else:
                        try:
                            coordinator.decide(initial=True)
                        except Exception as exc:
                            raise RuntimeError("Coordinator failed during initial decision") from exc
                    coordinator.start_planning()
                    coordinator_thread = threading.Thread(target=coordinator.run, daemon=True)
                    coordinator_thread.start()
                    cleanup.callback(coordinator.finish, coordinator_thread)


                def collect_raw_diff(env, aid: str) -> tuple[str, str]:
                    """Agent diff with share fallback: agents sometimes wipe their
                    working tree at the end (stash/checkout/rm to "verify a clean
                    patch"); the 45s share sync holds their last state, so recover
                    the diff from the pushed branch when the tree reads empty."""
                    raw = env.git_diff()
                    if strip_test_sections(raw).strip():
                        return raw, "working_tree"
                    base = getattr(env, "_base_commit", "") or "HEAD"
                    r = env.execute(
                        f"git fetch -q shared {aid} 2>/dev/null && "
                        f"git diff --binary {base} FETCH_HEAD -- . 2>/dev/null")
                    if r.stdout.strip():
                        return r.stdout, "shared_branch"
                    # last resort: the agent destroyed even its .git — read the
                    # share volume directly with a throwaway container
                    if hasattr(env, "recover_shared_diff"):
                        return env.recover_shared_diff(aid), "shared_branch_recovery"
                    vol = next((v.split(":")[0] for v in getattr(env, "volumes", None) or []
                                if v.endswith(":/cbshared")), None)
                    if vol and base != "HEAD":
                        import subprocess as _sp
                        rr = _sp.run(["docker", "run", "--rm", "-v", f"{vol}:/cb",
                                      "alpine/git", "--git-dir=/cb/repo.git",
                                      "diff", "--binary", base, aid],
                                     capture_output=True, text=True, timeout=120)
                        if rr.returncode == 0 and rr.stdout.strip():
                            return rr.stdout, "shared_volume_recovery"
                    return raw, "empty_working_tree"

                def collect_diff(env, aid: str, result: AgentResult) -> str:
                    if self.checkpoint_dir is None:
                        raw = collect_raw_diff(env, aid)[0]
                        if coordinator is not None and coordinator._planning:
                            raw_worker_patches[aid] = raw
                            result.patch = raw
                        return strip_test_sections(raw)
                    from cooperagents.checkpoint import save_checkpoint

                    # Block sync only during capture; teammates still receive later final pushes.
                    with gitsync._locks[aid] if gitsync is not None else nullcontext():

                        def capture_raw():
                            raw, source = collect_raw_diff(env, aid)
                            if coordinator is not None and coordinator._planning:
                                raw_worker_patches[aid] = raw
                                result.patch = raw
                            return raw, source

                        return save_checkpoint(
                            env,
                            self.checkpoint_dir / f"worker-{aid}",
                            metadata=dict(boundary="worker_end_before_collect_diff", actor=aid, result=asdict(result)),
                            collect_patch=capture_raw,
                            trace=partial(self.trajectory.emit, aid) if self.trajectory else None,
                        )

                def run_coop(a: Assignment) -> tuple[str, AgentResult, str]:
                    env = coop_envs[a.agent_id]
                    if spec.team_roles:
                        # Complete-Team cell: role block replaces the generic
                        # coordination paragraphs (mirrors CooperBench team mode,
                        # where the team block substitutes for the coop block).
                        lead = assignments[0]
                        if a.agent_id == lead.agent_id:
                            others = [x for x in assignments if x.agent_id != a.agent_id]
                            task = a.task + _team_lead_block(others)
                        else:
                            task = a.task + _team_member_block(a, lead)
                        poller = _TeammatePoller(a.agent_id, coop_envs) if spec.task_board else None
                        if poller is not None:
                            poller.watch_board(bus)
                        r = run_on_shared(env, a.agent_id, a.role, task, a.feature_id, poller=poller, time_limit_s=spec.agent_time_limit)
                        return a.agent_id, r, collect_diff(env, a.agent_id, r)
                    if True:
                        mates = ", ".join(
                            f"{x.agent_id} (feature {x.feature_id})" for x in assignments if x.agent_id != a.agent_id
                        )
                        task = a.task + (
                            f"\n\nTEAMMATES: {mates} are implementing their features RIGHT NOW in parallel "
                            "copies of this repo; your diffs will be merged at the end. Coordinate via the "
                            "send_message tool (incoming messages appear as [Message from ...]): agree on "
                            "shared names/signatures early and avoid editing the same regions."
                        )
                        if spec.preserve_invariants and a.feature_id is not None:
                            # Q8: the published check is used ONLY for best-of-N candidate
                            # selection (cross-attempt agreement probes); it is stripped
                            # before grading and never ships.
                            task += (
                                f"\n\nPUBLISH YOUR ACCEPTANCE CHECK — before finishing, create "
                                f"`.cb_checks/f{a.feature_id}.py`: a minimal STANDALONE python script (no "
                                "pytest) that exercises YOUR feature's public behavior exactly as the spec "
                                "describes and exits non-zero if it is broken or absent. Run it to confirm "
                                "it passes. It must terminate in seconds — no loops that can hang."
                            )
                        if spec.claim_mode:
                            task += (
                                "\n\nWORK ALLOCATION — the objective above contains MULTIPLE features and the "
                                "board has one UNCLAIMED task per feature. FIRST action: task_list, then "
                                "task_claim one task. Implement ONLY features you claimed. When done, mark it "
                                "done and claim more unclaimed work if any remains. If a claim fails, someone "
                                "else owns it — pick another."
                            )
                        if spec.task_board:
                            task += (
                                "\n\nTASK BOARD PROTOCOL — before coding, post 2-4 short tasks describing "
                                "your plan (task_create), mark each 'doing' when you start and 'done' when "
                                "finished (task_update). Check the whole board (task_list) before editing "
                                "files a teammate's tasks mention. Keep titles short; spend steps on code."
                            )
                        if spec.wait_protocol:
                            task += (
                                "\n\nIf you need an agreed public name/signature from a teammate BEFORE you "
                                "can proceed, use send_message with wait:true — the reply comes back in the "
                                "same tool output."
                            )
                        if spec.tool_protocol and coordinator is None:
                            first_mate = next((x.agent_id for x in assignments if x.agent_id != a.agent_id), "your teammate")
                            task += (
                                f"\n\nCOORDINATION PROTOCOL — your FIRST action must be a send_message to "
                                f"{first_mate} stating the public names, signatures, and files you plan to "
                                "create for your feature. Before editing any file you suspect your teammate "
                                "also touches, check your observations for [Message from ...] notes and "
                                "reconcile names with what they declared. Keep messages short; spend your "
                                "steps on code."
                            )
                        if contract:
                            task += (
                                "\n\nSHARED INTERFACE CONTRACT — the team agreed on this up front; "
                                "follow it EXACTLY (names, signatures, file locations). Deviating breaks "
                                "the merge with your teammates:\n" + contract
                            )
                        poller = (
                            _TeammatePoller(a.agent_id, coop_envs)
                            if (spec.live_awareness or spec.task_board or spec.coordinator or spec.git_share)
                            else None
                        )
                        if poller is not None and spec.git_share:
                            poller.watch_gitshare()
                        if poller is not None and spec.task_board:
                            poller.watch_board(bus)
                        if poller is not None and coordinator is not None:
                            poller.watch_coordinator(coordinator)
                        r = run_on_shared(env, a.agent_id, a.role, task, a.feature_id,
                                          poller=poller, monitor=coordinator, time_limit_s=spec.agent_time_limit)

                        return a.agent_id, r, collect_diff(env, a.agent_id, r)
                    return None  # unreachable

                with ThreadPoolExecutor(max_workers=len(roster)) as ex:
                    for aid, r, diff in ex.map(run_coop, assignments):
                        seeds[aid] = r
                        member_patches.append(diff)
                        if spec.team_roles and aid == assignments[0].agent_id:
                            team_lead_patch = diff
                        prior.append(f"feature {roster[aid].feature_id}" if roster[aid].feature_id is not None else roster[aid].role)
            for th in spawn_threads:
                th.join(timeout=1200)
            member_patches.extend(p for p in helper_patches if p.strip())
            assignments = []  # sequential loop below is skipped
        for a in assignments:
            env = env_factory(a.agent_id)
            try:
                if spec.seed_prior:
                    seed_prior(env, integrated_patch)
                task = a.task
                if prior and spec.teammate_context:
                    if spec.seed_prior:
                        task += (
                            f"\n\nTeammates before you already implemented {', '.join(prior)} — their code is "
                            "ALREADY in this repository (run `git diff` to see it). Build on it, reuse their "
                            "public names/signatures, and do not duplicate or revert it."
                        )
                    elif integrated_patch.strip():
                        drafts = integrated_patch[:6000]
                        task += (
                            f"\n\nTeammates are implementing {', '.join(prior)} in parallel; their drafts:\n"
                            f"```diff\n{drafts}\n```\nReuse their public names/signatures."
                        )
                if spec.preserve_invariants:
                    # Coordination-under-interdependence: each agent publishes a runnable
                    # regression check for its OWN feature into the shared tree (.cb_checks/,
                    # stripped before grading); later agents MUST keep all prior checks green —
                    # directly targeting the dominant coupled failure (a later agent silently
                    # breaking an earlier teammate's feature while editing shared code).
                    if prior_fids:
                        checks = " ".join(f".cb_checks/f{f}.py" for f in prior_fids)
                        task += (
                            f"\n\nTEAMMATE INVARIANTS — features {', '.join(str(f) for f in prior_fids)} are already "
                            f"implemented and WORKING, each verified by a check script: {checks}. FIRST run "
                            f"`python {checks}` to see them pass. As you add your feature you MUST keep ALL of them "
                            "passing — do not change or break a teammate's behavior. Re-run them before finishing; "
                            "if you broke one, fix your code (not the check) until it passes again."
                        )
                    if a.feature_id is not None:
                        task += (
                            f"\n\nPUBLISH YOUR INVARIANT — before finishing, create `.cb_checks/f{a.feature_id}.py`: a "
                            "minimal STANDALONE python script (no pytest) that exercises YOUR feature's public "
                            "behavior and raises/exits non-zero if it regresses. Run it to confirm it passes. This is "
                            "your contract to teammates who build on your work; keep it small and fast."
                        )
                pre_healthy = _tree_health(env) if spec.do_no_harm else True
                seeds[a.agent_id] = run_on_shared(env, a.agent_id, a.role, task, a.feature_id,
                                                  time_limit_s=spec.agent_time_limit)
                diff = strip_test_sections(env.git_diff())
                if spec.do_no_harm and pre_healthy and not _tree_health(env):
                    # Q1 do-no-harm gate: the agent broke a previously-healthy
                    # tree — discard its delta so the team keeps the last
                    # healthy state instead of shipping corrupted code.
                    gate_discards.append(a.agent_id)
                    diff = integrated_patch if spec.seed_prior else ""
            finally:
                env.cleanup()
            member_patches.append(diff)
            if spec.seed_prior:
                integrated_patch = diff  # cumulative
            else:
                integrated_patch = "\n".join(member_patches)  # text-only context for next agent
            prior.append(f"feature {a.feature_id}" if a.feature_id is not None else a.role)
            if a.feature_id is not None:
                prior_fids.append(a.feature_id)

        if coordinator is not None and coordinator.phase == "failed":
            # Preserve every worker's actual patch and bypass all generating tails.
            return RunResult(
                run_id=spec.run_id,
                repo=spec.repo,
                task_id=spec.task_id,
                features=sorted(spec.features),
                seeds=seeds,
                duration_seconds=time.time() - start,
                integrated=AgentResult(agent_id="team", role="team", status="planning_failed", patch="\n".join(member_patches)),
                metrics={**coordinator.planning_metrics(), "planning_failed": True, "raw_worker_patches": raw_worker_patches},
            )

        # Integration. Seed mode already has the cumulative diff; no-seed must merge
        # the independent member patches in a fresh integrator container.
        if not spec.seed_prior:
            integrated_patch = ""  # rebuild from member patches below
        if spec.team_roles and team_lead_patch is not None:
            # Complete-Team cell: the lead already integrated the member's
            # scratchpad patch in its own container; the lead's tree is the
            # team submission (matches CooperBench team-mode scoring).
            integrated_patch = team_lead_patch
        elif spec.verify_fix and len(assignments) > 1:
            env = env_factory("integrator")
            try:
                if spec.seed_prior:
                    seed_prior(env, integrated_patch)
                else:
                    for p in member_patches:
                        seed_prior(env, p)
                seeds["integrator"] = run_on_shared(env, "integrator", "integrator", _repair_task(assignments), None)
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
        elif not spec.seed_prior and len(member_patches) > 1 and spec.select_integration is not None:
            # Iteration 7 completion (pre-submission merge arms): each agent's
            # final tree ALREADY contains the merged team work and passed the
            # completion gate; 3-way merging two both-merged trees re-creates
            # the damage the gate just prevented (observed: tuijournal/fx i7
            # regressions to 0 with zero gate rejections). Select the best
            # tree mechanically instead of re-merging.
            idx = spec.select_integration(member_patches)
            integrated_patch = member_patches[idx]
            print(f"[harness] integration=selected chosen={idx} "
                  f"sizes={[len(p) for p in member_patches]}")
        elif not spec.seed_prior and len(member_patches) > 1:
            # No-seed without an LLM integrator: mechanically merge the independent
            # member patches in a fresh container. A real 3-way merge goes first —
            # it auto-resolves non-overlapping edits to the same file, so clean
            # merges skip the apply-chain's .rej fallout (and usually the repair
            # pass). Only on genuine region conflicts fall back to the apply chain
            # (partial applies + .rej) for the repair agent to reconcile.
            env = env_factory("merge")
            try:
                merge_base = (env.execute("git rev-parse HEAD").stdout or "").strip()
                if spec.apply_chain_merge:
                    conflict = True  # TK8: force the apply-chain path (repairable damage)
                else:
                    conflict, _merged = _threeway_merge(env, member_patches)
                if conflict:
                    env.execute(f"git checkout -q -B _fb {merge_base}" if merge_base else "true")
                    for p in member_patches:
                        seed_prior(env, p)
                # Merge hygiene: apply-fallback artifacts (.rej/.orig) are not part
                # of any feature — without this they ship inside the integrated
                # diff (observed on qwen14-q4: 7/14 pairs polluted).
                env.execute("find . -path ./.git -prune -o \\( -name '*.rej' -o -name '*.orig' \\) -print0 2>/dev/null | xargs -0 -r rm -f")
                _gate = _tree_health_behavioral if spec.behavioral_gate else _tree_health
                gate_checks = []

                def check_gate():
                    from cooperagents.trajectory import record_call

                    if self.checkpoint_dir is None and self.trajectory is None:
                        return _gate(env)
                    trace = partial(self.trajectory.emit, "merge_gate") if self.trajectory else None
                    class GateEnv:
                        def execute(_self, command, *, timeout=60):
                            result = record_call(trace, env.execute, command=command, timeout=timeout)
                            gate_checks.append(dict(command=command, timeout=timeout, **asdict(result)))
                            return result
                    return _gate(GateEnv())

                healthy = check_gate() if spec.repair_integrator else None
                if self.checkpoint_dir is not None:
                    from cooperagents.checkpoint import save_checkpoint

                    save_checkpoint(env, self.checkpoint_dir / "pre-repair", metadata=dict(
                        boundary="merged_before_repair", conflict=conflict, apply_chain=spec.apply_chain_merge,
                        repair_enabled=spec.repair_integrator, healthy=healthy, gate_checks=gate_checks,
                        gate="behavioral" if spec.behavioral_gate else "syntax_build",
                        task=_merge_repair_task(assignments_all),
                    ), trace=partial(self.trajectory.emit, "merge") if self.trajectory else None)
                repair_started = time.monotonic()
                attempts = []
                if spec.repair_integrator and not healthy:
                    def run_repair_agent(actor, task, on_start):
                        return run_on_shared(
                            env, actor, "integrator", task, None,
                            step_limit=spec.repair_step_limit, time_limit_s=spec.repair_time_limit,
                            on_start=on_start,
                        )

                    attempts = run_repair_tail(
                        env, spec=spec, assignments=assignments_all, seeds=seeds,
                        run_agent=run_repair_agent, check_gate=check_gate, gate_checks=gate_checks,
                        checkpoint_dir=self.checkpoint_dir,
                        checkpoint_bus=bus,
                        trace=self.trajectory.emit if self.trajectory else None,
                    )
                    healthy = attempts[-1]["healthy"]
                raw_integrated_patch = env.git_diff()
                integrated_patch = strip_test_sections(raw_integrated_patch)
                if self.checkpoint_dir is not None:
                    # Capture after final collect_diff so the saved patch is the actual submission.
                    save_checkpoint(env, self.checkpoint_dir / "post-repair", metadata=dict(
                        boundary="integrated_submission", healthy=healthy, gate_checks=gate_checks, attempts=attempts,
                        repair_duration_seconds=sum(a["duration_seconds"] for a in attempts),
                        repair_wall_seconds_including_checkpoints=time.monotonic() - repair_started if attempts else 0,
                        results={aid: asdict(r) for aid, r in seeds.items() if aid.startswith("integrator")},
                    ), collect_patch=lambda: (raw_integrated_patch, "integrated_submission"),
                        trace=partial(self.trajectory.emit, "merge") if self.trajectory else None)
            finally:
                env.cleanup()
        elif not spec.seed_prior:
            integrated_patch = member_patches[0] if member_patches else ""

        # T3: completeness review — own container, seeded with the full diff,
        # enumerates each feature and fills gaps (the dominant failure mode).
        if spec.completeness_review and len(assignments) > 1:
            env = env_factory("reviewer")
            try:
                seed_prior(env, integrated_patch)
                seeds["reviewer"] = run_on_shared(env, "reviewer", "reviewer", _completeness_task(assignments), None)
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()

        integrated = AgentResult(
            agent_id="team",
            role="integrated",
            status="submitted" if integrated_patch.strip() else "error",
            patch=integrated_patch,
            cost=sum(r.cost for r in seeds.values()),
            steps=sum(r.steps for r in seeds.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=seeds,
            integrated=integrated,
            duration_seconds=time.time() - start,
            metrics={
                **coordination_metrics(bus.task_events(), final_tasks=bus.list_tasks()),
                **({"do_no_harm_discards": gate_discards} if spec.do_no_harm else {}),
                **(
                    {"coordinator_events": coordinator.events(), **coordinator.planning_metrics()}
                    if spec.coordinator and coordinator is not None
                    else {}
                ),
            },
        )

    def _run_worker(
        self,
        spec: TeamSpec,
        env: Environment,
        *,
        agent_id: str,
        role: str,
        task: str,
        feature_id: int | None,
        bus: TeamBus,
        llm: LLMClient | None,
    ) -> AgentResult:
        """Run one agent (mini-swe or builtin) in its own container, applying the
        team-level prompt seams (spec-fidelity / TDD / convention-mining)."""
        if spec.spec_fidelity:
            task = _SPEC_FIDELITY + task
        if spec.tdd_preamble:
            task = _TDD_PREAMBLE + task
        if spec.mine_conventions:
            task = _MINE_CONVENTIONS + task
        if spec.worker == "mini_swe":
            from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

            return run_mini_swe_agent(
                env,
                task=task,
                agent_id=agent_id,
                role=role,
                model_name=spec.model,
                step_limit=self.step_limit,
                cost_limit=self.cost_limit,
                feature_id=feature_id,
                command_timeout=self.command_timeout,
                guard_git=spec.guard_git,
                temperature=spec.temperature,
                completion=self.repair_completion if role == "integrator" else None,
            )
        if llm is None:
            raise ValueError("builtin worker requires an LLM client")
        agent = Agent(
            agent_id=agent_id,
            role=role,
            task=task,
            env=env,
            llm=llm,
            bus=bus,
            feature_id=feature_id,
            allow_spawn=False,
            step_limit=self.step_limit,
            cost_limit=self.cost_limit,
            command_timeout=self.command_timeout,
        )
        return agent.run()

    def _run_decomposed(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
        planner: Planner | None,
    ) -> RunResult:
        """G1+G2+G3: plan an independence-maximizing subtask DAG, run it with
        independent subtasks in PARALLEL (own containers, seeded only along DAG
        edges), then merge the branch deltas.

        Each subtask is one agent in its own container (hard constraint). A
        subtask is seeded with the deltas of its transitive ancestors only — not
        the whole shared state — so two independent branches never see each
        other's edits, eliminating the interference that coupled sequential
        seeding causes (Round 6). Whether the merge is clean is exactly the test
        of decomposition quality.
        """
        start = time.time()
        specs: list[tuple[int, str]] = [
            (a.feature_id if a.feature_id is not None else i + 1, a.task) for i, a in enumerate(assignments)
        ]
        cap = spec.max_agents if spec.max_agents is not None else len(assignments)
        max_sub = max(1, min(cap, len(assignments) + 1))
        if planner is not None:
            subs, rationale = planner(specs, spec.objective)
        else:
            subs, rationale = plan_decomposition(specs, objective=spec.objective, max_subtasks=max_sub, model=spec.model)
        by_id = {s.id: s for s in subs}
        levels = topo_levels(subs)
        topo_order = [s.id for level in levels for s in level]

        def pick_llm(agent_id: str, role: str) -> LLMClient | None:
            return llm_factory(agent_id, role) if llm_factory is not None else llm

        deltas: dict[str, str] = {}
        results: dict[str, AgentResult] = {}
        lock = threading.Lock()

        def ownership_preamble(s: SubTask) -> str:
            """Hard write-set boundary: the key to conflict-free re-division —
            this agent edits ONLY its regions; teammates' regions are off-limits
            so two subtasks on the same file (disjoint regions) merge cleanly."""
            if not s.owns:
                return ""
            others = sorted({o for t in subs if t.id != s.id for o in t.owns})
            msg = (
                "OWNERSHIP — you may edit ONLY these regions (your write-set):\n  - "
                + "\n  - ".join(s.owns)
                + "\nDo NOT edit anything outside them."
            )
            if others:
                msg += (
                    " Teammates own these regions in parallel — do NOT touch them; if you need their "
                    "code, assume the public interface described in the spec:\n  - " + "\n  - ".join(others)
                )
            return msg + "\n\n"

        def publish_preamble(s: SubTask) -> str:
            # Guarded-merge (loss-free parallelism): each parallel branch publishes a
            # runnable check for its feature so the integrator can detect & repair any
            # feature the merge breaks (Round 9: parallel split+merge loses 7/28 features).
            if not (spec.preserve_invariants and s.features):
                return ""
            return (
                "PUBLISH YOUR INVARIANT — before finishing, create `.cb_checks/f"
                f"{s.features[0]}.py`: a minimal STANDALONE python script (no pytest) that exercises YOUR "
                "feature's public behavior and exits non-zero if it regresses. Run it to confirm it passes. "
                "The integrator runs it after merging all branches to ensure the merge didn't break you.\n\n"
            )

        def run_sub(s: SubTask) -> None:
            env = env_factory(s.id)
            try:
                anc = ancestors(s, by_id)
                for aid in [x for x in topo_order if x in anc]:
                    with lock:
                        seed = deltas.get(aid, "")
                    _seed_patch(env, seed)
                res = self._run_worker(
                    spec,
                    env,
                    agent_id=s.id,
                    role="member",
                    task=publish_preamble(s) + ownership_preamble(s) + s.task,
                    feature_id=s.features[0] if s.features else None,
                    bus=bus,
                    llm=pick_llm(s.id, "member"),
                )
                delta = strip_test_sections(env.git_diff())
            except Exception as e:  # noqa: BLE001 - one subtask must not kill the run
                res = AgentResult(agent_id=s.id, role="member", status="error", error=str(e))
                delta = ""
            finally:
                env.cleanup()
            with lock:
                deltas[s.id] = delta
                results[s.id] = res

        # Run level by level; subtasks within a level are independent → parallel.
        for level in levels:
            threads = [threading.Thread(target=run_sub, args=(s,)) for s in level]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Integrate: a fresh container, apply every delta in topo order. Clean if
        # the decomposition was well-separated; conflicts (.rej) if it wasn't.
        env = env_factory("integrator")
        guarded = spec.preserve_invariants and len(subs) > 1
        try:
            for sid in topo_order:
                _seed_patch(env, deltas.get(sid, ""))
            if guarded:
                # Loss-free parallelism: the merge may have broken a feature that worked
                # in its own branch. The integrator runs every published check and repairs
                # the merge until all pass — turning a lossy split+merge into a guarded one.
                feats = sorted({f for s in subs for f in s.features})
                checks = " ".join(f".cb_checks/f{f}.py" for f in feats)
                repair = (
                    "You are the integrator. Branches were developed in parallel and merged into this repo "
                    f"(some merges may have left conflicts or broken a feature). Run `python {checks}` — these "
                    "are per-feature checks each branch published. Any that FAIL means the merge broke that "
                    "feature: fix the integration (resolve conflict markers / .rej, reconcile shared code) until "
                    "EVERY check passes. Do NOT edit the check files or delete features. Submit when all pass."
                )
                results["integrator"] = self._run_worker(
                    spec,
                    env,
                    agent_id="integrator",
                    role="integrator",
                    task=repair,
                    feature_id=None,
                    bus=bus,
                    llm=pick_llm("integrator", "integrator"),
                )
            integrated_patch = strip_test_sections(env.git_diff())
        finally:
            env.cleanup()

        integrated = AgentResult(
            agent_id="team",
            role="integrated",
            status="submitted" if integrated_patch.strip() else "error",
            patch=integrated_patch,
            cost=sum(r.cost for r in results.values()),
            steps=sum(r.steps for r in results.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=results,
            integrated=integrated,
            duration_seconds=time.time() - start,
            metrics={
                "decompose": True,
                "n_subtasks": len(subs),
                "n_edges": sum(len(s.depends_on) for s in subs),
                "levels": [[s.id for s in level] for level in levels],
                "max_parallel": max((len(level) for level in levels), default=0),
                "guarded_merge": guarded,
                "rationale": rationale,
            },
        )

    def _run_adaptive(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
    ) -> RunResult:
        """Let the work decide sequential vs parallel (runtime topology selection).

        Phase 1: run every feature in PARALLEL from base (own containers), each
        publishing a runnable invariant check. Phase 2: probe the merge — if the
        branches apply cleanly onto each other and the checks stay green, KEEP the
        parallel result. Phase 3: on a conflict (the work was coupled), FALL BACK
        to the sequential build-on-prior handoff for the remaining features,
        reusing the first branch. The conflict is the decision — no ex-ante guess.
        """
        start = time.time()

        def pick_llm(agent_id: str, role: str) -> LLMClient | None:
            return llm_factory(agent_id, role) if llm_factory is not None else llm

        def publish_pre(a: Assignment) -> str:
            if a.feature_id is None:
                return ""
            return (
                f"PUBLISH YOUR INVARIANT — before finishing, create `.cb_checks/f{a.feature_id}.py`: a minimal "
                "STANDALONE python script (no pytest) that exercises YOUR feature's public behavior and exits "
                "non-zero if it regresses. Run it to confirm it passes.\n\n"
            )

        # Phase 1 — parallel from base (own container each), publish checks.
        deltas: dict[str, str] = {}
        par_results: dict[str, AgentResult] = {}
        lock = threading.Lock()

        def run_par(a: Assignment) -> None:
            env = env_factory(a.agent_id)
            try:
                res = self._run_worker(
                    spec, env, agent_id=a.agent_id, role=a.role, task=publish_pre(a) + a.task,
                    feature_id=a.feature_id, bus=bus, llm=pick_llm(a.agent_id, a.role),
                )
                delta = strip_test_sections(env.git_diff())
            except Exception as e:  # noqa: BLE001 - one branch must not kill the run
                res = AgentResult(agent_id=a.agent_id, role=a.role, status="error", feature_id=a.feature_id, error=str(e))
                delta = ""
            finally:
                env.cleanup()
            with lock:
                deltas[a.agent_id] = delta
                par_results[a.agent_id] = res

        threads = [threading.Thread(target=run_par, args=(a,)) for a in assignments]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Phase 2 — merge probe: do the parallel branches compose under a real
        # 3-way merge? (base-aware; only genuine region overlaps conflict.)
        order = [a.agent_id for a in assignments]
        conflict = False
        integrated_patch = ""
        env = env_factory("merge-probe")
        try:
            conflict, integrated_patch = _threeway_merge(env, [deltas.get(aid, "") for aid in order])
            if not conflict and integrated_patch.strip():
                # secondary signal: a feature silently broke even though it merged
                probe = env.execute(
                    'ok=1; for f in .cb_checks/*.py; do [ -e "$f" ] || continue; python "$f" >/dev/null 2>&1 || ok=0; done; [ "$ok" = 1 ]'
                )
                if probe.exit_code != 0:
                    conflict = True
        finally:
            env.cleanup()

        if not conflict and integrated_patch.strip():
            integrated = AgentResult(
                agent_id="team", role="integrated", status="submitted", patch=integrated_patch,
                cost=sum(r.cost for r in par_results.values()), steps=sum(r.steps for r in par_results.values()),
                feature_id=sorted(spec.features)[0] if spec.features else None,
            )
            return RunResult(
                run_id=spec.run_id, repo=spec.repo, task_id=spec.task_id, features=sorted(spec.features),
                seeds=par_results, integrated=integrated, duration_seconds=time.time() - start,
                metrics={"adaptive": True, "topology": "parallel", "conflict": False},
            )

        # Phase 3 — coupled: fall back to sequential build-on-prior, reusing branch 1.
        seq_results: dict[str, AgentResult] = {order[0]: par_results.get(order[0], AgentResult(order[0], assignments[0].role, "error"))}
        cumulative = deltas.get(order[0], "")
        for a in assignments[1:]:
            env = env_factory(f"{a.agent_id}-seq")
            try:
                _seed_patch(env, cumulative)
                prior = [f"feature {p.feature_id}" for p in assignments[: assignments.index(a)] if p.feature_id is not None]
                task = (
                    publish_pre(a) + a.task
                    + f"\n\nTeammates already implemented {', '.join(prior)} — their working code is ALREADY in "
                    "this repo (run `git diff`). Build on it and do NOT break it."
                )
                res = self._run_worker(
                    spec, env, agent_id=a.agent_id, role=a.role, task=task,
                    feature_id=a.feature_id, bus=bus, llm=pick_llm(a.agent_id, a.role),
                )
                cumulative = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
            seq_results[a.agent_id] = res

        integrated = AgentResult(
            agent_id="team", role="integrated", status="submitted" if cumulative.strip() else "error", patch=cumulative,
            cost=sum(r.cost for r in seq_results.values()), steps=sum(r.steps for r in seq_results.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id, repo=spec.repo, task_id=spec.task_id, features=sorted(spec.features),
            seeds=seq_results, integrated=integrated, duration_seconds=time.time() - start,
            metrics={"adaptive": True, "topology": "sequential-fallback", "conflict": True},
        )

    def _run_best_of_n(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
        selector: Callable[[list[RunResult]], int] | None,
    ) -> RunResult:
        """T6: run the isolated team ``best_of_n`` times (each its own containers),
        then a self-available selector picks the candidate to submit.

        Run-to-run variance is the headroom; the selector must use only
        self-available signal (LLM judge / build probe), never the hidden grader.
        If no selector is given, fall back to the candidate whose integrated diff
        touches the most files (a weak coverage heuristic, offline-safe).
        """
        def run_attempt(i: int) -> RunResult:
            cand_bus = InMemoryBus(f"{spec.run_id}-c{i}")
            self._emit(f"best-of-{spec.best_of_n}: attempt {i + 1}")
            # Q3: attempt 1 stays at the pinned temperature (reproducible floor);
            # later attempts sample hotter so selection has genuinely different
            # candidates (at temp 0 both attempts usually converge — Q2 finding).
            spec_i = spec
            if i > 0 and spec.diversity_temperature is not None:
                from dataclasses import replace

                spec_i = replace(spec, temperature=spec.diversity_temperature)
            return self._run_isolated(spec_i, assignments, cand_bus, env_factory, llm, llm_factory)

        if llm is None and llm_factory is None:
            # Live worker path: attempts are independent whole-team runs — run them
            # CONCURRENTLY so best-of-N costs ~1 attempt of wall-clock, not N.
            # (Scripted/demo policies stay sequential: a shared ScriptedLLM queue
            # is not safe to consume from two attempts at once.)
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=spec.best_of_n) as ex:
                candidates = list(ex.map(run_attempt, range(spec.best_of_n)))
        else:
            candidates = [run_attempt(i) for i in range(spec.best_of_n)]

        def coverage(r: RunResult) -> int:
            patch = r.integrated.patch if r.integrated else ""
            return sum(1 for line in patch.splitlines() if line.startswith("+++ "))

        if selector is not None:
            try:
                chosen = selector(candidates)
            except Exception as e:  # noqa: BLE001 - a flaky selector must not kill the run
                self._emit(f"selector failed ({e}); falling back to coverage heuristic")
                chosen = max(range(len(candidates)), key=lambda j: coverage(candidates[j]))
        else:
            chosen = max(range(len(candidates)), key=lambda j: coverage(candidates[j]))

        result = candidates[chosen]
        result.metrics = {
            **result.metrics,
            "best_of_n": spec.best_of_n,
            "chosen_index": chosen,
            "candidate_coverage": [coverage(c) for c in candidates],
            "candidate_patch_lines": [c.integrated.patch_lines if c.integrated else 0 for c in candidates],
        }
        result.duration_seconds = sum(c.duration_seconds for c in candidates)
        return result

    def run(
        self,
        spec: TeamSpec,
        *,
        env_factory: EnvFactory,
        llm: LLMClient | None = None,
        llm_factory: LLMFactory | None = None,
        selector: Callable[[list[RunResult]], int] | None = None,
        planner: Planner | None = None,
    ) -> RunResult:
        if self.checkpoint_dir is not None:
            from cooperagents.repair import validate_capture_spec

            validate_capture_spec(spec, self.bus)
            ids = [a.agent_id for a in self._build_assignments(spec)]
            if len(ids) < 2 or len(set(ids)) != len(ids) or any(
                Path(aid).name != aid or aid in {".", "..", ""} for aid in ids
            ):
                raise ValueError("Repair checkpoints require two workers with unique safe IDs")
        if self.langfuse:
            if (
                spec.worker != "mini_swe" or not spec.shared_workspace or not spec.coop_tools
                or spec.best_of_n != 1 or spec.decompose or spec.adaptive
            ):
                raise ValueError("Langfuse tracing requires a single mini_swe coop-tools team")
            try:
                from cooperagents.observability import LangfuseTrace
            except ImportError as exc:
                raise RuntimeError("Install cooperagents[langfuse] to enable Langfuse tracing") from exc
            sink = LangfuseTrace(spec, self.trajectory)
            traced = copy.copy(self)
            traced.langfuse, traced.trajectory = False, sink
            failed = True
            try:
                result = traced.run(spec, env_factory=env_factory, llm=llm, llm_factory=llm_factory, selector=selector, planner=planner)
                failed = False
                return result
            finally:
                sink.close(failed=failed)
        if not self.coordinator_implementation_enabled and not spec.coordinator:
            raise ValueError("Disabling implementation decisions requires coordinator enabled")
        if self.coordination_variant == "human-prompt" and not (spec.coordinator and spec.coordinator_notebook):
            raise ValueError("Human-prompt coordination requires coordinator and notebook")
        if (spec.coordinator or self.coordinator_complete is not None) and (
            not spec.coordinator or not spec.shared_workspace or not spec.coop_tools
            or spec.worker != "mini_swe" or spec.team_roles
            or spec.adaptive or spec.decompose or spec.best_of_n != 1
            or spec.seed_prior or spec.contract_first or spec.allow_spawn_tool
        ):
            raise ValueError(
                "coordinator requires a single shared-workspace mini_swe coop-tools no-seed team "
                "with coordinator enabled (no team_roles, adaptive, decomposition, best-of-N, contract_first or helpers)"
            )
        if llm is None and llm_factory is None and spec.worker != "mini_swe":
            raise ValueError("provide either llm or llm_factory")
        bus = self.bus or InMemoryBus(spec.run_id)
        assignments = self._build_assignments(spec)
        if self.checkpoint_dir is not None:
            from cooperagents.repair import describe_gate

            self.checkpoint_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
            config = asdict(spec)
            config["completion_gate"] = describe_gate(spec.completion_gate)
            (self.checkpoint_dir / "run.json").write_text(json.dumps(dict(
                spec=config, assignments=[asdict(a) for a in assignments], step_limit=self.step_limit,
                cost_limit=self.cost_limit, command_timeout=self.command_timeout,
                coordination_variant=self.coordination_variant,
                coordinator_implementation_enabled=self.coordinator_implementation_enabled,
            ), indent=2))
        if spec.coordinator:
            ids = [a.agent_id for a in assignments]
            if not ids or len(set(ids)) != len(ids) or "coordinator" in ids:
                raise ValueError("Coordinator requires a nonempty, unique worker roster without the reserved ID 'coordinator'")
            if spec.coordinator_notebook:
                path = self.coordinator_notebook_path
                if path is None or path.name != "notebook.md" or path.exists():
                    raise ValueError("Coordinator notebook requires a new run-specific coordinator_notebook_path named notebook.md")
        if self.coordination_mode != "direct" and (
            not spec.coordinator or not spec.coordinator_notebook or len(assignments) != 2 or spec.wait_protocol
        ):
            raise ValueError("Group coordination requires two workers, coordinator, notebook, and no wait-protocol")
        if self.coordination_mode == "group-chat-planning" and (spec.task_board or spec.claim_mode or spec.allow_spawn_tool):
            raise ValueError("Opening planning does not support task-board, claim-mode, or helpers")
        if self.initial_plan is not None:
            if (not spec.shared_workspace or spec.worker != "mini_swe" or not spec.coop_tools or spec.seed_prior
                    or spec.best_of_n != 1 or spec.adaptive or spec.decompose or spec.contract_first
                    or spec.task_board or spec.claim_mode or spec.allow_spawn_tool):
                raise ValueError("Initial plan requires the standard two-worker no-seed coop-tools implementation path")
            self.initial_plan.validate_assignments(spec.repo, spec.task_id, assignments)
        if spec.adaptive:
            return self._run_adaptive(spec, assignments, bus, env_factory, llm, llm_factory)
        if spec.decompose:
            return self._run_decomposed(spec, assignments, bus, env_factory, llm, llm_factory, planner)
        if spec.shared_workspace and spec.best_of_n > 1:
            return self._run_best_of_n(spec, assignments, env_factory, llm, llm_factory, selector)
        if spec.shared_workspace:
            return self._run_isolated(spec, assignments, bus, env_factory, llm, llm_factory)
        n_seed = len(assignments)
        cap = spec.max_agents if spec.max_agents is not None else n_seed
        spawning = spec.allow_spawn and cap > n_seed

        def pick_llm(agent_id: str, role: str) -> LLMClient:
            if llm_factory is not None:
                return llm_factory(agent_id, role)
            if llm is None:
                raise ValueError("either llm or llm_factory must be provided")
            return llm

        seeds: dict[str, AgentResult] = {}
        helpers: dict[str, AgentResult] = {}
        envs: list[Environment] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()
        live = 0
        total_agents = n_seed

        def worker(agent_id: str, role: str, task: str, feature_id: int | None, *, is_helper: bool) -> None:
            nonlocal live
            try:
                env = env_factory(agent_id)
                with lock:
                    envs.append(env)
                agent = Agent(
                    agent_id=agent_id,
                    role=role,
                    task=task,
                    env=env,
                    llm=pick_llm(agent_id, role),
                    bus=bus,
                    feature_id=feature_id,
                    allow_spawn=spawning,
                    step_limit=self.step_limit,
                    cost_limit=self.cost_limit,
                    command_timeout=self.command_timeout,
                )
                result = agent.run()
            except Exception as e:  # noqa: BLE001 - never let a worker kill the run
                result = AgentResult(agent_id=agent_id, role=role, status="error", feature_id=feature_id, error=str(e))
            with lock:
                (helpers if is_helper else seeds)[agent_id] = result
                live -= 1
            self._emit(f"{agent_id} done: {result.status}")

        def start(agent_id: str, role: str, task: str, feature_id: int | None, *, is_helper: bool) -> None:
            nonlocal live
            with lock:
                live += 1
            t = threading.Thread(target=worker, args=(agent_id, role, task, feature_id), kwargs={"is_helper": is_helper})
            threads.append(t)
            t.start()

        def supervise() -> None:
            nonlocal total_agents
            while True:
                req = bus.spawn_pop(timeout=0.5)
                if req is None:
                    with lock:
                        if live == 0:
                            break
                    continue
                with lock:
                    granted = total_agents < cap
                    if granted:
                        total_agents += 1
                if not granted:
                    bus.spawn_mark(req.id, outcome="capped")
                    self._emit(f"spawn capped (cap={cap}) from {req.requested_by}")
                    continue
                idx = bus.spawn_next_index()
                helper_id = f"helper{idx}"
                bus.spawn_mark(req.id, outcome="granted", agent_id=helper_id)
                self._emit(f"spawned {helper_id} for {req.requested_by}")
                start(helper_id, req.role or "helper", req.task, None, is_helper=True)

        start_time = time.time()
        supervisor: threading.Thread | None = None
        try:
            for a in assignments:
                start(a.agent_id, a.role, a.task, a.feature_id, is_helper=False)
            if spawning:
                supervisor = threading.Thread(target=supervise, daemon=True)
                supervisor.start()
                supervisor.join()  # returns only when the whole pool is idle
            for t in list(threads):
                t.join()
        finally:
            for env in envs:
                env.cleanup()

        duration = time.time() - start_time
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=seeds,
            helpers=helpers,
            duration_seconds=duration,
            metrics=coordination_metrics(bus.task_events(), final_tasks=bus.list_tasks()),
            spawn_metrics=spawn_metrics(bus.spawn_events()) if spawning else {},
        )


__all__ = ["UnifiedHarness", "EnvFactory", "LLMFactory"]
