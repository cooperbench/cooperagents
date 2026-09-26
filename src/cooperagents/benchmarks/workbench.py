"""WorkBench benchmark on the state substrate.

WorkBench is outcome-based tool use: an instruction is solved by a sequence of
tool calls against sandboxed business databases, graded on the final DB state.
The current (LangChain-free) WorkBench holds its sandbox in a **thread-local**
``ToolState``, so cooperagents' one-thread-per-agent model gives each agent an
isolated sandbox for free — no shim, no subprocess.

Wiring is composition only: WorkBench is located as a sibling checkout, its
``src`` put on ``sys.path``, and its official tools + scorer
(``is_correct`` / ``has_side_effects``) imported and called. The WorkBench repo
is never modified; the one runtime touch is rewriting its ``_CSV_PATHS`` to
absolute paths (in memory) so it loads its data regardless of cwd.

The deliverable is the ordered list of executed **side-effect** action strings
(WorkBench's ``name.func(arg="v")`` format); the scorer re-executes them against
a fresh sandbox and compares final DB state. Pinned to the v1 (2024, 690-task)
data. Scoring is fully offline and deterministic.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cooperagents.benchmarks.base import StateBenchmark
from cooperagents.env.artifact import Artifact, StateArtifact
from cooperagents.env.base import Environment
from cooperagents.env.state import StateEnv
from cooperagents.eval.scoring import Scorer, TaskScore
from cooperagents.llm import Action
from cooperagents.tools import ToolSet

# WorkBench's fixed benchmark clock (now = Thursday 2023-11-30).
DATETIME_PREFIX = (
    "Today's date is Thursday, 2023-11-30 and the current time is 00:00:00. "
    "Remember the current date and time when completing tasks. "
    "Meetings must not start before 9am or end after 6pm."
)

_DOMAINS = ["email", "calendar", "analytics", "project_management", "customer_relationship_manager", "multi_domain"]

_lock = threading.Lock()
_wb: dict[str, Any] = {}


def find_workbench(explicit: str | None = None) -> Path:
    """Locate the WorkBench checkout (sibling of cooperagents by default)."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.getenv("WORKBENCH_DIR"):
        candidates.append(Path(os.environ["WORKBENCH_DIR"]))
    candidates.append(Path.cwd() / "WorkBench")
    candidates.append(Path.cwd().parent / "WorkBench")
    candidates.append(Path(__file__).resolve().parents[4] / "WorkBench")
    for c in candidates:
        if (c / "src" / "evals" / "evaluation.py").is_file():
            return c
    raise FileNotFoundError("Could not find WorkBench (with src/evals/evaluation.py). Set WORKBENCH_DIR.")


def _load_workbench(workbench_dir: str | None = None) -> dict[str, Any]:
    """Import WorkBench's official tools + scorer once, with absolute data paths."""
    if _wb:
        return _wb
    with _lock:
        if _wb:
            return _wb
        root = find_workbench(workbench_dir)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from src.tools import state as wb_state

        # Rewrite relative CSV paths to absolute so WorkBench loads regardless of
        # cwd (in-memory only — the repo file is untouched). Must precede any
        # get_state()/reset_state(), which lazily loads the pristine snapshot.
        wb_state._CSV_PATHS = {k: str(root / v) for k, v in wb_state._CSV_PATHS.items()}

        from src.evals.actions import convert_intermediate_step_to_function_call
        from src.evals.evaluation import has_side_effects, is_correct
        from src.tools.state import reset_state
        from src.tools.toolkits import all_tools, tools_with_side_effects

        _wb.update(
            root=root,
            tools={t.name: t for t in all_tools},
            side_effect_names={t.name for t in tools_with_side_effects},
            is_correct=is_correct,
            has_side_effects=has_side_effects,
            to_call=convert_intermediate_step_to_function_call,
            reset_state=reset_state,
        )
        return _wb


@dataclass
class WorkBenchInstance:
    instance_id: str
    task: str
    outcome: list[str]
    domains: list[str]


def _harvest(env: StateEnv) -> Artifact:
    actions = env.store.get("actions", [])
    return StateArtifact(state=json.dumps(actions))


class WorkBenchToolSet(ToolSet):
    """Exposes WorkBench's 27 tools; records executed side-effect calls as the
    deliverable. Tool calls run against the calling thread's WorkBench sandbox."""

    def __init__(self) -> None:
        wb = _load_workbench()
        self._tools = wb["tools"]
        self._side = wb["side_effect_names"]
        self._to_call = wb["to_call"]

    def specs(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for name, t in self._tools.items():
            desc = t.signature_str + " — " + (t.description or "").strip()[:100]
            out.append({"name": name, "description": desc})
        return out

    def dispatch(self, env: Environment, action: Action) -> tuple[str, bool] | None:
        assert isinstance(env, StateEnv)
        tool = self._tools.get(action.tool)
        if tool is None:
            return None
        args = {k: str(v) for k, v in action.args.items()}
        try:
            result = tool.func(**action.args)
        except Exception as e:  # noqa: BLE001 - surface tool errors to the agent
            result = f"error: {e}"
        if action.tool in self._side:
            env.store.setdefault("actions", []).append(self._to_call(action.tool, args))
        return str(result), False


class WorkBenchScorer(Scorer):
    name = "workbench"

    def __init__(self) -> None:
        wb = _load_workbench()
        self._is_correct = wb["is_correct"]
        self._has_side_effects = wb["has_side_effects"]

    def score(self, instance: Any, submission: str) -> TaskScore:
        predicted = json.loads(submission) if submission.strip() else []
        gt = list(getattr(instance, "outcome", []))
        # The official scorer executes actions against the thread-local sandbox
        # and resets it; serialize to keep concurrent scoring deterministic.
        with _lock:
            correct = bool(self._is_correct(predicted, gt, ""))
            side = bool(self._has_side_effects(predicted, correct))
        return TaskScore(
            instance_id=str(getattr(instance, "instance_id", "")),
            success=1.0 if correct else 0.0,
            detail={"side_effects": side, "n_actions": len(predicted)},
        )


class WorkBenchBenchmark(StateBenchmark):
    name = "workbench"

    def __init__(self, *, workbench_dir: str | None = None) -> None:
        self._dir = workbench_dir

    def instances(self, split: str = "email") -> list[WorkBenchInstance]:
        import pandas as pd

        wb = _load_workbench(self._dir)
        path = wb["root"] / "data" / "processed" / "tasks_and_outcomes" / "v1" / f"{split}_tasks_and_outcomes.csv"
        df = pd.read_csv(path, dtype=str)
        out: list[WorkBenchInstance] = []
        for i, row in df.iterrows():
            out.append(
                WorkBenchInstance(
                    instance_id=f"{split}-{i}",
                    task=str(row["task"]),
                    outcome=list(ast.literal_eval(row["outcome"])),
                    domains=list(ast.literal_eval(row["domains"])),
                )
            )
        return out

    def task_prompt(self, instance: Any) -> str:
        return f"{DATETIME_PREFIX}\n\nComplete this task using the available tools:\n{instance.task}"

    def make_env(self, instance: Any) -> StateEnv:
        # Runs in the agent's worker thread; resets THIS thread's sandbox.
        _load_workbench(self._dir)["reset_state"]()
        return StateEnv(harvest=_harvest)

    def toolset(self) -> ToolSet:
        return WorkBenchToolSet()

    def scorer(self) -> Scorer:
        return WorkBenchScorer()


__all__ = ["WorkBenchBenchmark", "WorkBenchToolSet", "WorkBenchScorer", "WorkBenchInstance", "find_workbench"]
