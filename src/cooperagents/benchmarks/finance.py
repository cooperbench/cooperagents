"""Finance-Agent benchmark — STUB (not yet runnable).

The Finance Agent benchmark answers expert finance questions by fetching
sources via tools (GoogleSearch / EdgarSearch / ParseHTML / RetrieveInformation)
and emitting a final answer + sources, graded by a rubric LLM judge.

Wiring it needs live network + API keys, and the official grader plus the
private/test splits are gated behind the Vals platform (only 50 of 537 questions
are open). So this adapter is a documented placeholder that raises with
instructions. To complete it (see docs/SCALING_BENCHMARKS.md):

1. Provide API keys (LLM provider, Tavily/SerpAPI, SEC EDGAR) and, for the full
   set, Vals platform access. Load them from the environment (never commit).
2. Implement a ``ToolSet`` wrapping the 4 official tools + ``submit_answer``.
3. Implement a ``Scorer``: call the Vals rubric grader where available, else a
   local rubric-judge approximation over the 50 open questions (documented as
   an approximation, not leaderboard-comparable).
4. ``instances`` loads the open 50-question validation split.

Topology fit: Centralized (planner splits into sub-questions -> workers fetch
-> synthesizer composes), the paper's ~+80% regime — but note coordination
raises $/query, which the benchmark penalizes.
"""

from __future__ import annotations

from typing import Any

from cooperagents.benchmarks.base import StateBenchmark
from cooperagents.env.state import StateEnv
from cooperagents.eval.scoring import Scorer
from cooperagents.tools import ToolSet

_NEED = (
    "Finance-Agent is not wired yet: it needs live API keys (LLM, web search, SEC EDGAR) and the "
    "Vals-gated grader (only 50/537 open). See docs/SCALING_BENCHMARKS.md ('Completing Finance-Agent')."
)


class FinanceAgentBenchmark(StateBenchmark):
    name = "finance"

    def instances(self, split: str = "validation") -> list[Any]:
        raise NotImplementedError(_NEED)

    def task_prompt(self, instance: Any) -> str:
        raise NotImplementedError(_NEED)

    def make_env(self, instance: Any) -> StateEnv:
        raise NotImplementedError(_NEED)

    def toolset(self) -> ToolSet:
        raise NotImplementedError(_NEED)

    def scorer(self) -> Scorer:
        raise NotImplementedError(_NEED)


__all__ = ["FinanceAgentBenchmark"]
