"""Built-in deterministic policies — no LLM, no API key required.

:class:`DemoPolicy` is a fixed heuristic that exercises every part of the
unified harness: it edits files (producing a real diff), uses the shared
task list, and — when the lead is allowed — recruits a helper.  It exists so
the harness can be run, demonstrated, and tested end-to-end offline (the
flash validation uses it), and as a worked example of the agent contract a
real :class:`LLMClient` fulfils.
"""

from __future__ import annotations

from typing import Any

from cooperagents.llm import Action


class DemoPolicy:
    """A scripted-by-role policy that completes a small, real workflow."""

    def __init__(self) -> None:
        self._progress: dict[str, int] = {}
        self._plans: dict[str, list[Action]] = {}

    def _plan_for(self, agent_id: str, role: str, can_spawn: bool) -> list[Action]:
        notes = Action(
            tool="write_file",
            args={"path": f"NOTES_{agent_id}.md", "content": f"# {agent_id} ({role})\nWorked the assigned task.\n"},
            thought=f"{agent_id}: recording my work so the diff is non-empty",
        )
        echo = Action(tool="bash", args={"command": "git status --short"}, thought="check working tree")
        if role == "lead":
            plan = [
                Action(tool="task_create", args={"title": "integrate team output"}, thought="open an integration task"),
                notes,
            ]
            if can_spawn:
                plan.append(
                    Action(
                        tool="spawn_helper",
                        args={"task": "Assist the lead: add a HELPER_NOTES.md summary file.", "role": "helper"},
                        thought="recruit a helper for a parallel sub-task",
                    )
                )
            plan += [echo, Action(tool="finish", thought="lead done")]
            return plan
        if role == "helper":
            return [notes, echo, Action(tool="finish", thought="helper done")]
        # member
        return [
            Action(tool="task_list", args={"mine": True}, thought="see what's assigned to me"),
            notes,
            echo,
            Action(tool="finish", thought="member done"),
        ]

    def decide(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Action:
        if agent_id not in self._plans:
            can_spawn = any(t["name"] == "spawn_helper" for t in tools)
            self._plans[agent_id] = self._plan_for(agent_id, role, can_spawn)
            self._progress[agent_id] = 0
        plan = self._plans[agent_id]
        i = self._progress[agent_id]
        if i >= len(plan):
            return Action(tool="finish", thought="plan complete")
        self._progress[agent_id] = i + 1
        return plan[i]


__all__ = ["DemoPolicy"]
