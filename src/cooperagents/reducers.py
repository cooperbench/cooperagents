"""Reducers: the non-code aggregation step that replaces git integration.

On the git substrate the team's work is combined by ``git apply`` / 3-way
merge.  On the state substrate there is no diff to merge, so a reducer takes
the team's per-agent :class:`~cooperagents.types.AgentResult` list (each with a
:class:`~cooperagents.env.artifact.StateArtifact`) and reduces it to the single
result the team submits.

Two families, mirroring cooperagents' existing selection seams:

  * :func:`best_of_first_nonempty` — deterministic, dependency-free selection
    (first non-empty answer wins). Used by tests and as an offline default.
  * :func:`lead_synthesis` — an LLM reads every agent's answer and writes one
    synthesized final answer (the "full in-place stack" default). This reducer
    IS part of what the team number measures, so it is an explicit, swappable
    component, not a hidden default.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from cooperagents.env.artifact import StateArtifact
from cooperagents.llm import LLMClient
from cooperagents.types import AgentResult

Reducer = Callable[[Sequence[AgentResult]], AgentResult]


def _nonempty(results: Sequence[AgentResult]) -> list[AgentResult]:
    return [r for r in results if r.artifact is not None and not r.artifact.is_empty()]


def best_of_first_nonempty(results: Sequence[AgentResult]) -> AgentResult:
    """Deterministic reducer: submit the first agent with a non-empty artifact."""
    candidates = _nonempty(results) or list(results)
    return candidates[0]


def lead_synthesis(llm: LLMClient, *, lead_id: str = "integrator") -> Reducer:
    """Build a reducer that asks ``llm`` to synthesize one final answer.

    The lead sees only the agents' answers (never the grader), so this is
    genuine self-synthesis. Returns a synthetic integrator ``AgentResult``
    carrying the synthesized :class:`StateArtifact`.
    """

    def reduce(results: Sequence[AgentResult]) -> AgentResult:
        candidates = _nonempty(results)
        if not candidates:
            return best_of_first_nonempty(results)
        if len(candidates) == 1:
            return candidates[0]
        answers = "\n\n".join(f"[{r.agent_id}] {r.artifact.text() if r.artifact else ''}" for r in candidates)
        prompt = (
            "You are the team lead. Your teammates each produced a candidate answer "
            "to the same task below. Synthesize the single best final answer, "
            "reconciling and correcting them. Reply with ONLY the final answer.\n\n"
            f"{answers}"
        )
        action = llm.decide(
            agent_id=lead_id,
            role="lead",
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        final = (action.thought or "").strip()
        cost = sum(r.cost for r in results) + action.cost
        steps = sum(r.steps for r in results)
        return AgentResult(
            agent_id=lead_id,
            role="lead",
            status="submitted",
            artifact=StateArtifact(answer=final),
            cost=cost,
            steps=steps,
        )

    return reduce


__all__ = ["Reducer", "best_of_first_nonempty", "lead_synthesis"]
