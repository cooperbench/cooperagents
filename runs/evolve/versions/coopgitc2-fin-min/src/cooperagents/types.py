"""Core data types (minimal extraction: only what coopgitc2-fin uses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentResult:
    """The outcome of a single agent's run.

    Mirrors the fields CooperBench's evaluator and analysis expect, so the
    eval adapter can serialize this straight into a ``result.json`` block.
    """

    agent_id: str
    role: str
    status: str  # "submitted" | "error" | "limit"
    patch: str = ""
    cost: float = 0.0
    steps: int = 0
    feature_id: int | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    segments: list[dict[str, Any]] | None = None
    """Full pre-compaction history: each entry {kind, messages} holds the raw
    turns a compaction discarded from ``messages`` (solver segments) or the
    summarizer's output. None when the run never compacted. Without this the
    most productive agents lose most of their trajectory (observed: 611 of
    694 steps invisible)."""

    @property
    def patch_lines(self) -> int:
        return len(self.patch.splitlines())


@dataclass
class SpawnRequest:
    """An agent's runtime request for a helper agent."""

    id: str
    requested_by: str
    task: str
    role: str = "helper"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Assignment:
    """A seed work item: one agent, one task, optionally tied to a feature."""

    agent_id: str
    task: str
    role: str = "member"
    feature_id: int | None = None


@dataclass
class RunResult:
    """The aggregate outcome of a team run."""

    run_id: str
    repo: str
    task_id: int
    features: list[int]
    seeds: dict[str, AgentResult] = field(default_factory=dict)
    helpers: dict[str, AgentResult] = field(default_factory=dict)
    integrated: AgentResult | None = None
    """The single coherent diff from a shared-workspace run (both features in
    one tree).  When set, the eval writer submits it as the team's solution."""
    duration_seconds: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    spawn_metrics: dict[str, Any] = field(default_factory=dict)
    log_dir: str | None = None

    @property
    def total_cost(self) -> float:
        return sum(r.cost for r in {**self.seeds, **self.helpers}.values())

    @property
    def total_steps(self) -> int:
        return sum(r.steps for r in {**self.seeds, **self.helpers}.values())
