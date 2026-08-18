"""CooperAgents — a unified, self-evolving harness for a team of LLM agents.

Stage 1 unifies what used to be two stacked layers (a team harness wrapping
an opaque agent harness) into one orchestrator whose agents and supervisor
share a single coordination bus.  The team can grow itself at runtime via
``spawn_helper``.  CooperBench is used only to supply tasks and to score
results.
"""

from __future__ import annotations

from cooperagents.agent import Agent
from cooperagents.bus import InMemoryBus, TeamBus, make_bus
from cooperagents.env import Environment, LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.llm import Action, CallbackLLM, LiteLLMClient, LLMClient, ScriptedLLM
from cooperagents.types import AgentResult, Assignment, RunResult, SpawnRequest, TeamSpec

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "UnifiedHarness",
    "TeamBus",
    "InMemoryBus",
    "make_bus",
    "Environment",
    "LocalEnv",
    "LLMClient",
    "ScriptedLLM",
    "CallbackLLM",
    "LiteLLMClient",
    "Action",
    "AgentResult",
    "Assignment",
    "RunResult",
    "SpawnRequest",
    "TeamSpec",
]
