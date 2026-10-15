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

import hashlib
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cooperagents.benchmarks.base import StateBenchmark
from cooperagents.env.base import Environment
from cooperagents.eval.scoring import Scorer, TaskScore
from cooperagents.harness import UnifiedHarness
from cooperagents.llm import LLMClient
from cooperagents.reducers import Reducer
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
    records: list[dict] = field(default_factory=list)  # rich per-task records


def _log(msg: str) -> None:
    print(msg, flush=True, file=sys.stderr)


def _team_totals(res: Any) -> tuple[int, float]:
    """Total steps and cost across every agent in the run (seeds + helpers),
    so a team task's captured compute is the whole team's, not just the agent
    whose artifact the reducer selected."""
    agents = list(getattr(res, "seeds", {}).values()) + list(getattr(res, "helpers", {}).values())
    steps = sum(int(getattr(a, "steps", 0) or 0) for a in agents)
    cost = sum(float(getattr(a, "cost", 0.0) or 0.0) for a in agents)
    return steps, cost


def _trajectory(res: Any) -> str:
    art = res.integrated.artifact if res.integrated and res.integrated.artifact else None
    return getattr(art, "trajectory", "") or "" if art is not None else ""


def _agent_breakdown(res: Any) -> list[dict]:
    """Per-agent capture for every agent in the run (seeds + helpers).

    Team mode submits a single reducer-selected artifact, so the top-level
    record reflects only that one agent. This breakdown keeps each agent's own
    steps, status, submission size, and a non-empty-trajectory flag, so a
    spent/mutated instance that produced a steps=0 episode stays visible even
    when the reducer selected it over a sibling that genuinely worked.
    """
    out: list[dict] = []
    seeds = getattr(res, "seeds", {}) or {}
    helpers = getattr(res, "helpers", {}) or {}
    for aid, a in list(seeds.items()) + list(helpers.items()):
        art = getattr(a, "artifact", None)
        text = art.text() if art is not None and hasattr(art, "text") else ""
        traj = getattr(art, "trajectory", "") or "" if art is not None else ""
        out.append(
            {
                "agent_id": aid,
                "role": getattr(a, "role", ""),
                "status": getattr(a, "status", ""),
                "steps": int(getattr(a, "steps", 0) or 0),
                "cost": round(float(getattr(a, "cost", 0.0) or 0.0), 6),
                "submission_chars": len(text),
                "traj_lines": len([x for x in traj.splitlines() if x.strip()]),
                "trajectory": traj,  # full per-agent log; the sink dumps it to a side file
            }
        )
    return out


def _fingerprint(task: str) -> dict:
    """Initial-state fingerprint of a task instance. The reused-instance bug
    manifested as a mutated/empty initial state: the same instance_id yielding a
    different (shorter) initial prompt across arms. Capturing a hash + length of
    the initial task prompt makes that divergence directly detectable."""
    return {"prompt_chars": len(task), "prompt_sha1": hashlib.sha1(task.encode()).hexdigest()[:12]}


def _cache_key(model: str, mode: str, instance_id: str) -> str:
    return f"{model}\x1f{mode}\x1f{instance_id}"


def run_config(
    benchmark: StateBenchmark,
    instances: Sequence[Any],
    *,
    llm_factory: LLMFactory,
    team_size: int = 1,
    reducer: Reducer | None = None,
    step_limit: int = 30,
    max_agents: int | None = None,
    model: str = "",
    mode: str = "",
    record_fn: Callable[[dict], None] | None = None,
    records_out: list[dict] | None = None,
    completed: dict[str, dict] | None = None,
) -> list[TaskScore]:
    """Run one configuration over ``instances`` and return per-instance scores.

    ``record_fn`` is called once per FRESHLY-RUN instance with a rich per-task
    record (success, steps, cost, duration, per-agent breakdown, scorer detail,
    submission excerpt, trajectory). It is the durability seam: a sink that
    appends each record to a file makes progress survive a crash or sleep.

    ``records_out``, when given, collects EVERY record (both freshly-run and
    resumed-from-cache) for the final summary.

    ``completed`` enables true resume: a mapping ``_cache_key(model, mode,
    instance_id) -> record`` of tasks already finished in a prior run. A task
    found there is NOT re-run — its stored result is reused verbatim. Because a
    task is executed at most once ever and its single authoritative outcome is
    reused on resume, the aggregate success rate is identical to an
    uninterrupted run: resume cannot shift any metric.
    """
    scorer: Scorer = benchmark.scorer()
    toolset_factory = benchmark.toolset
    completed = completed or {}
    scores: list[TaskScore] = []
    n = len(instances)
    for idx, inst in enumerate(instances):
        inst_id = str(getattr(inst, "id", getattr(inst, "instance_id", str(idx))))

        cached = completed.get(_cache_key(model, mode, inst_id))
        if cached is not None:
            score = TaskScore(instance_id=inst_id, success=float(cached.get("success", 0.0)), detail=cached.get("detail", {}) or {})
            scores.append(score)
            if records_out is not None:
                records_out.append(cached)
            passed = len([s for s in scores if s.success])
            _log(f"  [{idx + 1}/{n}] {inst_id}  {'PASS' if score.success else 'FAIL'}  CACHED  running={passed}/{idx + 1}")
            continue

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
            env = benchmark.make_env(_inst)
            # Tag the env with its agent id so cooperative toolsets can attribute
            # shared-notebook writes per agent. Benchmarks that ignore it are
            # unaffected.
            try:
                env.agent_id = _agent_id  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            return env

        res = harness.run(spec, env_factory=make_env, llm_factory=llm_factory)
        submission = res.integrated.artifact.text() if res.integrated and res.integrated.artifact else ""
        score = scorer.score(inst, submission)
        scores.append(score)

        steps, cost = _team_totals(res)
        duration = round(float(getattr(res, "duration_seconds", 0.0) or 0.0), 2)
        record = {
            "benchmark": benchmark.name,
            "model": model,
            "mode": mode,
            "instance_id": inst_id,
            "index": idx,
            "success": score.success,
            "passed": bool(score.passed),
            "n_agents": max(team_size, 1),
            "step_limit": step_limit,
            "messages": int((getattr(res, "metrics", {}) or {}).get("messages_total", 0)),
            "steps": steps,
            "cost": round(cost, 6),
            "duration_s": duration,
            "detail": score.detail,
            "fingerprint": _fingerprint(assignments[0].task),
            "agents": _agent_breakdown(res),
            "submission_chars": len(submission),
            "submission_excerpt": submission[:300],
            "trajectory": _trajectory(res),
        }
        if record_fn is not None:
            record_fn(record)
        if records_out is not None:
            records_out.append(record)

        passed = len([s for s in scores if s.success])
        _log(
            f"  [{idx + 1}/{n}] {inst_id}  {'PASS' if score.success else 'FAIL'}  "
            f"steps={steps} cost=${cost:.4f} {duration:.1f}s  running={passed}/{idx + 1}"
        )
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
    iso_compute: bool = True,
    record_fn: Callable[[dict], None] | None = None,
    completed: dict[str, dict] | None = None,
) -> list[ConfigResult]:
    """Run solo + team (+ optional iso-compute solo) for each model over one split.

    Emits one :class:`ConfigResult` per (model, mode) — the points that form the
    cooperagents boxes on this benchmark's panel:

      * ``solo`` — single agent, the SAS baseline.
      * ``solo-iso`` — single agent granted team_size x the step budget, so the
        team gain can be read net of raw compute (compute-matched delta). Emitted
        only when ``iso_compute`` and ``team_size > 1``.
      * ``team`` — team_size agents combined by the benchmark's own reducer
        (``benchmark.team_reducer``): lead-synthesis for text answers, selection
        for verifiable-outcome benchmarks such as PlanCraft.

    ``record_fn`` is forwarded to :func:`run_config` for durable persistence of
    freshly-run tasks; every record (fresh or resumed) is also collected onto
    its :class:`ConfigResult`. ``completed`` is the resume cache (see
    :func:`run_config`).
    """
    def _load() -> list[Any]:
        insts = list(benchmark.instances(split))
        return insts[:limit] if limit is not None else insts

    def _arm(mode: str, model: str, factory: LLMFactory, instances: list[Any], **kw: Any) -> ConfigResult:
        recs: list[dict] = []
        scores = run_config(
            benchmark,
            instances,
            llm_factory=factory,
            model=model,
            mode=mode,
            record_fn=record_fn,
            records_out=recs,
            completed=completed,
            **kw,
        )
        cr = ConfigResult(model, mode, success_rate(scores), len(scores), scores, recs)
        _log(f"    {mode} done: {cr.success_rate:.3f}")
        return cr

    results: list[ConfigResult] = []
    for model in models:

        def factory(_agent_id: str, _role: str, _m: str = model) -> LLMClient:
            return make_llm(_m)

        instances = _load()
        _log(f"\n=== {model} | solo ({len(instances)} instances, {step_limit} steps) ===")
        results.append(_arm("solo", model, factory, instances, team_size=1, step_limit=step_limit))

        if iso_compute and team_size > 1:
            instances = _load()
            _log(f"\n=== {model} | solo-iso ({len(instances)} instances, {step_limit * team_size} steps) ===")
            results.append(_arm("solo-iso", model, factory, instances, team_size=1, step_limit=step_limit * team_size))

        instances = _load()
        reducer = benchmark.team_reducer(make_llm(model)) if team_size > 1 else None
        _log(f"\n=== {model} | team ({len(instances)} instances, {team_size} agents, {step_limit} steps) ===")
        results.append(
            _arm("team", model, factory, instances, team_size=team_size, reducer=reducer, step_limit=step_limit, max_agents=team_size)
        )
    return results


__all__ = ["ConfigResult", "run_config", "success_rate", "box_sweep", "LLMFactory"]
