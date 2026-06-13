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
from cooperagents.vendor.mini_swe.exceptions import Submitted
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

    def __init__(self, env: Environment, *, timeout: int = 240, guard_git: bool = False) -> None:
        self._env = env
        self._timeout = timeout
        self._guard_git = guard_git

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
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
            submission = "".join(lines[1:])
            raise Submitted({"role": "exit", "content": submission, "extra": {"exit_status": "Submitted", "submission": submission}})

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return {**platform.uname()._asdict(), "cwd": self._env.repo_path, **kwargs}

    def serialize(self) -> dict:
        return {}


def build_model(model_name: str) -> LitellmModel:
    """Build a mini-swe LitellmModel against the configured OpenAI-compatible endpoint."""
    base_url = os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    model_cfg = _solo_config()["model"]
    model_kwargs = dict(model_cfg.get("model_kwargs", {}))
    if base_url:
        model_kwargs["api_base"] = base_url
    if api_key:
        model_kwargs["api_key"] = api_key
    # litellm needs the openai/ provider prefix to treat it as OpenAI-compatible.
    name = model_name if "/" in model_name else f"openai/{model_name}"
    return LitellmModel(
        model_name=name,
        model_kwargs=model_kwargs,
        cost_tracking="ignore_errors",
        observation_template=model_cfg["observation_template"],
        format_error_template=model_cfg["format_error_template"],
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
) -> AgentResult:
    """Run one mini-swe DefaultAgent on the shared ``env``; return an AgentResult.

    The patch is intentionally left empty — the harness computes the integrated
    diff from the shared tree after all agents run.
    """
    cfg = _solo_config()["agent"]
    model = build_model(model_name)
    agent = DefaultAgent(
        model,
        MiniSweEnvAdapter(env, timeout=command_timeout, guard_git=guard_git),
        agent_id=agent_id,
        system_template=cfg["system_template"],
        instance_template=cfg["instance_template"],
        step_limit=step_limit,
        cost_limit=cost_limit,
        compaction_token_trigger=cfg.get("compaction_token_trigger", 28000),
    )
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
