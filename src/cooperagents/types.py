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
    behavioral_gate: bool = False
    """Q10: use the BEHAVIORAL merge gate (syntax + agents' published checks +
    fail-fast repo tests) instead of syntax-only to decide whether the merged
    tree needs the repair agent — textually-clean 3-way merges can be
    semantically broken (measured: q5g 13.3 vs Q5 15.7)."""
    repair_step_limit: int = 25
    """Step cap for the Q5 merge-repair agent — repair is a focused job and must
    not wander (observed: one uncapped repair pass ran 44 minutes)."""
    repair_integrator: bool = False
    """Q5 (qwen program): in the no-seed mechanical-merge tail, health-check the
    merged tree (AST/build); if the merge demonstrably broke it (conflict
    markers, partial hunks), run ONE repair agent in the merged container with a
    focused reconcile-both-features brief. Repair cost is paid ONLY on
    demonstrated breakage — parallel wall-clock is preserved on clean merges."""
    git_share: bool = False
    """TK-git: a live shared git remote for parallel agents (the coop+git cell
    of the CooperBench team-harness ablation). A bare repository on a shared
    docker volume; a harness thread pushes each agent's working tree to a
    per-agent branch every ~45s (via `git stash create`, leaving the agent's
    tree and history untouched); the poller fetches teammate branches into each
    agent's repo and reports changed files with the exact commands to view or
    take teammate code. System-prompt billing included (TK3 result)."""
    coordinator: bool = False
    """C2: a live monitor thread over the parallel agents — mechanical
    triggers (loop / stall / collision) decide WHEN to intervene, an LLM
    composes the nudge text, injection rides the pushed team_poller channel.
    Max 3 nudges/agent; offline-safe static fallbacks."""
    focused_repair: bool = False
    """R2: the harness gathers the merge-damage EVIDENCE (reject hunks,
    conflict-marker locations, failing check output, failing test tail) and
    puts it in the repair brief — converting the 9B's repair job from
    find-then-fix to fix-only. Same seam philosophy as every mechanism that
    worked: harness does the finding, model does the fixing."""
    repair_time_limit: int | None = None
    """TK9f: wall-clock cap (seconds) for the merge-repair agent. A TIME cap
    bounds the wandering tail (worst observed: 44 min) without starving typical
    repairs the way the 25-step cap did (Q5f regression)."""
    apply_chain_merge: bool = False
    """TK8: bypass the 3-way-first merge and use the pure apply-chain (Q5's
    original, best-measured base 15.7): visible .rej/marker damage routes more
    pairs through repair, which the 9B CAN fix — vs 3-way's silent semantic
    breakage, which it can't (q5g 13.4)."""
    team_roles: bool = False
    """Complete-Team cell (CooperBench team-harness analogue): lead/member role
    asymmetry + shared scratchpad volume at /workspace/shared mounted in every
    agent container (matching CooperBench's ``scratchpad_mount_args``). The
    member exports its diff to the scratchpad; the LEAD applies it and the
    lead's final tree is the team submission — no harness-side mechanical
    merge, repair, or selection. Use with coop_tools + task_board and a
    scratchpad volume in the env factory."""
    claim_mode: bool = False
    """TK6 (allocation axis): shared-objective coop — the harness seeds the
    board with one UNCLAIMED task per feature (full spec in the task), gives
    every agent the whole objective, and agents divide the work themselves via
    task_claim. Tests SELF-partitioning at runtime (vs Round 7's failed
    planner-imposed partitioning). Use with coop_tools + task_board."""
    allow_spawn_tool: bool = False
    """TK7 (allocation axis): spawn_helper as a mini-swe tool — the agent
    recruits a helper in a fresh container on a self-described subtask; the
    helper's diff joins the merge. Capped by max_agents."""
    task_board: bool = False
    """TK4: shared task-board tools (task_create/update/list on the TeamBus)
    exposed to mini-swe with fair system-prompt billing + a status protocol;
    board deltas are pushed via the live-awareness poller."""
    wait_protocol: bool = False
    """TK5: blocking request/response — send_message gains wait:true (reply
    returned in the same tool output, 60s timeout), billed in the system
    prompt for use when an agent needs an agreed name BEFORE proceeding."""
    tool_protocol: bool = False
    """TK3 (toolkit program): make the OFFERED send_message tool fairly
    advertised instead of buried — equal billing in the SYSTEM prompt (with a
    worked example) plus a first-action protocol instruction in the brief. The
    model still chooses whether/how to use it (unlike TK1, where the harness
    performs the exchange itself). Tests whether Q4's zero tool uses were prompt
    salience (bash-mandating system template) or capability."""
    contract_first: bool = False
    """TK1/Q6 (toolkit program): before parallel agents start, the harness makes
    ONE planner call that reads both specs and writes the shared interface
    contract (exact public names/signatures/locations); it is injected into
    every agent's brief as a constraint. Harness-PUSHED coordination — the 9B
    lesson is that offered tools go unused (Q4: 0 send_message calls)."""
    live_awareness: bool = False
    """TK2/Q9 (toolkit program): a harness-side poller injects a one-line
    '[team] agentX is currently editing: ...' note into each agent's context
    whenever a TEAMMATE's changed-file set changes (via the vendored agent's
    team_poller hook). Passive fs_mirror-style awareness: no tool calls, no
    agent initiative, near-zero token cost (emits only on change)."""
    coop_tools: bool = False
    """Q4 (qwen program): run the feature agents CONCURRENTLY (own containers,
    no seeding) with a bus-backed `send_message` tool — the CooperBench
    team-harness shape (explicit runtime coordination) inside the unified
    harness. Incoming messages are drained into the agent's observations each
    step. Use with seed_prior=False; the no-seed integration tail merges."""
    temperature: float | None = None
    """Sampling temperature override for the worker's model calls (None = the
    profile default, e.g. COOPER_TEMPERATURE). Set per-attempt by best-of-N."""
    diversity_temperature: float | None = None
    """Q3 (qwen program): with best_of_n > 1, attempts after the first sample at
    this temperature instead of the pinned one — attempt 1 keeps the greedy
    reproducible floor, later attempts add the candidate DIVERSITY that
    mechanical selection needs (Q2 found temp-0 attempts usually converge)."""
    do_no_harm: bool = False
    """Q1 (qwen program — regression gate at the integration seam): after each
    sequential agent, the harness runs a MECHANICAL health check on the agent's
    tree (language-appropriate compile/syntax check of the repo). If the tree was
    healthy when the agent started and is broken after, the agent's delta is
    DISCARDED and the next agent seeds from the last healthy state. Targets the
    dominant small-model team failure diagnosed on qwen-14: a later agent
    syntactically/behaviorally corrupting code an earlier agent (or solo) had
    working (tiktoken: solo PASS -> team SyntaxError). Purely mechanical — no
    LLM calls, no agent-authored checks (unlike C1); the team layer filters
    agent output using an observed signal, the agent loop is untouched."""
    adaptive: bool = False
    """Runtime-adaptive topology selection (let the work decide sequential vs parallel):
    run all features in PARALLEL from base (own containers, each publishing an invariant
    check), then probe the merge. If the branches merge cleanly (no git-apply conflict and
    all published checks stay green) → KEEP the parallel result (fast path). If they
    collide → the work was coupled → FALL BACK to the sequential build-on-prior handoff for
    the remaining features (safe path), reusing the first branch. The conflict signal is the
    decision, so it never pays the ex-ante write-set-prediction tax (Round 7) — it commits to
    parallel only when parallel demonstrably worked."""
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
