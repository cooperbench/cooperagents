"""Run the vendored mini-swe-agent loop as a worker inside the unified harness.

The unified harness owns orchestration (shared workspace, sequential
build-on-each-other, commit-between-agents, integration, metrics); this module
supplies the *worker* — mini-swe-agent's ``DefaultAgent`` tool-calling loop —
so "team vs solo" is measured with the same agent class CooperBench benchmarks
(rather than CooperAgents' minimal built-in loop).

The bridge is :class:`MiniSweEnvAdapter`, which makes the harness's own
:class:`~cooperagents.env.base.Environment` (one shared container) satisfy
mini-swe's ``Environment`` protocol — so every sequential mini-swe agent acts
on the *same* shared git tree.
"""

from __future__ import annotations

import os
import platform
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from cooperagents.env.base import Environment
from cooperagents.types import AgentResult
from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
from cooperagents.vendor.mini_swe.exceptions import LimitsExceeded, Submitted
from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModel

_CONFIG = Path(__file__).resolve().parents[1] / "vendor" / "mini_swe" / "config" / "solo.yaml"
# Exports mini-swe normally injects via `docker run -e` (kill pagers/progress
# bars that would otherwise hang a non-interactive shell).
_ENV_PREFIX = "export PAGER=cat MANPAGER=cat LESS=-R PIP_PROGRESS_BAR=off TQDM_DISABLE=1 2>/dev/null; "

# S7: destructive git on the shared tree would wipe teammates' committed work.
_DESTRUCTIVE_GIT = re.compile(
    r"git\s+(reset\s+--hard|checkout\s+(--\s+)?\.|checkout\s+--\s|clean\s+-[a-z]*[df]|stash)|rm\s+-rf?\s+[^\n]*\.git\b"
)


@lru_cache(maxsize=1)
def _solo_config() -> dict:
    return yaml.safe_load(_CONFIG.read_text())


class MiniSweEnvAdapter:
    """Adapt a CooperAgents :class:`Environment` to mini-swe's env protocol."""

    config: Any = None

    def __init__(
        self,
        env: Environment,
        *,
        timeout: int = 240,
        guard_git: bool = False,
        deadline: float | None = None,
        completion_gate=None,
        gate_max_rejections: int = 3,
    ) -> None:
        self._env = env
        self._timeout = timeout
        self._guard_git = guard_git
        self._deadline = deadline
        # Iteration 6 (gate-at-source): callable(env) -> None (pass) or error
        # text. Run when the agent tries to finish; on failure the finish is
        # rejected and the error injected as the observation, so the agent
        # fixes the problem with its full context and remaining budget
        # (post-hoc repair agents start cold and failed 150-step budgets on
        # errors the original agent could fix in-context).
        self._completion_gate = completion_gate
        self._gate_rejections = 0
        self._gate_max_rejections = gate_max_rejections

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        if self._deadline is not None:
            import time as _time

            if _time.time() > self._deadline:
                # Wall-clock budget (repair tail cap): end the run gracefully;
                # the harness still extracts whatever the agent achieved.
                raise LimitsExceeded(
                    {"role": "exit", "content": "LimitsExceeded", "extra": {"exit_status": "LimitsExceeded", "submission": ""}}
                )
        command = action.get("command", "")
        if self._guard_git and _DESTRUCTIVE_GIT.search(command):
            return {
                "output": "BLOCKED: destructive git on the shared team tree is not allowed "
                "(it would wipe teammates' work). Edit files directly instead.",
                "returncode": 1,
                "exception_info": "",
            }
        res = self._env.execute(_ENV_PREFIX + command, timeout=timeout or self._timeout)
        output = {"output": res.stdout, "returncode": res.exit_code, "exception_info": ""}
        self._check_finished(output)
        return output

    def _check_finished(self, output: dict) -> None:
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and output["returncode"] == 0:
            if self._completion_gate is not None and self._gate_rejections < self._gate_max_rejections:
                err = self._completion_gate(self._env)
                if err:
                    self._gate_rejections += 1
                    left = self._gate_max_rejections - self._gate_rejections
                    output["output"] = (
                        "SUBMISSION REJECTED by the completion gate "
                        f"({left} rejection(s) left before forced accept):\n{err}\n"
                        "Fix the problem, verify, then submit again."
                    )
                    output["returncode"] = 1
                    return
            submission = "".join(lines[1:])
            raise Submitted({"role": "exit", "content": submission, "extra": {"exit_status": "Submitted", "submission": submission}})

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return {**platform.uname()._asdict(), "cwd": self._env.repo_path, **kwargs}

    def serialize(self) -> dict:
        return {}


_TASK_TOOLS = [
    {"type": "function", "function": {
        "name": "task_create", "description": "Post a shared task to the team board (visible to teammates)",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short task title"},
            "note": {"type": "string", "description": "Optional detail"},
        }, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "task_update", "description": "Update a board task's status so teammates see your progress",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "status": {"type": "string", "description": "open | doing | done"},
            "note": {"type": "string"},
        }, "required": ["task_id", "status"]}}},
    {"type": "function", "function": {
        "name": "task_list", "description": "List the shared team board (yours and teammates' tasks with status)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "task_claim", "description": "Claim an unclaimed board task so you own it (first claimer wins)",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
]

SPAWN_TOOL = {
    "type": "function",
    "function": {
        "name": "spawn_helper",
        "description": "Recruit a helper agent in a fresh copy of the repo to work on a subtask in parallel;"
        " its diff is merged with yours at the end",
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "Complete, self-contained subtask brief for the helper"}},
            "required": ["task"],
        },
    },
}

_GIT_SHARE_SYSTEM = (
    "\n\nA shared git remote named 'shared' connects you to teammates working in "
    "parallel copies of this repository. Their in-progress branches are fetched "
    "for you automatically and announced as [git] notes with changed-file lists. "
    "View teammate code: `git diff HEAD...shared/<agent> -- <file>`. Take their "
    "version of a file: `git checkout shared/<agent> -- <file>`. Reusing their "
    "public names and code avoids conflicts when the work is merged."
)

_SPAWN_SYSTEM = (
    "\n\nYou can also recruit help: spawn_helper {task} starts another agent in a "
    "fresh copy of this repository working on the subtask you describe, in "
    "parallel; its changes are merged with everyone's at the end. Write the task "
    "brief as if for a competent colleague with no other context. Use it when "
    "your feature splits into independent pieces."
)

_TASK_BOARD_SYSTEM = (
    "\n\nYou also have shared TASK BOARD tools visible to your teammates: "
    "task_create {title, note?} posts a task; task_update {task_id, status, note?} "
    "sets its status (open|doing|done); task_list {} shows the whole board. "
    "The board is how the team tracks who is doing what."
)

_WAIT_SYSTEM = (
    "\n\nsend_message accepts \"wait\": true — the call blocks up to 60s and the "
    "teammate's reply comes back in the same tool output. Use it when you need "
    "an answer (e.g. an agreed name) BEFORE you can proceed."
)


class TaskBoard:
    """TK4: host-side handlers exposing the TeamBus task board to mini-swe."""

    def __init__(self, bus, agent_id: str) -> None:
        self._bus = bus
        self._id = agent_id

    def handlers(self) -> dict:
        return {"task_create": self._create, "task_update": self._update, "task_list": self._list, "task_claim": self._claim}

    def _claim(self, action: dict) -> dict:
        ok = self._bus.claim_task(str(action.get("task_id", "")), by=self._id)
        return {"output": "claimed — it is yours" if ok else "already taken — pick another (task_list)",
                "returncode": 0 if ok else 1, "exception_info": ""}

    def _create(self, action: dict) -> dict:
        tid = self._bus.create_task(title=str(action.get("title", ""))[:200], created_by=self._id, owner=self._id)
        return {"output": f"task {tid} created on the board", "returncode": 0, "exception_info": ""}

    def _update(self, action: dict) -> dict:
        self._bus.update_task(str(action.get("task_id", "")), by=self._id,
                              status=str(action.get("status", "")) or None, note=str(action.get("note", "")) or None)
        return {"output": "task updated", "returncode": 0, "exception_info": ""}

    def _list(self, action: dict) -> dict:
        rows = self._bus.list_tasks()
        if not rows:
            return {"output": "(board empty)", "returncode": 0, "exception_info": ""}
        lines = [f"[{t.get('id')}] {t.get('status','open')} · {t.get('owner') or 'unclaimed'} · {t.get('title','')}" for t in rows]
        return {"output": "\n".join(lines), "returncode": 0, "exception_info": ""}


class BusComm:
    """Adapts the TeamBus to the vendored mini-swe MessagingConnector shape:
    ``send(recipient, content)`` / ``receive() -> [{"from", "content"}, ...]``.
    Bus receive() drains the inbox, which matches the agent's per-step drain."""

    def __init__(self, bus, agent_id: str) -> None:
        self._bus = bus
        self._id = agent_id

    def send(self, recipient: str, content: str) -> None:
        self._bus.send(sender=self._id, to=recipient, content=content)

    def receive(self) -> list[dict]:
        return self._bus.receive(self._id)

    def send_and_wait(self, recipient: str, content: str, timeout: int = 60) -> list[dict]:
        """TK5: blocking request — send, then poll the inbox for a reply."""
        import time as _time

        self.send(recipient, content)
        deadline = _time.time() + timeout
        got: list[dict] = []
        while _time.time() < deadline:
            got = self.receive()
            if got:
                break
            _time.sleep(2)
        return got


def build_model(
    model_name: str,
    *,
    temperature: float | None = None,
    with_send_message: bool = False,
    with_task_board: bool = False,
    with_spawn: bool = False,
) -> LitellmModel:
    """Build a mini-swe LitellmModel against the configured OpenAI-compatible endpoint."""
    base_url = os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    model_cfg = _solo_config()["model"]
    model_kwargs = dict(model_cfg.get("model_kwargs", {}))
    if base_url:
        model_kwargs["api_base"] = base_url
    if api_key:
        model_kwargs["api_key"] = api_key
    # Pin sampling for measurement runs (unset = provider default). Without
    # this, small-model runs vary wildly between identical configs (observed:
    # team 6/26 vs 11/28 features on the same set), drowning seam deltas.
    if os.getenv("COOPER_TEMPERATURE"):
        model_kwargs["temperature"] = float(os.environ["COOPER_TEMPERATURE"])
    if temperature is not None:  # explicit per-call override (e.g. Q3 diversity)
        model_kwargs["temperature"] = temperature
    # Short request timeout: the litellm/httpx default (~600s) makes an agent
    # hang 10 minutes on a dead pooled connection (socket to a scaled-down
    # serving container that vanished without RST) before retrying onto a
    # healthy one — measured as 8-25min mid-run stalls while the endpoint
    # answered fresh probes in <3s. Healthy calls are 1-3s; productive
    # long generations <=90s; 180s cuts dead-socket waits 3x with margin.
    model_kwargs.setdefault("timeout", 180)
    # litellm needs the openai/ provider prefix to treat it as OpenAI-compatible.
    # HF-style names ("Qwen/Qwen3.5-9B") contain a slash but are not provider
    # prefixes, so only skip the prefix for explicit litellm providers.
    _providers = ("openai/", "azure/", "anthropic/", "hosted_vllm/")
    name = model_name if model_name.startswith(_providers) else f"openai/{model_name}"
    extra_tools = []
    if with_send_message:
        from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import SEND_MESSAGE_TOOL

        extra_tools.append(SEND_MESSAGE_TOOL)
    if with_task_board:
        extra_tools.extend(_TASK_TOOLS)
    if with_spawn:
        extra_tools.append(SPAWN_TOOL)
    extra_tools = extra_tools or None
    return LitellmModel(
        model_name=name,
        model_kwargs=model_kwargs,
        cost_tracking="ignore_errors",
        observation_template=model_cfg["observation_template"],
        format_error_template=model_cfg["format_error_template"],
        extra_tools=extra_tools,
    )


_SEND_MESSAGE_SYSTEM = (
    "\n\nYou also have a second tool: send_message — use it to coordinate with "
    "teammates working on the same repository in parallel. Call it with "
    '{"recipient": "<agent_id>", "content": "<text>"}. Replies and teammate '
    "messages appear in your observations as [Message from <agent_id>]: ... . "
    'Example: send_message with {"recipient": "agent2", "content": "I am adding '
    'class RouteMetrics in metrics.py; reuse that name."} — then continue your '
    "bash work in the same response. Coordinating shared names/files early "
    "prevents merge conflicts later."
)


def run_mini_swe_agent(
    env: Environment,
    *,
    task: str,
    agent_id: str,
    role: str,
    model_name: str,
    step_limit: int,
    cost_limit: float,
    feature_id: int | None = None,
    command_timeout: int = 240,
    guard_git: bool = False,
    temperature: float | None = None,
    comm: BusComm | None = None,
    poller=None,
    tool_protocol: bool = False,
    task_board: TaskBoard | None = None,
    wait_protocol: bool = False,
    spawn_handler=None,
    time_limit_s: int | None = None,
    monitor=None,
    git_share: bool = False,
    completion_gate=None,
) -> AgentResult:
    """Run one mini-swe DefaultAgent on the shared ``env``; return an AgentResult.

    The patch is intentionally left empty — the harness computes the integrated
    diff from the shared tree after all agents run.
    """
    cfg = _solo_config()["agent"]
    model = build_model(
        model_name,
        temperature=temperature,
        with_send_message=comm is not None,
        with_task_board=task_board is not None,
        with_spawn=spawn_handler is not None,
    )
    system_template = cfg["system_template"]
    if tool_protocol and comm is not None:
        system_template = system_template + _SEND_MESSAGE_SYSTEM
    if wait_protocol and comm is not None:
        system_template = system_template + _WAIT_SYSTEM
    if task_board is not None:
        system_template = system_template + _TASK_BOARD_SYSTEM
    if spawn_handler is not None:
        system_template = system_template + _SPAWN_SYSTEM
    if git_share:
        system_template = system_template + _GIT_SHARE_SYSTEM
    import time as _time

    agent = DefaultAgent(
        model,
        MiniSweEnvAdapter(
            env,
            timeout=command_timeout,
            guard_git=guard_git,
            deadline=(_time.time() + time_limit_s) if time_limit_s else None,
            completion_gate=completion_gate,
        ),
        agent_id=agent_id,
        system_template=system_template,
        wall_deadline=(_time.time() + time_limit_s) if time_limit_s else None,
        instance_template=cfg["instance_template"],
        step_limit=step_limit,
        cost_limit=cost_limit,
        compaction_token_trigger=cfg.get("compaction_token_trigger", 28000),
        comm=comm,
    )
    if poller is not None:
        agent.team_poller = poller  # per-step pushed context (TK2/Q9 live awareness)
    if monitor is not None:
        monitor.register(agent_id, agent)  # C2 coordinator watches live trajectory
    handlers = {}
    if task_board is not None:
        handlers.update(task_board.handlers())  # TK4 board tools, host-side
    if spawn_handler is not None:
        handlers["spawn_helper"] = spawn_handler  # TK7 recruit tool
    if handlers:
        agent.tool_handlers = handlers
    try:
        exit_extra = agent.run(task=task)
        status = "submitted" if exit_extra.get("exit_status") == "Submitted" else "limit"
    except Exception as e:  # noqa: BLE001 - surface any failure as an error result
        status = "error"
        return AgentResult(
            agent_id=agent_id,
            role=role,
            status=status,
            cost=agent.cost,
            steps=agent.n_calls,
            feature_id=feature_id,
            messages=agent.messages,
            error=str(e),
        )
    return AgentResult(
        agent_id=agent_id,
        role=role,
        status=status,
        cost=agent.cost,
        steps=agent.n_calls,
        feature_id=feature_id,
        messages=agent.messages,
    )


__all__ = ["MiniSweEnvAdapter", "build_model", "run_mini_swe_agent"]
