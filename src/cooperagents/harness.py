"""The unified harness orchestrator.

**Hard constraint: every agent runs in its OWN container/environment** — agents
never share a live workspace. The coordinated team path (``_run_isolated``)
seeds each fresh container with teammates' cumulative diff so an agent still
builds on prior work, but execution is always isolated per agent.

One ``run`` call drives a whole team on one problem.  Unlike a two-level
design (a team harness that wraps an opaque agent harness), the orchestrator
and every agent share one :class:`TeamBus`, so the team can reshape itself
while it works:

  1. **Seed** the team from the spec — one agent per assignment
     (``N tasks for N agents``) or a lead + members on a shared objective
     (``one task for the whole team``).
  2. Run every seed agent concurrently, each in its own environment.
  3. A **supervisor** drains the spawn queue: when an agent calls
     ``spawn_helper``, it launches a fresh helper agent on that sub-task —
     up to ``max_agents`` — which itself may recruit further help.
  4. When the pool goes idle, harvest patches + coordination/spawn metrics.

The result is eval-ready: per-feature seed patches are what CooperBench
scores; helper output reaches the score through the agent that integrates
it (the conventional lead-merges-the-team pattern).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from cooperagents.agent import Agent
from cooperagents.bus.base import TeamBus
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import Environment
from cooperagents.llm import LLMClient
from cooperagents.metrics import coordination_metrics, spawn_metrics
from cooperagents.patching import strip_test_sections
from cooperagents.planner import ancestors, plan_decomposition, topo_levels
from cooperagents.types import AgentResult, Assignment, RunResult, SubTask, TeamSpec

Planner = Callable[[list[tuple[int, str]], "str | None"], "tuple[list[SubTask], str]"]


def _seed_patch(env: Environment, patch: str) -> None:
    """Apply a teammate/ancestor delta into a fresh container and commit it, so
    the agent starts from a clean coherent base (a dirty tree confuses it)."""
    if not patch.strip():
        return
    env.write_file(".cb_prior.patch", patch)
    env.execute(
        "git apply --whitespace=nowarn .cb_prior.patch 2>/dev/null "
        "|| git apply --3way .cb_prior.patch 2>/dev/null "
        "|| git apply --reject .cb_prior.patch 2>/dev/null || true"
    )
    env.execute(
        "rm -f .cb_prior.patch && git add -A && "
        "git -c user.email=team@cooperagents.local -c user.name=cooperagents commit -q -m 'seed' || true"
    )

EnvFactory = Callable[[str], Environment]
LLMFactory = Callable[[str, str], LLMClient]


_SPEC_FIDELITY = (
    "IMPORTANT — API fidelity: a hidden automated test suite grades your work by "
    "referencing the EXACT public names and signatures the spec describes "
    "(types, functions, methods, struct fields, parameters, constants). Implement "
    "them verbatim as named or strongly implied by the spec — do not rename, "
    "abbreviate, pluralize differently, or invent API surface. Match the spec's "
    "wording when choosing identifiers.\n\n"
)


_TDD_PREAMBLE = (
    "WORKFLOW — verify as you go: BEFORE writing code, derive from the spec a short "
    "checklist of concrete acceptance criteria (exact public API names, expected "
    "behaviors, edge cases). For each, write a THROWAWAY local check you can run "
    "(a `python -c ...` one-liner or a scratch script OUTSIDE any tests/ directory) "
    "— never add or edit files in the project's real test suite. Implement until "
    "every check passes, then DELETE your scratch checks before you finish. Do not "
    "submit until your own checks confirm each acceptance criterion holds.\n\n"
)


_MINE_CONVENTIONS = (
    "WORKFLOW — mine conventions first: BEFORE editing, inspect how this repo "
    "already does things in the area you will touch. Grep for the public symbols "
    "the spec mentions and read the neighbouring code, existing tests, and similar "
    "features to learn the naming, signatures, error handling, and patterns in use. "
    "Mirror those existing conventions in your implementation rather than inventing "
    "new ones, so your feature integrates cleanly with what is already there.\n\n"
)


def _completeness_task(assignments: list[Assignment]) -> str:
    """T3 reviewer brief — verify EACH feature is fully implemented; fill gaps."""
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(f"### Feature {i + 1}\n{s}" for i, s in enumerate(specs))
    return (
        "You are a completeness reviewer. The team has implemented the features below in THIS repo "
        "(see `git diff`). Teams frequently implement one feature fully but OMIT or half-finish "
        "another. Go feature by feature:\n"
        "1. For EACH feature, confirm its required public API/behavior actually exists in the code "
        "(grep for the names/symbols the feature requires).\n"
        "2. If a feature is missing, partial, or only stubbed, IMPLEMENT it fully now.\n"
        "3. Ensure the project still builds. Do NOT create or edit test files.\n"
        "Submit only when every feature below is genuinely present and the code builds.\n\n"
        f"{bundle}"
    )


def _repair_task(assignments: list[Assignment]) -> str:
    """Integration/repair brief for the S5 verify-and-fix pass."""
    # Distinct feature specs (skip duplicates from shared-objective mode).
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(specs)
    return (
        "Your teammates have implemented the features below in THIS repository "
        "(see `git diff`). Your job is integration + repair, not new features:\n"
        "1. Make sure EVERY feature below is fully and correctly implemented.\n"
        "2. Make the project BUILD/COMPILE and its existing test suite pass — fix "
        "any compile errors, broken imports, or half-finished work you find.\n"
        "3. Resolve any inconsistencies between the features (naming, signatures).\n"
        "Do NOT create or edit test files. When it builds and is complete, submit.\n\n"
        f"## Features that must all work\n\n{bundle}"
    )


class UnifiedHarness:
    """Runs one team on one task, growing it on demand."""

    def __init__(
        self,
        *,
        bus: TeamBus | None = None,
        step_limit: int = 40,
        cost_limit: float = 5.0,
        command_timeout: int = 60,
        quiet: bool = True,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.bus = bus
        self.step_limit = step_limit
        self.cost_limit = cost_limit
        self.command_timeout = command_timeout
        self.quiet = quiet
        self._on_event = on_event

    def _emit(self, msg: str) -> None:
        if self._on_event is not None:
            self._on_event(msg)

    def _build_assignments(self, spec: TeamSpec) -> list[Assignment]:
        """Resolve a spec into concrete seed assignments.

        ``assignments`` given → used verbatim.  Otherwise a single
        ``objective`` is fanned out to ``team_size`` agents (lead first).
        """
        if spec.assignments:
            return spec.assignments
        if spec.objective is None:
            raise ValueError("TeamSpec needs either assignments or an objective")
        seeds = []
        for i in range(max(1, spec.team_size)):
            seeds.append(
                Assignment(
                    agent_id=f"agent{i + 1}",
                    task=spec.objective,
                    role="lead" if i == 0 else "member",
                    feature_id=spec.features[i] if i < len(spec.features) else None,
                )
            )
        return seeds

    def _run_isolated(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
    ) -> RunResult:
        """Coordinated team where **every agent runs in its OWN container**
        (hard constraint — no shared live container).

        Agents run sequentially; each agent's fresh container is seeded with the
        cumulative diff of the teammates before it (applied via ``git apply``),
        so it builds on their committed work without sharing a live workspace.
        The last agent's cumulative diff is the integrated submission; an
        optional verify-fix integrator also runs in its own container.
        """

        def pick_llm(agent_id: str, role: str) -> LLMClient:
            return llm_factory(agent_id, role) if llm_factory is not None else llm  # type: ignore[return-value]

        def run_on_shared(env: Environment, agent_id: str, role: str, task: str, feature_id: int | None) -> AgentResult:
            """Run one agent (mini-swe or builtin worker) on the shared tree."""
            if spec.spec_fidelity:  # S8: team injects spec-fidelity policy into the agent prompt
                task = _SPEC_FIDELITY + task
            if spec.tdd_preamble:  # T2: in-loop self-verification workflow
                task = _TDD_PREAMBLE + task
            if spec.mine_conventions:  # T4: in-loop convention-mining workflow
                task = _MINE_CONVENTIONS + task
            if spec.worker == "mini_swe":
                from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

                return run_mini_swe_agent(
                    env,
                    task=task,
                    agent_id=agent_id,
                    role=role,
                    model_name=spec.model,
                    step_limit=self.step_limit,
                    cost_limit=self.cost_limit,
                    feature_id=feature_id,
                    command_timeout=self.command_timeout,
                    guard_git=spec.guard_git,
                )
            agent = Agent(
                agent_id=agent_id,
                role=role,
                task=task,
                env=env,
                llm=pick_llm(agent_id, role),
                bus=bus,
                feature_id=feature_id,
                allow_spawn=False,
                step_limit=self.step_limit,
                cost_limit=self.cost_limit,
                command_timeout=self.command_timeout,
            )
            return agent.run()

        def seed_prior(env: Environment, patch: str) -> None:
            """Seed a fresh container with teammates' cumulative work via ``git apply``,
            then COMMIT it so the agent starts from a clean, coherent base (a dirty
            seeded tree confuses the agent and pollutes its own diff)."""
            if not patch.strip():
                return
            env.write_file(".cb_prior.patch", patch)
            env.execute(
                "git apply --whitespace=nowarn .cb_prior.patch 2>/dev/null "
                "|| git apply --3way .cb_prior.patch 2>/dev/null "
                "|| git apply --reject .cb_prior.patch 2>/dev/null || true"
            )
            env.execute(
                "rm -f .cb_prior.patch && git add -A && "
                "git -c user.email=team@cooperagents.local -c user.name=cooperagents commit -q -m 'teammate work' || true"
            )

        seeds: dict[str, AgentResult] = {}
        start = time.time()
        integrated_patch = ""  # cumulative diff across the team so far (seed mode)
        member_patches: list[str] = []  # each agent's own diff (no-seed mode)
        prior: list[str] = []
        prior_fids: list[int] = []  # feature ids done so far (preserve_invariants)
        # HARD CONSTRAINT: every agent runs in its OWN container. With seed mode
        # (default) each fresh container is seeded by teammates' cumulative diff;
        # with no-seed each agent works independently and an integrator merges.
        for a in assignments:
            env = env_factory(a.agent_id)
            try:
                if spec.seed_prior:
                    seed_prior(env, integrated_patch)
                task = a.task
                if prior and spec.teammate_context:
                    if spec.seed_prior:
                        task += (
                            f"\n\nTeammates before you already implemented {', '.join(prior)} — their code is "
                            "ALREADY in this repository (run `git diff` to see it). Build on it, reuse their "
                            "public names/signatures, and do not duplicate or revert it."
                        )
                    elif integrated_patch.strip():
                        drafts = integrated_patch[:6000]
                        task += (
                            f"\n\nTeammates are implementing {', '.join(prior)} in parallel; their drafts:\n"
                            f"```diff\n{drafts}\n```\nReuse their public names/signatures."
                        )
                if spec.preserve_invariants:
                    # Coordination-under-interdependence: each agent publishes a runnable
                    # regression check for its OWN feature into the shared tree (.cb_checks/,
                    # stripped before grading); later agents MUST keep all prior checks green —
                    # directly targeting the dominant coupled failure (a later agent silently
                    # breaking an earlier teammate's feature while editing shared code).
                    if prior_fids:
                        checks = " ".join(f".cb_checks/f{f}.py" for f in prior_fids)
                        task += (
                            f"\n\nTEAMMATE INVARIANTS — features {', '.join(str(f) for f in prior_fids)} are already "
                            f"implemented and WORKING, each verified by a check script: {checks}. FIRST run "
                            f"`python {checks}` to see them pass. As you add your feature you MUST keep ALL of them "
                            "passing — do not change or break a teammate's behavior. Re-run them before finishing; "
                            "if you broke one, fix your code (not the check) until it passes again."
                        )
                    if a.feature_id is not None:
                        task += (
                            f"\n\nPUBLISH YOUR INVARIANT — before finishing, create `.cb_checks/f{a.feature_id}.py`: a "
                            "minimal STANDALONE python script (no pytest) that exercises YOUR feature's public "
                            "behavior and raises/exits non-zero if it regresses. Run it to confirm it passes. This is "
                            "your contract to teammates who build on your work; keep it small and fast."
                        )
                seeds[a.agent_id] = run_on_shared(env, a.agent_id, a.role, task, a.feature_id)
                diff = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
            member_patches.append(diff)
            if spec.seed_prior:
                integrated_patch = diff  # cumulative
            else:
                integrated_patch = "\n".join(member_patches)  # text-only context for next agent
            prior.append(f"feature {a.feature_id}" if a.feature_id is not None else a.role)
            if a.feature_id is not None:
                prior_fids.append(a.feature_id)

        # Integration. Seed mode already has the cumulative diff; no-seed must merge
        # the independent member patches in a fresh integrator container.
        if not spec.seed_prior:
            integrated_patch = ""  # rebuild from member patches below
        if spec.verify_fix and len(assignments) > 1:
            env = env_factory("integrator")
            try:
                if spec.seed_prior:
                    seed_prior(env, integrated_patch)
                else:
                    for p in member_patches:
                        seed_prior(env, p)
                seeds["integrator"] = run_on_shared(env, "integrator", "integrator", _repair_task(assignments), None)
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
        elif not spec.seed_prior and len(member_patches) > 1:
            # No-seed without an LLM integrator: mechanically merge the independent
            # member patches in a fresh container (apply each; conflicts left as .rej).
            env = env_factory("merge")
            try:
                for p in member_patches:
                    seed_prior(env, p)
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
        elif not spec.seed_prior:
            integrated_patch = member_patches[0] if member_patches else ""

        # T3: completeness review — own container, seeded with the full diff,
        # enumerates each feature and fills gaps (the dominant failure mode).
        if spec.completeness_review and len(assignments) > 1:
            env = env_factory("reviewer")
            try:
                seed_prior(env, integrated_patch)
                seeds["reviewer"] = run_on_shared(env, "reviewer", "reviewer", _completeness_task(assignments), None)
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()

        integrated = AgentResult(
            agent_id="team",
            role="integrated",
            status="submitted" if integrated_patch.strip() else "error",
            patch=integrated_patch,
            cost=sum(r.cost for r in seeds.values()),
            steps=sum(r.steps for r in seeds.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=seeds,
            integrated=integrated,
            duration_seconds=time.time() - start,
            metrics=coordination_metrics(bus.task_events(), final_tasks=bus.list_tasks()),
        )

    def _run_worker(
        self,
        spec: TeamSpec,
        env: Environment,
        *,
        agent_id: str,
        role: str,
        task: str,
        feature_id: int | None,
        bus: TeamBus,
        llm: LLMClient | None,
    ) -> AgentResult:
        """Run one agent (mini-swe or builtin) in its own container, applying the
        team-level prompt seams (spec-fidelity / TDD / convention-mining)."""
        if spec.spec_fidelity:
            task = _SPEC_FIDELITY + task
        if spec.tdd_preamble:
            task = _TDD_PREAMBLE + task
        if spec.mine_conventions:
            task = _MINE_CONVENTIONS + task
        if spec.worker == "mini_swe":
            from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

            return run_mini_swe_agent(
                env,
                task=task,
                agent_id=agent_id,
                role=role,
                model_name=spec.model,
                step_limit=self.step_limit,
                cost_limit=self.cost_limit,
                feature_id=feature_id,
                command_timeout=self.command_timeout,
                guard_git=spec.guard_git,
            )
        agent = Agent(
            agent_id=agent_id,
            role=role,
            task=task,
            env=env,
            llm=llm,
            bus=bus,
            feature_id=feature_id,
            allow_spawn=False,
            step_limit=self.step_limit,
            cost_limit=self.cost_limit,
            command_timeout=self.command_timeout,
        )
        return agent.run()

    def _run_decomposed(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
        planner: Planner | None,
    ) -> RunResult:
        """G1+G2+G3: plan an independence-maximizing subtask DAG, run it with
        independent subtasks in PARALLEL (own containers, seeded only along DAG
        edges), then merge the branch deltas.

        Each subtask is one agent in its own container (hard constraint). A
        subtask is seeded with the deltas of its transitive ancestors only — not
        the whole shared state — so two independent branches never see each
        other's edits, eliminating the interference that coupled sequential
        seeding causes (Round 6). Whether the merge is clean is exactly the test
        of decomposition quality.
        """
        start = time.time()
        specs: list[tuple[int, str]] = [
            (a.feature_id if a.feature_id is not None else i + 1, a.task) for i, a in enumerate(assignments)
        ]
        cap = spec.max_agents if spec.max_agents is not None else len(assignments)
        max_sub = max(1, min(cap, len(assignments) + 1))
        if planner is not None:
            subs, rationale = planner(specs, spec.objective)
        else:
            subs, rationale = plan_decomposition(specs, objective=spec.objective, max_subtasks=max_sub, model=spec.model)
        by_id = {s.id: s for s in subs}
        levels = topo_levels(subs)
        topo_order = [s.id for level in levels for s in level]

        def pick_llm(agent_id: str, role: str) -> LLMClient | None:
            return llm_factory(agent_id, role) if llm_factory is not None else llm

        deltas: dict[str, str] = {}
        results: dict[str, AgentResult] = {}
        lock = threading.Lock()

        def ownership_preamble(s: SubTask) -> str:
            """Hard write-set boundary: the key to conflict-free re-division —
            this agent edits ONLY its regions; teammates' regions are off-limits
            so two subtasks on the same file (disjoint regions) merge cleanly."""
            if not s.owns:
                return ""
            others = sorted({o for t in subs if t.id != s.id for o in t.owns})
            msg = (
                "OWNERSHIP — you may edit ONLY these regions (your write-set):\n  - "
                + "\n  - ".join(s.owns)
                + "\nDo NOT edit anything outside them."
            )
            if others:
                msg += (
                    " Teammates own these regions in parallel — do NOT touch them; if you need their "
                    "code, assume the public interface described in the spec:\n  - " + "\n  - ".join(others)
                )
            return msg + "\n\n"

        def run_sub(s: SubTask) -> None:
            env = env_factory(s.id)
            try:
                anc = ancestors(s, by_id)
                for aid in [x for x in topo_order if x in anc]:
                    with lock:
                        seed = deltas.get(aid, "")
                    _seed_patch(env, seed)
                res = self._run_worker(
                    spec,
                    env,
                    agent_id=s.id,
                    role="member",
                    task=ownership_preamble(s) + s.task,
                    feature_id=s.features[0] if s.features else None,
                    bus=bus,
                    llm=pick_llm(s.id, "member"),
                )
                delta = strip_test_sections(env.git_diff())
            except Exception as e:  # noqa: BLE001 - one subtask must not kill the run
                res = AgentResult(agent_id=s.id, role="member", status="error", error=str(e))
                delta = ""
            finally:
                env.cleanup()
            with lock:
                deltas[s.id] = delta
                results[s.id] = res

        # Run level by level; subtasks within a level are independent → parallel.
        for level in levels:
            threads = [threading.Thread(target=run_sub, args=(s,)) for s in level]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Integrate: a fresh container, apply every delta in topo order. Clean if
        # the decomposition was well-separated; conflicts (.rej) if it wasn't.
        env = env_factory("integrator")
        try:
            for sid in topo_order:
                _seed_patch(env, deltas.get(sid, ""))
            integrated_patch = strip_test_sections(env.git_diff())
        finally:
            env.cleanup()

        integrated = AgentResult(
            agent_id="team",
            role="integrated",
            status="submitted" if integrated_patch.strip() else "error",
            patch=integrated_patch,
            cost=sum(r.cost for r in results.values()),
            steps=sum(r.steps for r in results.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=results,
            integrated=integrated,
            duration_seconds=time.time() - start,
            metrics={
                "decompose": True,
                "n_subtasks": len(subs),
                "n_edges": sum(len(s.depends_on) for s in subs),
                "levels": [[s.id for s in level] for level in levels],
                "max_parallel": max((len(level) for level in levels), default=0),
                "rationale": rationale,
            },
        )

    def _run_best_of_n(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
        selector: Callable[[list[RunResult]], int] | None,
    ) -> RunResult:
        """T6: run the isolated team ``best_of_n`` times (each its own containers),
        then a self-available selector picks the candidate to submit.

        Run-to-run variance is the headroom; the selector must use only
        self-available signal (LLM judge / build probe), never the hidden grader.
        If no selector is given, fall back to the candidate whose integrated diff
        touches the most files (a weak coverage heuristic, offline-safe).
        """
        candidates: list[RunResult] = []
        for i in range(spec.best_of_n):
            cand_bus = InMemoryBus(f"{spec.run_id}-c{i}")
            self._emit(f"best-of-{spec.best_of_n}: attempt {i + 1}")
            candidates.append(self._run_isolated(spec, assignments, cand_bus, env_factory, llm, llm_factory))

        def coverage(r: RunResult) -> int:
            patch = r.integrated.patch if r.integrated else ""
            return sum(1 for line in patch.splitlines() if line.startswith("+++ "))

        if selector is not None:
            try:
                chosen = selector(candidates)
            except Exception as e:  # noqa: BLE001 - a flaky selector must not kill the run
                self._emit(f"selector failed ({e}); falling back to coverage heuristic")
                chosen = max(range(len(candidates)), key=lambda j: coverage(candidates[j]))
        else:
            chosen = max(range(len(candidates)), key=lambda j: coverage(candidates[j]))

        result = candidates[chosen]
        result.metrics = {
            **result.metrics,
            "best_of_n": spec.best_of_n,
            "chosen_index": chosen,
            "candidate_coverage": [coverage(c) for c in candidates],
            "candidate_patch_lines": [c.integrated.patch_lines if c.integrated else 0 for c in candidates],
        }
        result.duration_seconds = sum(c.duration_seconds for c in candidates)
        return result

    def run(
        self,
        spec: TeamSpec,
        *,
        env_factory: EnvFactory,
        llm: LLMClient | None = None,
        llm_factory: LLMFactory | None = None,
        selector: Callable[[list[RunResult]], int] | None = None,
        planner: Planner | None = None,
    ) -> RunResult:
        if llm is None and llm_factory is None and spec.worker != "mini_swe":
            raise ValueError("provide either llm or llm_factory")
        bus = self.bus or InMemoryBus(spec.run_id)
        assignments = self._build_assignments(spec)
        if spec.decompose:
            return self._run_decomposed(spec, assignments, bus, env_factory, llm, llm_factory, planner)
        if spec.shared_workspace and spec.best_of_n > 1:
            return self._run_best_of_n(spec, assignments, env_factory, llm, llm_factory, selector)
        if spec.shared_workspace:
            return self._run_isolated(spec, assignments, bus, env_factory, llm, llm_factory)
        n_seed = len(assignments)
        cap = spec.max_agents if spec.max_agents is not None else n_seed
        spawning = spec.allow_spawn and cap > n_seed

        def pick_llm(agent_id: str, role: str) -> LLMClient:
            return llm_factory(agent_id, role) if llm_factory is not None else llm  # type: ignore[return-value]

        seeds: dict[str, AgentResult] = {}
        helpers: dict[str, AgentResult] = {}
        envs: list[Environment] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()
        live = 0
        total_agents = n_seed

        def worker(agent_id: str, role: str, task: str, feature_id: int | None, *, is_helper: bool) -> None:
            nonlocal live
            try:
                env = env_factory(agent_id)
                with lock:
                    envs.append(env)
                agent = Agent(
                    agent_id=agent_id,
                    role=role,
                    task=task,
                    env=env,
                    llm=pick_llm(agent_id, role),
                    bus=bus,
                    feature_id=feature_id,
                    allow_spawn=spawning,
                    step_limit=self.step_limit,
                    cost_limit=self.cost_limit,
                    command_timeout=self.command_timeout,
                )
                result = agent.run()
            except Exception as e:  # noqa: BLE001 - never let a worker kill the run
                result = AgentResult(agent_id=agent_id, role=role, status="error", feature_id=feature_id, error=str(e))
            with lock:
                (helpers if is_helper else seeds)[agent_id] = result
                live -= 1
            self._emit(f"{agent_id} done: {result.status}")

        def start(agent_id: str, role: str, task: str, feature_id: int | None, *, is_helper: bool) -> None:
            nonlocal live
            with lock:
                live += 1
            t = threading.Thread(target=worker, args=(agent_id, role, task, feature_id), kwargs={"is_helper": is_helper})
            threads.append(t)
            t.start()

        def supervise() -> None:
            nonlocal total_agents
            while True:
                req = bus.spawn_pop(timeout=0.5)
                if req is None:
                    with lock:
                        if live == 0:
                            break
                    continue
                with lock:
                    granted = total_agents < cap
                    if granted:
                        total_agents += 1
                if not granted:
                    bus.spawn_mark(req.id, outcome="capped")
                    self._emit(f"spawn capped (cap={cap}) from {req.requested_by}")
                    continue
                idx = bus.spawn_next_index()
                helper_id = f"helper{idx}"
                bus.spawn_mark(req.id, outcome="granted", agent_id=helper_id)
                self._emit(f"spawned {helper_id} for {req.requested_by}")
                start(helper_id, req.role or "helper", req.task, None, is_helper=True)

        start_time = time.time()
        supervisor: threading.Thread | None = None
        try:
            for a in assignments:
                start(a.agent_id, a.role, a.task, a.feature_id, is_helper=False)
            if spawning:
                supervisor = threading.Thread(target=supervise, daemon=True)
                supervisor.start()
                supervisor.join()  # returns only when the whole pool is idle
            for t in list(threads):
                t.join()
        finally:
            for env in envs:
                env.cleanup()

        duration = time.time() - start_time
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=seeds,
            helpers=helpers,
            duration_seconds=duration,
            metrics=coordination_metrics(bus.task_events(), final_tasks=bus.list_tasks()),
            spawn_metrics=spawn_metrics(bus.spawn_events()) if spawning else {},
        )


__all__ = ["UnifiedHarness", "EnvFactory", "LLMFactory"]
