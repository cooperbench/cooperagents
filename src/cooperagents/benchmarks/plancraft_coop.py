"""PlanCraft under the cooperative-notebook protocol (no oracle).

NOTE (non-principled experiment): this shared-notebook variant uses a single
mutable "solution" slot, which departs from cooperagents' principle of isolated
execution + reconcile over per-agent artifacts. The principled path is the plain
``plancraft`` benchmark run as a cooperagents team (own gym per agent, bus
coordination, success-max reducer). This file is kept for comparison only.

The oracle variant (:class:`~cooperagents.benchmarks.plancraft.PlanCraftBenchmark`)
runs N independent live-scored episodes and selects the best by ground-truth
success — an oracle pass@N upper bound. This variant removes the oracle:

  * Each agent gets its OWN scratch gym to experiment in (unscored).
  * Agents share one :class:`SharedNotebook`: a scratchpad to pool recipes and
    subplans, and a solution slot.
  * ``solution_commit`` copies the committing agent's experimented action
    sequence into the shared solution slot (last write wins — the team must
    converge; nothing selects for them).
  * The single final solution sequence is executed ONCE in a fresh scored gym;
    its gym reward is the grade. This is PlanCraft's native evaluator applied to
    one jointly-authored plan.

Solo (team_size 1) and team (team_size > 1) use the identical protocol — one
agent is simply the sole author — so the comparison isolates the impact of the
cooperation setup rather than a change of regime.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from cooperagents.benchmarks.notebook import NotebookArtifact, SharedNotebook
from cooperagents.benchmarks.plancraft import MAX_STEPS, PlanCraftBenchmark, _action_string
from cooperagents.env.artifact import Artifact
from cooperagents.env.base import Environment
from cooperagents.env.state import StateEnv
from cooperagents.eval.scoring import Scorer, TaskScore
from cooperagents.llm import Action, LLMClient
from cooperagents.reducers import Reducer, best_of_first_nonempty
from cooperagents.tools import ToolSet

_GYM_TOOLS = {"move", "smelt", "think", "search", "impossible"}
_PLAN_TOOLS = {"move", "smelt", "impossible"}  # state-changing actions that form the scored sequence


class PlanCraftCoopToolSet(ToolSet):
    """Scratch-gym experimentation tools plus shared-notebook coordination tools.

    Auto-capture: every state-changing action (move/smelt/impossible) updates the
    shared solution to this agent's running plan, so there is no separate commit
    step — the agent's natural actions ARE the contribution (last writer wins).

    ``env.sandbox`` carries ``{"gym": <this agent's scratch gym>, "notebook":
    <shared notebook>, "plan": [...]}``; ``env.agent_id`` identifies the writer.
    """

    def specs(self) -> list[dict[str, str]]:
        return [
            {"name": "move", "description": "Move items in your scratch gym; appends to the shared solution. args: {from, to, quantity}"},
            {"name": "smelt", "description": "Smelt in your scratch gym; appends to the shared solution. args: {from, to, quantity}"},
            {"name": "search", "description": "Look up a crafting/smelting recipe. args: {recipe}"},
            {"name": "think", "description": "Private reasoning, no effect. args: {thought}"},
            {"name": "impossible", "description": "Mark the target uncraftable; also sets the shared solution. args: {reason}"},
            {"name": "note_write", "description": "Append to the SHARED scratchpad teammates can read. args: {text}"},
            {"name": "note_read", "description": "Read the SHARED scratchpad. args: {}"},
            {"name": "solution_read", "description": "Read the current shared SOLUTION. args: {}"},
        ]

    def dispatch(self, env: Environment, action: Action) -> tuple[str, bool] | None:
        assert isinstance(env, StateEnv)
        aid = str(getattr(env, "agent_id", "?"))
        sandbox = env.sandbox
        nb: SharedNotebook = sandbox["notebook"]
        gym = sandbox["gym"]

        if action.tool in _GYM_TOOLS:
            action_str = _action_string(action.tool, action.args)
            obs, reward, terminated, truncated, info = gym.step(action_str)
            if action.tool in _PLAN_TOOLS:
                sandbox.setdefault("plan", []).append({"tool": action.tool, "args": dict(action.args)})
                # Auto-capture: the agent's running plan is the shared solution.
                nb.write_solution(aid, json.dumps(sandbox["plan"]))
            text = obs.get("text", "") if isinstance(obs, dict) else str(obs)
            env.record(f"{action_str}\n{text}")
            return text, False

        if action.tool == "note_write":
            text = str(action.args.get("text", ""))
            nb.write_scratch(aid, text)
            env.record(f"note_write: {text[:120]}")
            return "noted to shared scratchpad", False
        if action.tool == "note_read":
            scratch = nb.read_scratch()
            env.record("note_read")
            return scratch or "(scratchpad empty)", False
        if action.tool == "solution_read":
            cur = nb.read_solution()
            env.record("solution_read")
            return (f"current solution: {cur}" if cur else "(solution empty)"), False
        return None


def _execute(gym: Any, actions: list[dict]) -> tuple[bool, int, str]:
    """Execute a committed action sequence in a fresh gym; return (success, steps, reason)."""
    success, steps, reason = False, 0, ""
    for a in actions:
        if not isinstance(a, dict) or "tool" not in a:
            continue
        obs, reward, terminated, truncated, info = gym.step(_action_string(str(a["tool"]), dict(a.get("args", {}))))
        steps += 1
        reason = str(info.get("reason", "")) if isinstance(info, dict) else ""
        if reward > 0.0:
            success = True
        if terminated or truncated:
            break
    return success, steps, reason


class PlanCraftCoopScorer(Scorer):
    """Executes the jointly-authored solution sequence ONCE in a fresh gym."""

    name = "plancraft-coop"

    def __init__(self, gym_factory: Any) -> None:
        self._gym_factory = gym_factory

    def score(self, instance: Any, submission: str) -> TaskScore:
        try:
            actions = json.loads(submission) if submission.strip() else []
            if not isinstance(actions, list):
                actions = []
        except (ValueError, TypeError):
            actions = []
        success, steps, reason = _execute(self._gym_factory(instance), actions)
        return TaskScore(
            instance_id=str(getattr(instance, "id", "")),
            success=1.0 if success else 0.0,
            detail={"steps": steps, "committed_actions": len(actions), "reason": reason, "empty_solution": not actions},
        )


class PlanCraftCoopBenchmark(PlanCraftBenchmark):
    """PlanCraft with a shared notebook and a single graded solution (no oracle)."""

    name = "plancraft-coop"

    def __init__(self, *, max_steps: int = MAX_STEPS, resolution: str = "high") -> None:
        super().__init__(max_steps=max_steps, resolution=resolution)
        self._nbs: dict[int, SharedNotebook] = {}
        self._nb_lock = threading.Lock()

    def _notebook(self, instance: Any) -> SharedNotebook:
        with self._nb_lock:
            return self._nbs.setdefault(id(instance), SharedNotebook())

    def task_prompt(self, instance: Any) -> str:
        gym = self._gym(instance)
        obs, _r, _t, _tr, _info = gym.step("")
        initial = obs.get("text", "") if isinstance(obs, dict) else str(obs)
        return (
            "You are on a TEAM solving a Minecraft-style crafting task together. Each of you has a "
            "private scratch gym to experiment in (move/smelt/search/think/impossible). Your "
            "move/smelt/impossible actions automatically build the shared SOLUTION — the exact "
            "action sequence that will be executed once and graded (so craft the target with your "
            "actions, or make a single impossible action if it cannot be crafted). You also share a "
            "scratchpad (note_write/note_read) to pool recipes and partial plans with teammates, and "
            "can inspect the current shared solution with solution_read. The LAST agent to act sets "
            "the final solution, so coordinate via the scratchpad rather than overwriting a working "
            f"sequence. You have at most {self.max_steps} steps each.\n\n{initial}"
        )

    def make_env(self, instance: Any) -> StateEnv:
        nb = self._notebook(instance)

        def harvest(_e: StateEnv) -> Artifact:
            return NotebookArtifact(nb, trajectory="\n".join(_e.log))

        env = StateEnv(harvest=harvest)
        env.sandbox = {"gym": self._gym(instance), "notebook": nb, "plan": []}
        return env

    def toolset(self) -> ToolSet:
        return PlanCraftCoopToolSet()

    def scorer(self) -> Scorer:
        return PlanCraftCoopScorer(self._gym)

    def team_reducer(self, llm: LLMClient) -> Reducer:
        # All agents share one notebook, so every candidate artifact resolves to
        # the same final solution; a trivial non-empty selection returns it. No
        # oracle, no outcome-based ranking.
        return best_of_first_nonempty


__all__ = ["PlanCraftCoopBenchmark", "PlanCraftCoopToolSet", "PlanCraftCoopScorer"]
