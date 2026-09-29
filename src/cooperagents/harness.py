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
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from cooperagents.agent import Agent
from cooperagents.bus.base import TeamBus
from cooperagents.bus.memory import InMemoryBus
from cooperagents.coordination_prompts import (
    HUMAN_COORDINATOR_BASE,
    HUMAN_COORDINATOR_INITIAL,
    HUMAN_WORKER_WORKFLOW,
)
from cooperagents.env.base import Environment
from cooperagents.llm import LLMClient
from cooperagents.metrics import coordination_metrics, spawn_metrics
from cooperagents.patching import strip_test_sections
from cooperagents.planner import ancestors, plan_decomposition, topo_levels
from cooperagents.types import AgentResult, Assignment, RunResult, SubTask, TeamSpec

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
Worker warnings are mechanical hints (LOOP, STALL, COLLISION), not proof that an intervention is needed.
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
        coordination_variant: str = "current",
        repo: str = "",
        task_id: int = 0,
        step_limit: int = 0,
        time_limit_s: int | None = None,
        trace=None,
        complete: Callable[[str], list[dict[str, str]]] | None = None,
    ) -> None:
        from cooperagents.planner import _default_planner_complete

        if coordination_variant not in ("current", "human_in_loop"):
            raise ValueError(f"Unknown coordination variant: {coordination_variant}")
        if coordination_variant == "human_in_loop" and notebook_path is None:
            raise ValueError("Human-in-loop coordination requires a notebook")
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
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fired: list[dict] = []
        self._pending: list[dict] = []
        self._processed: tuple = ()
        self._last_error = ""
        self._notebook_path = notebook_path
        self._notebook = ""
        self._version = -1
        self._injected = complete is not None
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
                tools=[_SEND_MESSAGE_TOOL, *([_UPDATE_NOTEBOOK_TOOL] if notebook_path is not None else [])],
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

    def _emit(self, event: str, **data) -> None:
        if self.trace is not None:
            self.trace(event, **data)

    def worker_instructions(self) -> str:
        if self.coordination_variant == "human_in_loop":
            return "\n\n" + HUMAN_WORKER_WORKFLOW
        note = ""
        if self._notebook_path is not None:
            note = (
                f"\nAfter acknowledging a new notice, read the coordinator's read-only notebook at {_COORDINATOR_PATH} "
                "before starting affected work, after an update notification, and after context compaction. "
                f"Use `cat {_COORDINATOR_PATH}`; the file is outside your code repository. "
                "Read the version in the file header; proposals still require confirmation."
            )
        return _COORDINATION_WORKFLOW + note

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
            self._emit("register", target=agent_id)

    def mark_finished(self, agent_id: str, status: str) -> None:
        with self._lock:
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
        self._emit("notebook_update", previous_version=previous, version=version, content=content)

    def _parse_actions(self, response: list[dict[str, str]]) -> list[_NotebookUpdate | _CoordinatorMessage]:
        if not isinstance(response, list):
            raise ValueError("Expected tool calls")
        if len(response) > len(self._assignments) + 1:
            raise ValueError("Too many actions")
        parsed: list[_NotebookUpdate | _CoordinatorMessage] = []
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
                if not isinstance(recipient, str) or recipient not in self._assignments or recipient in recipients:
                    raise ValueError("Unknown or repeated message recipient")
                recipients.add(recipient)
                parsed.append(_CoordinatorMessage(recipient, content))
            else:
                raise ValueError("Unknown action or fields")
        return parsed

    def _apply_actions(self, actions: list[_NotebookUpdate | _CoordinatorMessage]) -> None:
        previous_version = self._version
        for action in actions:
            if isinstance(action, _NotebookUpdate):
                self.update_notebook(action.content)
        notebook_updated = self._version != previous_version
        with self._lock:
            for action in actions:
                if not isinstance(action, _CoordinatorMessage):
                    continue
                if self._stop.is_set() or action.recipient in self._finished:
                    self._emit("message_dropped", target=action.recipient, reason="worker_finished_or_stopped", content=action.content)
                    continue
                version = self._version if self._notebook_path is not None else None
                if self.coordination_variant == "human_in_loop":
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
                record = {"action": {k: cls._clip(v) if isinstance(v, str) else v for k, v in action.items()}, "result": None}
                if output is not None:
                    extra = output["extra"]
                    record["result"] = {
                        "returncode": extra.get("returncode"),
                        "output": cls._clip(str(extra.get("raw_output", ""))),
                        "exception_info": cls._clip(str(extra.get("exception_info") or "")),
                    }
                records.append(record)
        return records[-6:]

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
    def _add_warnings(workers: list[dict]) -> None:
        for worker in workers:
            warnings = worker["warnings"] = []
            actions = worker["recent_actions"]
            commands = [record["action"].get("command", "") for record in actions]
            commands = [command for command in commands if isinstance(command, str) and command.strip()]
            if len(commands) >= 6:
                heads = [" ".join(command.split()[:2]) for command in commands[-6:]]
                if max(heads.count(head) for head in set(heads)) >= 4:
                    warnings.append("LOOP: at least 4 of the last 6 commands share their first two words")
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
                overlap = sorted(files.intersection(peer["modified_files"] or []))
                if overlap:
                    warnings.append(f"COLLISION: modified files overlap with {peer['id']}: {', '.join(overlap[:3])}")

    def decide(self, *, initial: bool = False) -> None:
        replies = self._bus.receive("coordinator")
        self._pending.extend(replies)
        if replies:
            self._emit("replies", messages=replies)
        with self._lock:
            if self._stop.is_set():
                return
            agents, finished = dict(self._agents), dict(self._finished)
            version, notebook = self._version, self._notebook
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
        if not initial and not self._pending and progress == self._processed:
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
        self._add_warnings(workers)
        observation = {
            "repo": self._repo,
            "task_id": self._task_id,
            "initial": initial,
            "workers": workers,
            "replies": list(self._pending),
            "previous_error": self._last_error,
        }
        if self.coordination_variant == "human_in_loop":
            protocol = HUMAN_COORDINATOR_BASE + "\n"
            if initial:
                protocol += "\nINITIAL DECISION:\n" + HUMAN_COORDINATOR_INITIAL + "\n"
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
        if self._notebook_path is not None:
            observation["notebook"] = {"version": version, "content": notebook}
            if self.coordination_variant == "current":
                protocol += (
                    "Call update_notebook at most once, with the complete Markdown (at most 8000 characters).\n"
                    "Prefer a short notebook (about 1000-2000 characters) of current responsibilities/regions, "
                    "interfaces/dependencies, pending issues and handoff. Preserve unresolved questions when replacing it.\n"
                    "Distinguish proposed, worker-reported, confirmed (explicit replies) and verified (observed checks).\n"
                    f"Workers read {_COORDINATOR_PATH}; an update automatically sends a path/version reminder, not the full text.\n"
                )
        else:
            protocol += "Notebook is disabled; only send_message is available.\n"
        prompt = protocol + "\nOBSERVATION:\n" + json.dumps(observation, ensure_ascii=False)
        count = len(self._pending)
        self._emit("observation", **observation)
        try:
            if self._complete is None:
                raise ConnectionError("Coordinator completion client unavailable")
            response = self._complete(prompt)
        except Exception as exc:
            self._last_error = f"Completion failed: {type(exc).__name__}"
            self._emit("decision", initial=initial, valid=False, outcome="transport_error", error=str(exc))
            if self._injected:
                raise
            return
        try:
            actions = self._parse_actions(response)
        except (ValueError, TypeError, RecursionError) as exc:
            self._last_error = str(exc)
            self._emit("decision", initial=initial, valid=False, outcome="invalid_response", response=response, error=str(exc))
            if self._injected:
                raise
            return
        self._emit("decision", initial=initial, valid=True, outcome="actions" if actions else "no_op", response=response)
        self._apply_actions(actions)
        del self._pending[:count]
        self._processed = progress
        self._last_error = ""

    def run(self) -> None:
        try:
            while not self._stop.wait(20):
                self.decide()
        except Exception as exc:
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


class _GitShareSync:
    """TK-git: harness-side push of each agent's working tree to a per-agent
    branch on the shared bare repository. `git stash create` produces a commit
    object from the dirty tree while leaving the agent's HEAD, index, and
    working tree unchanged; a clean tree pushes HEAD."""

    def __init__(self, envs: dict[str, Environment]) -> None:
        self._envs = envs
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(45):
            for aid, env in list(self._envs.items()):
                try:
                    env.execute(
                        "C=$(git stash create 2>/dev/null); "
                        f"git push -q -f shared ${{C:-HEAD}}:refs/heads/{aid} 2>/dev/null || true",
                        timeout=60,
                    )
                except Exception:  # noqa: BLE001 - sync must never disturb the run
                    pass


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
        coordination_variant: str = "current",
    ) -> None:
        if coordinator_complete is not None and not callable(coordinator_complete):
            raise TypeError("coordinator_complete must be callable")
        self.coordinator_complete = coordinator_complete
        if coordination_variant not in ("current", "human_in_loop"):
            raise ValueError(f"Unknown coordination variant: {coordination_variant}")
        self.coordination_variant = coordination_variant
        self.coordinator_notebook_path = Path(coordinator_notebook_path).resolve() if coordinator_notebook_path is not None else None
        self.trajectory = trajectory
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
                    comm=BusComm(bus, agent_id) if spec.coop_tools else None,
                    poller=poller,
                    tool_protocol=spec.tool_protocol,
                    task_board=TaskBoard(bus, agent_id) if spec.task_board else None,
                    wait_protocol=spec.wait_protocol,
                    spawn_handler=make_spawn_handler(agent_id) if spec.allow_spawn_tool else None,
                    time_limit_s=time_limit_s,
                    monitor=monitor,
                    git_share=spec.git_share,
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
                        coop_envs, spec.model, assignments=assignments, bus=bus,
                        notebook_path=self.coordinator_notebook_path if spec.coordinator_notebook else None,
                        coordination_variant=self.coordination_variant,
                        repo=spec.repo, task_id=spec.task_id, step_limit=self.step_limit, time_limit_s=spec.agent_time_limit,
                        complete=self.coordinator_complete,
                        trace=partial(self.trajectory.emit, "coordinator") if self.trajectory else None,
                    ) if spec.coordinator else None
                )
                for assignment in assignments:
                    env = env_factory(assignment.agent_id)
                    cleanup.callback(env.cleanup)
                    coop_envs[assignment.agent_id] = env
                gitsync = None
                if spec.git_share:
                    first = True
                    for _aid, _e in coop_envs.items():
                        if first:
                            _e.execute(f"git init -q --bare {_GITSHARE} 2>/dev/null || true")
                            first = False
                        _e.execute(f"git remote add shared {_GITSHARE} 2>/dev/null || true")
                        _e.execute(f"git push -q shared HEAD:refs/heads/{_aid} 2>/dev/null || true")
                    gitsync = _GitShareSync(coop_envs)
                    cleanup.callback(gitsync.stop)
                    threading.Thread(target=gitsync.run, daemon=True).start()
                coordinator_thread = None
                if coordinator is not None:
                    coordinator.verify_mounts()
                    try:
                        coordinator.decide(initial=True)
                    except Exception as exc:
                        raise RuntimeError("Coordinator failed during initial decision") from exc
                    coordinator_thread = threading.Thread(target=coordinator.run, daemon=True)
                    coordinator_thread.start()
                    cleanup.callback(coordinator.finish, coordinator_thread)


                def collect_diff(env, aid: str) -> str:
                    """Agent diff with share fallback: agents sometimes wipe their
                    working tree at the end (stash/checkout/rm to "verify a clean
                    patch"); the 45s share sync holds their last state, so recover
                    the diff from the pushed branch when the tree reads empty."""
                    d = strip_test_sections(env.git_diff())
                    if d.strip():
                        return d
                    base = getattr(env, "_base_commit", "") or "HEAD"
                    r = env.execute(
                        f"git fetch -q shared {aid} 2>/dev/null && "
                        f"git diff {base} FETCH_HEAD -- . 2>/dev/null")
                    if r.stdout.strip():
                        return strip_test_sections(r.stdout)
                    # last resort: the agent destroyed even its .git — read the
                    # share volume directly with a throwaway container
                    if hasattr(env, "recover_shared_diff"):
                        return strip_test_sections(env.recover_shared_diff(aid))
                    vol = next((v.split(":")[0] for v in getattr(env, "volumes", None) or []
                                if v.endswith(":/cbshared")), None)
                    if vol and base != "HEAD":
                        import subprocess as _sp
                        rr = _sp.run(["docker", "run", "--rm", "-v", f"{vol}:/cb",
                                      "alpine/git", "--git-dir=/cb/repo.git",
                                      "diff", base, aid],
                                     capture_output=True, text=True, timeout=120)
                        if rr.returncode == 0 and rr.stdout.strip():
                            return strip_test_sections(rr.stdout)
                    return d

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
                        return a.agent_id, r, collect_diff(env, a.agent_id)
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

                        return a.agent_id, r, collect_diff(env, a.agent_id)
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
                if spec.repair_integrator and not _gate(env):
                    for _repair_attempt in range(max(1, spec.repair_attempts)):
                        repair_brief = _merge_repair_task(assignments_all)
                        if spec.focused_repair:
                            ev = _gather_merge_evidence(env)
                            if ev:
                                repair_brief += (
                                    "\n\nEVIDENCE — the harness already located the damage; fix THESE "
                                    "directly instead of searching:\n\n" + ev
                                )
                        # Run ONE repair agent per attempt; stop early if the tree recovers.
                        seeds[f"integrator{_repair_attempt + 1}"] = run_on_shared(
                            env,
                            f"integrator{_repair_attempt + 1}",
                            "integrator",
                            repair_brief,
                            None,
                            step_limit=spec.repair_step_limit,
                            time_limit_s=spec.repair_time_limit,
                        )
                        env.execute(
                            "find . -path ./.git -prune -o \\( -name '*.rej' -o -name '*.orig' \\) -print0 2>/dev/null | xargs -0 -r rm -f"
                        )
                        if _gate(env):
                            break
                integrated_patch = strip_test_sections(env.git_diff())
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
                **({"coordinator_events": coordinator.events()} if spec.coordinator and coordinator is not None else {}),
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
        if self.coordination_variant == "human_in_loop" and not (spec.coordinator and spec.coordinator_notebook):
            raise ValueError("Human-in-loop coordination requires coordinator and notebook")
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
        if spec.coordinator:
            ids = [a.agent_id for a in assignments]
            if not ids or len(set(ids)) != len(ids) or "coordinator" in ids:
                raise ValueError("Coordinator requires a nonempty, unique worker roster without the reserved ID 'coordinator'")
            if spec.coordinator_notebook:
                path = self.coordinator_notebook_path
                if path is None or path.name != "notebook.md" or path.exists():
                    raise ValueError("Coordinator notebook requires a new run-specific coordinator_notebook_path named notebook.md")
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
