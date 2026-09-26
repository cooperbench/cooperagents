"""PlanCraft benchmark on the state substrate.

PlanCraft is a stateful crafting simulator: each turn the agent emits a typed
action (move/smelt/think/search/impossible) and receives the next observation.
That maps directly onto a :class:`~cooperagents.tools.ToolSet` — each tool call
is one ``env.step(action_str)`` on a per-agent ``PlancraftGymWrapper`` held on
``StateEnv.sandbox``, so the statefulness needs no harness stepper and each
agent has its own isolated simulator copy (Independent MAS / SAS).

Scoring is intrinsic and offline: the gym wrapper returns ``reward == 1.0`` on
success (target crafted, or a correct ``impossible`` declaration). ``plancraft``
is imported lazily so the dependency is optional.
"""

from __future__ import annotations

import json
from typing import Any

from cooperagents.benchmarks.base import StateBenchmark
from cooperagents.env.artifact import Artifact, StateArtifact
from cooperagents.env.state import StateEnv
from cooperagents.eval.scoring import Scorer, TaskScore
from cooperagents.llm import Action
from cooperagents.tools import ToolSet

# Episode budget the PlanCraft paper/harness uses.
MAX_STEPS = 30

_TOOL_SPECS = [
    {"name": "move", "description": "Move items between slots. args: {from, to, quantity} e.g. from='[I19]' to='[A1]' quantity='1'"},
    {"name": "smelt", "description": "Smelt items from one slot to another. args: {from, to, quantity}"},
    {"name": "think", "description": "Private reasoning, no effect on the world. args: {thought}"},
    {"name": "search", "description": "Look up a crafting/smelting recipe by item name. args: {recipe}"},
    {"name": "impossible", "description": "Declare the target cannot be crafted from this inventory. args: {reason}"},
]


def _action_string(tool: str, args: dict[str, Any]) -> str:
    if tool == "move":
        return f"move: from {args.get('from', '')} to {args.get('to', '')} with quantity {args.get('quantity', 1)}"
    if tool == "smelt":
        return f"smelt: from {args.get('from', '')} to {args.get('to', '')} with quantity {args.get('quantity', 1)}"
    if tool == "think":
        return f"think: {args.get('thought', '')}"
    if tool == "search":
        return f"search: {args.get('recipe', '')}"
    return f"impossible: {args.get('reason', '')}"


class PlanCraftToolSet(ToolSet):
    """Steps the per-agent PlancraftGymWrapper held on ``env.sandbox``."""

    def specs(self) -> list[dict[str, str]]:
        return [dict(s) for s in _TOOL_SPECS]

    def dispatch(self, env, action: Action) -> tuple[str, bool] | None:
        if action.tool not in {"move", "smelt", "think", "search", "impossible"}:
            return None
        gym = env.sandbox
        action_str = _action_string(action.tool, action.args)
        obs, reward, terminated, truncated, info = gym.step(action_str)
        env.store["success"] = bool(reward > 0.0)
        env.store["steps"] = int(info.get("steps", 0))
        env.store["reason"] = str(info.get("reason", ""))
        text = obs.get("text", "") if isinstance(obs, dict) else str(obs)
        env.record(f"{action_str}\n{text}")
        return text, bool(terminated or truncated)


def _harvest(env: StateEnv) -> Artifact:
    state = json.dumps(
        {
            "success": bool(env.store.get("success", False)),
            "steps": env.store.get("steps", 0),
            "reason": env.store.get("reason", ""),
        }
    )
    return StateArtifact(state=state, trajectory="\n".join(env.log))


class PlanCraftScorer(Scorer):
    name = "plancraft"

    def score(self, instance: Any, submission: str) -> TaskScore:
        data = json.loads(submission) if submission.strip() else {}
        ok = bool(data.get("success", False))
        return TaskScore(
            instance_id=str(getattr(instance, "id", "")),
            success=1.0 if ok else 0.0,
            detail={"steps": data.get("steps"), "reason": data.get("reason")},
        )


class PlanCraftBenchmark(StateBenchmark):
    name = "plancraft"

    def __init__(self, *, max_steps: int = MAX_STEPS, resolution: str = "high") -> None:
        self.max_steps = max_steps
        self.resolution = resolution

    def instances(self, split: str = "val.small") -> list[Any]:
        from plancraft.simple import get_plancraft_examples

        return list(get_plancraft_examples(split=split))

    def _gym(self, instance: Any):
        from plancraft.environment.actions import (
            ImpossibleActionHandler,
            MoveActionHandler,
            SmeltActionHandler,
        )
        from plancraft.simple import PlancraftGymWrapper

        return PlancraftGymWrapper(
            example=instance,
            actions=[MoveActionHandler(), SmeltActionHandler(), ImpossibleActionHandler()],
            max_steps=self.max_steps,
            resolution=self.resolution,
        )

    def task_prompt(self, instance: Any) -> str:
        gym = self._gym(instance)
        obs, _r, _t, _tr, _info = gym.step("")
        initial = obs.get("text", "") if isinstance(obs, dict) else str(obs)
        return (
            "You are playing a Minecraft-style crafting game. Use the move/smelt tools to "
            "craft the target item into any inventory slot, using think to reason and search "
            "to look up recipes. If the target CANNOT be crafted from this inventory, call "
            f"impossible with a reason. You have at most {self.max_steps} steps.\n\n{initial}"
        )

    def make_env(self, instance: Any) -> StateEnv:
        env = StateEnv(harvest=_harvest)
        env.sandbox = self._gym(instance)
        return env

    def toolset(self) -> ToolSet:
        return PlanCraftToolSet()

    def scorer(self) -> Scorer:
        return PlanCraftScorer()


__all__ = ["PlanCraftBenchmark", "PlanCraftToolSet", "PlanCraftScorer"]
