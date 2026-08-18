"""The unified agent.

This is the single place the "team harness" and the "agent harness" become
one: the agent's tool catalog mixes ordinary execution tools (``bash``,
``read_file``, ``write_file``) with first-class *team* tools (``send_message``,
``spawn_helper``, the ``task_*`` verbs) that act on the shared :class:`TeamBus`.
The agent doesn't know whether it's "solo" or part of a team — coordination
is just more tools.  That is what lets one agent recruit another mid-run.

The loop itself is deliberately small: poll inbox → ask the policy for one
:class:`Action` → dispatch it → record the observation, until the policy
calls ``finish`` or a budget is hit.  The final patch is whatever the
environment's ``git_diff`` reports.
"""

from __future__ import annotations

from typing import Any

from cooperagents.bus.base import TeamBus
from cooperagents.env.base import Environment
from cooperagents.llm import Action, LLMClient
from cooperagents.types import AgentResult

# Tool catalog handed to the policy each step.  ``spawn_helper`` is appended
# at runtime only when the agent is allowed to recruit.
_BASE_TOOLS: list[dict[str, str]] = [
    {"name": "bash", "description": "Run a shell command in the repo. args: {command}"},
    {"name": "read_file", "description": "Read a file relative to the repo. args: {path}"},
    {"name": "write_file", "description": "Write a file relative to the repo. args: {path, content}"},
    {"name": "send_message", "description": "Message a teammate. args: {to, content}"},
    {"name": "broadcast", "description": "Message all teammates. args: {content}"},
    {"name": "task_create", "description": "Add a shared task. args: {title, owner?}"},
    {"name": "task_claim", "description": "Claim a shared task. args: {task_id}"},
    {"name": "task_update", "description": "Update a task. args: {task_id, status, note?}"},
    {"name": "task_list", "description": "List shared tasks. args: {mine?, open?}"},
    {"name": "finish", "description": "Stop working; your current diff is submitted. args: {}"},
]

_SPAWN_TOOL = {
    "name": "spawn_helper",
    "description": "Ask the harness to launch a helper agent on a sub-task. args: {task, role?}",
}


class Agent:
    """One team member (or helper) running a tool-calling loop."""

    def __init__(
        self,
        *,
        agent_id: str,
        role: str,
        task: str,
        env: Environment,
        llm: LLMClient,
        bus: TeamBus,
        feature_id: int | None = None,
        allow_spawn: bool = False,
        step_limit: int = 40,
        cost_limit: float = 5.0,
        command_timeout: int = 60,
        max_obs_chars: int = 6000,
        keep_recent: int = 24,
        max_actions_per_turn: int = 12,
        finish_nudges: int = 3,
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.task = task
        self.env = env
        self.llm = llm
        self.bus = bus
        self.feature_id = feature_id
        self.allow_spawn = allow_spawn
        self.step_limit = step_limit
        self.cost_limit = cost_limit
        self.command_timeout = command_timeout
        self.max_obs_chars = max_obs_chars
        self.keep_recent = keep_recent
        self.max_actions_per_turn = max_actions_per_turn
        self.finish_nudges = finish_nudges
        self.messages: list[dict[str, Any]] = []
        self.cost = 0.0
        self.steps = 0
        self.spawn_requests: list[str] = []

    @property
    def tools(self) -> list[dict[str, str]]:
        return [*_BASE_TOOLS, _SPAWN_TOOL] if self.allow_spawn else list(_BASE_TOOLS)

    def _add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def _context(self) -> list[dict[str, Any]]:
        """Bounded view of the transcript sent to the policy.

        Always keeps the system message + the original task (messages[:2]),
        then the most recent ``keep_recent`` messages — so per-call context
        (and cost) stays roughly constant on long episodes instead of growing
        every step.  The full transcript is still retained on ``self.messages``
        for the trajectory record.
        """
        if len(self.messages) <= self.keep_recent + 2:
            return self.messages
        head = self.messages[:2]
        recent = self.messages[-self.keep_recent :]
        elided = {"role": "user", "content": f"[... {len(self.messages) - len(head) - len(recent)} earlier steps elided ...]"}
        return [*head, elided, *recent]

    def _drain_inbox(self) -> None:
        for msg in self.bus.receive(self.agent_id):
            self._add("user", f"[message from {msg['from']}] {msg['content']}")

    def _dispatch(self, action: Action) -> tuple[str, bool]:
        """Execute one action; return (observation, finished)."""
        tool, args = action.tool, action.args
        if tool == "finish":
            return "finished", True
        if tool == "bash":
            res = self.env.execute(str(args.get("command", "")), timeout=self.command_timeout)
            return f"(exit {res.exit_code})\n{res.stdout}", False
        if tool == "read_file":
            return self.env.read_file(str(args.get("path", ""))), False
        if tool == "write_file":
            self.env.write_file(str(args.get("path", "")), str(args.get("content", "")))
            return f"wrote {args.get('path')}", False
        if tool == "send_message":
            self.bus.send(sender=self.agent_id, to=str(args.get("to", "")), content=str(args.get("content", "")))
            return f"sent to {args.get('to')}", False
        if tool == "broadcast":
            self.bus.broadcast(sender=self.agent_id, content=str(args.get("content", "")))
            return "broadcast sent", False
        if tool == "task_create":
            tid = self.bus.create_task(title=str(args.get("title", "")), created_by=self.agent_id, owner=str(args.get("owner", "")))
            return f"created task {tid}", False
        if tool == "task_claim":
            ok = self.bus.claim_task(str(args.get("task_id", "")), by=self.agent_id)
            return ("claimed" if ok else "claim failed (owned by another)"), False
        if tool == "task_update":
            self.bus.update_task(
                str(args.get("task_id", "")),
                by=self.agent_id,
                status=args.get("status"),
                note=args.get("note"),
            )
            return "task updated", False
        if tool == "task_list":
            owner = self.agent_id if args.get("mine") else None
            status = "open" if args.get("open") else None
            tasks = self.bus.list_tasks(owner=owner, status=status)
            return "\n".join(f"{t['id']} [{t['status']}] owner={t['owner'] or '?'}: {t['title']}" for t in tasks) or "(no tasks)", False
        if tool == "spawn_helper" and self.allow_spawn:
            rid = self.bus.spawn_request(requested_by=self.agent_id, task=str(args.get("task", "")), role=str(args.get("role", "helper")))
            self.spawn_requests.append(rid)
            return f"spawn requested ({rid})", False
        return f"unknown or disallowed tool: {tool}", False

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_obs_chars:
            return text
        half = self.max_obs_chars // 2
        cut = len(text) - self.max_obs_chars
        return f"{text[:half]}\n...[truncated {cut} chars]...\n{text[-half:]}"

    def _next_actions(self) -> list[Action]:
        """One LLM call → one or more actions (batched if the client supports it)."""
        ctx = self._context()
        batch = getattr(self.llm, "decide_batch", None)
        if callable(batch):
            actions = batch(agent_id=self.agent_id, role=self.role, messages=ctx, tools=self.tools)
        else:
            actions = [self.llm.decide(agent_id=self.agent_id, role=self.role, messages=ctx, tools=self.tools)]
        return actions[: self.max_actions_per_turn]

    def run(self) -> AgentResult:
        self.bus.register_agent(self.agent_id, role=self.role)
        self._add("system", f"You are {self.agent_id} (role: {self.role}).")
        self._add("user", self.task)

        status = "submitted"
        error: str | None = None
        try:
            turns = 0
            finished = False
            while not finished:
                if turns >= self.step_limit or self.cost >= self.cost_limit:
                    status = "limit"
                    break
                turns += 1
                self._drain_inbox()
                # Execute every action the model emitted this turn, in order, so
                # planned write_file/bash steps actually run (the model assumes
                # they did) instead of only the first being applied.
                for action in self._next_actions():
                    self.steps += 1
                    self.cost += action.cost
                    if action.thought:
                        self._add("assistant", action.thought)
                    observation, finished = self._dispatch(action)
                    self._add("user", self._truncate(observation))
                    if finished:
                        break
                # Veto a finish with an empty diff: the model often "plans" an
                # edit it never executed and quits with nothing.  Nudge it back
                # to work a few times before accepting an empty submission.
                if finished and self.finish_nudges > 0 and not self.env.git_diff().strip():
                    self.finish_nudges -= 1
                    finished = False
                    self._add(
                        "user",
                        "Your git diff is EMPTY — you have not written any code yet. Implement the "
                        "feature now with write_file/bash edits (do NOT create or edit test files), "
                        "then finish.",
                    )
        except Exception as e:  # noqa: BLE001 - surface any failure as a result
            status = "error"
            error = str(e)

        return AgentResult(
            agent_id=self.agent_id,
            role=self.role,
            status=status,
            patch=self.env.git_diff(),
            cost=self.cost,
            steps=self.steps,
            feature_id=self.feature_id,
            messages=self.messages,
            error=error,
        )


__all__ = ["Agent"]
