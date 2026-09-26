"""Model-sweep runner: produce the per-configuration success rates that become
the cooperagents box on a benchmark panel.

The Scaling-Agent-Systems paper's Figure 2 plots, per benchmark, one box per
architecture (SAS + MAS topologies); each point in a box is ONE configuration
(architecture x model) averaged over the benchmark's task instances. To place
cooperagents as an additional system, we run its in-place stack in two modes —
``solo`` (SAS baseline) and ``team`` (the full stack: N agents + bus + spawn +
lead-synthesis) — across a configurable set of models, and report each
(model, mode) config's success rate. A box over the models is the cooperagents
entry; the relative solo->team delta is the scorer-version-robust comparison.

This module is backend-agnostic: it drives any :class:`StateBenchmark` through
the unified harness on the state substrate. Models are injected via
``make_llm`` so the sweep is config-driven (Gemini today; add OpenAI/Anthropic
by extending the model list — no code change).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cooperagents.benchmarks.base import StateBenchmark
from cooperagents.env.base import Environment
from cooperagents.eval.scoring import Scorer, TaskScore
from cooperagents.harness import UnifiedHarness
from cooperagents.llm import LLMClient
from cooperagents.reducers import Reducer, best_of_first_nonempty, lead_synthesis
from cooperagents.types import Assignment, TeamSpec

LLMFactory = Callable[[str, str], LLMClient]  # (agent_id, role) -> client


@dataclass
class ConfigResult:
    """One (model, mode) cell: the point that lands in a box."""

    model: str
    mode: str  # "solo" | "team"
    success_rate: float
    n: int
    scores: list[TaskScore]


def run_config(
    benchmark: StateBenchmark,
    instances: Sequence[Any],
    *,
    llm_factory: LLMFactory,
    team_size: int = 1,
    reducer: Reducer | None = None,
    step_limit: int = 30,
    max_agents: int | None = None,
) -> list[TaskScore]:
    """Run one configuration over ``instances`` and return per-instance scores."""
    scorer: Scorer = benchmark.scorer()
    toolset_factory = benchmark.toolset
    scores: list[TaskScore] = []
    for inst in instances:
        assignments = [
            Assignment(
                agent_id=f"agent{i + 1}",
                role="lead" if i == 0 else "member",
                task=benchmark.task_prompt(inst),
            )
            for i in range(max(team_size, 1))
        ]
        spec = TeamSpec(
            run_id=uuid.uuid4().hex[:8],
            repo=benchmark.name,
            task_id=0,
            features=[],
            assignments=assignments,
            artifact_backend="state",
            toolset_factory=toolset_factory,
            reducer=reducer,
            max_agents=max_agents,
        )
        harness = UnifiedHarness(step_limit=step_limit)

        def make_env(_agent_id: str, _inst: Any = inst) -> Environment:
            return benchmark.make_env(_inst)

        res = harness.run(spec, env_factory=make_env, llm_factory=llm_factory)
        submission = res.integrated.artifact.text() if res.integrated and res.integrated.artifact else ""
        scores.append(scorer.score(inst, submission))
    return scores


def success_rate(scores: Sequence[TaskScore]) -> float:
    return sum(s.success for s in scores) / len(scores) if scores else 0.0


def box_sweep(
    benchmark: StateBenchmark,
    *,
    models: Sequence[str],
    make_llm: Callable[[str], LLMClient],
    split: str,
    limit: int | None = None,
    team_size: int = 3,
    step_limit: int = 30,
) -> list[ConfigResult]:
    """Run solo + team for each model over one benchmark split.

    Returns one :class:`ConfigResult` per (model, mode) — the points that form
    the cooperagents ``solo`` and ``team`` boxes on this benchmark's panel.
    """
    instances = list(benchmark.instances(split))
    if limit is not None:
        instances = instances[:limit]
    results: list[ConfigResult] = []
    for model in models:

        def factory(_agent_id: str, _role: str, _m: str = model) -> LLMClient:
            return make_llm(_m)

        solo = run_config(benchmark, instances, llm_factory=factory, team_size=1, step_limit=step_limit)
        results.append(ConfigResult(model, "solo", success_rate(solo), len(solo), solo))

        reducer = lead_synthesis(make_llm(model)) if team_size > 1 else best_of_first_nonempty
        team = run_config(
            benchmark,
            instances,
            llm_factory=factory,
            team_size=team_size,
            reducer=reducer,
            step_limit=step_limit,
            max_agents=team_size,
        )
        results.append(ConfigResult(model, "team", success_rate(team), len(team), team))
    return results


__all__ = ["ConfigResult", "run_config", "success_rate", "box_sweep", "LLMFactory"]
