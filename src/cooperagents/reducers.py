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

import re
from collections.abc import Callable, Sequence
from typing import Any

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


def lead_selection(llm: LLMClient, *, lead_id: str = "integrator") -> Reducer:
    """Build a reducer where a lead CHOOSES the single best candidate artifact.

    For benchmarks whose deliverable is structured/verifiable (e.g. WorkBench's
    action list) rather than free text, a lead cannot safely compose a NEW answer
    (synthesized prose is ungradeable), and an arbitrary first-non-empty pick is
    not meaningful. Instead the lead reads the task and every candidate solution
    and SELECTS the one it judges most likely to accomplish the task. It sees the
    agents' work and the task — never the grader — so this is self-selection, not
    an oracle; it returns an existing agent's artifact unchanged, so the result
    is always valid for the scorer.
    """

    def reduce(results: Sequence[AgentResult]) -> AgentResult:
        candidates = _nonempty(results)
        if not candidates:
            return best_of_first_nonempty(results)
        if llm is None or len(candidates) == 1:
            return candidates[0]
        task = ""
        for m in candidates[0].messages:
            if m.get("role") == "user":
                task = str(m.get("content", ""))
                break
        listing = "\n\n".join(
            f"[{i}] (agent {r.agent_id}):\n{r.artifact.text() if r.artifact else ''}" for i, r in enumerate(candidates)
        )
        prompt = (
            "You are the team lead. Your teammates each produced a candidate solution to the task "
            "below. Choose the SINGLE candidate most likely to accomplish the task correctly and "
            "completely. Reply with ONLY the candidate number.\n\n"
            f"## Task\n{task}\n\n## Candidates\n{listing}"
        )
        action = llm.decide(agent_id=lead_id, role="lead", messages=[{"role": "user", "content": prompt}], tools=[])
        match = re.search(r"\d+", action.thought or "")
        idx = int(match.group()) if match else 0
        if not (0 <= idx < len(candidates)):
            idx = 0
        return candidates[idx]

    return reduce


def select_by(key: Callable[[AgentResult], Any]) -> Reducer:
    """Reducer that submits the candidate maximizing ``key``.

    For benchmarks whose deliverable is a verifiable outcome rather than free
    text (PlanCraft success, a graded state), selection is the correct combine
    step: run N independent attempts, then submit the best by an intrinsic key
    (e.g. success first, then fewest steps). Ties keep the earliest candidate.
    """

    def reduce(results: Sequence[AgentResult]) -> AgentResult:
        candidates = _nonempty(results) or list(results)
        best = candidates[0]
        best_key = key(best)
        for cand in candidates[1:]:
            ck = key(cand)
            if ck > best_key:
                best, best_key = cand, ck
        return best

    return reduce


__all__ = ["Reducer", "best_of_first_nonempty", "lead_synthesis", "lead_selection", "select_by"]
