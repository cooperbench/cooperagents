"""Core data types shared across the unified harness.

These are intentionally small, frozen-ish dataclasses so the orchestrator,
agents, environments, and the eval adapter all speak the same vocabulary
without importing each other.
"""

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
class SubTask:
    """A node in a decomposition DAG (G1/G2).

    The planner re-cuts the objective into subtasks chosen to MINIMIZE coupling:
    work that touches the same code is merged into one subtask (one agent);
    independent work is split. ``depends_on`` are the subtask ids whose output
    this subtask needs — its container is seeded only with those (and their
    transitive ancestors'), never the whole shared state. Independent subtasks
    (no unfinished deps) run in parallel, each in its own container.
    """

    id: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    features: list[int] = field(default_factory=list)
    owns: list[str] = field(default_factory=list)
    """The write-set this subtask is allowed to edit — file + region/symbol
    descriptors (e.g. "src/click/termui.py: prompt(), confirm()"). The key to
    re-dividing coupled work: two subtasks may edit the SAME file as long as
    their ``owns`` regions are disjoint (non-overlapping hunks merge cleanly).
    Injected into the agent's prompt as a hard ownership boundary so independent
    subtasks produce a conflict-free merge."""


@dataclass
class TeamSpec:
    """Everything the harness needs to run one team on one problem.

    Two shapes are supported, matching CLAUDE.md Stage 1:

      * **N tasks for N agents** — pass several ``assignments``, one per
        agent (the generalization of CooperBench's coop/team).
      * **One task for the whole team** — pass a single ``objective`` and
        ``team_size``; the harness seeds a lead + members who all share
        that objective and decompose it via the task list.

    In both shapes the team can grow itself at runtime: any agent may call
    the ``spawn_helper`` tool, and the supervisor launches a helper up to
    ``max_agents``.
    """

    run_id: str
    repo: str
    task_id: int
    features: list[int]
    assignments: list[Assignment] = field(default_factory=list)
    objective: str | None = None
    team_size: int = 1
    max_agents: int | None = None
    allow_spawn: bool = True
    model: str = "scripted"
    worker: str = "builtin"
    """Agent loop to run as the worker: "builtin" (CooperAgents' own loop) or
    "mini_swe" (the vendored mini-swe-agent loop)."""
    shared_workspace: bool = False
    """When True, the team shares ONE git working tree and runs sequentially,
    each agent building on the others' work — the 'shared code substrate' that
    CooperBench's ablation found to be the dominant source of multi-agent value.
    Produces one coherent integrated diff instead of conflicting per-agent ones."""
    verify_fix: bool = False
    """S5 seam improvement: after the feature agents run, the team reuses the
    agent loop once more as an *integration/repair* pass on the shared tree —
    "make BOTH features work and the project build" — targeting the build-error
    and incomplete-integration failures that survive the shared workspace."""
    spec_fidelity: bool = False
    """S8 seam improvement: the team injects a spec-fidelity instruction into
    every agent's prompt (mirror the spec's exact public names/signatures, since
    the hidden grader tests reference them) — targets API/spec-mismatch misses."""
    teammate_context: bool = False
    """S2 seam improvement: each agent (after the first) is shown the actual diff
    teammates have already written in the shared tree, so it reuses their public
    names/signatures and integrates rather than duplicating/conflicting."""
    guard_git: bool = False
    """S7 seam improvement: the team blocks destructive git commands
    (reset --hard / checkout -- / clean / stash / rm .git) on the shared tree so
    one agent can't wipe teammates' work mid-run."""
    completeness_review: bool = False
    """T3 (analyst-generated) seam: after the team, a reviewer pass (own
    container) enumerates EACH feature and verifies it is fully implemented,
    filling any gaps — targets the dominant 'incomplete implementation' failure
    (a whole feature silently omitted), distinct from S5's build-only repair."""
    tdd_preamble: bool = False
    """T2 (analyst-generated) seam: prepend an in-loop instruction telling each
    agent to FIRST derive concrete acceptance criteria from the spec and write a
    throwaway local check (python -c / scratch script, never a graded test file)
    it runs to verify each criterion, then implement until those checks pass.
    Distinct from T3/S5: changes the agent's OWN loop, not an extra post-hoc pass
    (post-hoc passes washed at n=20 — they regress as much as they fix)."""
    mine_conventions: bool = False
    """T4 (analyst-generated) seam: prepend an in-loop instruction telling each
    agent to FIRST inspect the repo's existing tests/usages/conventions for the
    area it will touch (grep for the public symbols, read neighbouring code) and
    mirror them, before editing — targets incomplete/convention-mismatch misses.
    Like T2, an in-loop prompt change, not an extra pass."""
    preserve_invariants: bool = False
    """C1 (coordination under interdependence): each sequential agent PUBLISHES a
    runnable regression check for its own feature into the shared tree
    (`.cb_checks/`, stripped before grading); later agents must keep ALL prior
    checks green while adding their feature. Targets the dominant coupled failure
    diagnosed on the coupled set — a later agent silently BREAKING an earlier
    teammate's feature while editing shared code (eval pattern f1=F, f2=P).
    Coordination via a shared invariant communicated agent→agent at runtime,
    rather than partitioning the work up front (which can't be done — see
    decompose / Round 7)."""
    decompose: bool = False
    """G1+G2+G3 (separability-aware orchestration): instead of a fixed
    one-agent-per-feature map, a PLANNER re-cuts the objective into a dependency
    DAG of subtasks chosen to maximize independence (merge coupled work, split
    independent work). The harness then runs that DAG — independent subtasks in
    PARALLEL (own containers), each seeded only with its transitive ancestors'
    deltas (not the whole shared state) — and merges the branch deltas. The agent
    count is whatever the planner chooses (≤ max_agents), so granularity/spawning
    follows the decomposition. Round 6 showed multi-agent value = separability;
    this attacks separability at the source (how the work is cut), not coordination."""
    best_of_n: int = 1
    """T6 (best-of-N self-selection) seam: run the WHOLE isolated team this many
    times (each attempt in its own fresh containers — the own-container constraint
    is preserved per attempt), then a self-available selector (LLM judge, no access
    to the hidden grader) picks the best candidate integrated diff to submit.
    A distinct mechanism family from coordination prompts, post-hoc passes, and
    in-loop preambles: it exploits run-to-run variance via selection."""
    seed_prior: bool = True
    """When True (default), each isolated agent's container is seeded with
    teammates' cumulative committed diff (builds on prior work). When False,
    agents work fully INDEPENDENTLY (own container, base only) and an integrator
    (or a mechanical merge) combines their patches — the classic isolated-coop
    design. Both satisfy the own-container hard constraint."""

    def seed_count(self) -> int:
        return len(self.assignments) if self.assignments else self.team_size


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
