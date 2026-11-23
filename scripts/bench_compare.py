"""Solo vs. unified-team performance comparison on CooperBench pairs.

For each (repo, task, [f1,f2]) pair this runs two arms with the SAME agent
loop + model, so the only variable is team structure:

  * solo  — 1 agent implements both features (CooperBench ``solo`` scoring)
  * team  — 1 agent per feature + dynamic helpers (``team`` scoring)

then scores both with CooperBench and prints pass-rate + wall-clock.

Usage:
    uv run python scripts/bench_compare.py --limit 2 --max-agents 3
    uv run python scripts/bench_compare.py --pairs go_chi_task:26:1,2 go_chi_task:56:1,5
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from pathlib import Path

from cooperagents import verification as _verification
from cooperagents.agent_coordination_types import AgentCoordinatorConfig, GateConfig
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.runtime import task_environment
from cooperagents.eval.cooperbench import run_eval, write_run_outputs
from cooperagents.eval.dataset import WorkItem, image_name, load_subset, read_feature
from cooperagents.harness import UnifiedHarness
from cooperagents.initial_plan import InitialPlan, initial_plan_path
from cooperagents.metrics import completion_time_summary
from cooperagents.types import Assignment, TeamSpec


def _load_env(path: str | None = None) -> None:
    p = Path(path or os.getenv("ENV_FILE", ".env"))
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao")


def run_solo(item: WorkItem, *, run_name: str, logs_dir: Path, step_limit: int, agent_time_limit: int | None = None) -> dict:
    feats = sorted(item.features)
    objective = "\n\n---\n\n".join(f"## Feature {f}\n\n{read_feature(item.repo, item.task_id, f)}" for f in feats)
    run_id = uuid.uuid4().hex[:8]
    spec = TeamSpec(
        run_id=run_id,
        repo=item.repo,
        task_id=item.task_id,
        features=feats,
        objective=objective,
        team_size=1,
        agent_time_limit=agent_time_limit,
        max_agents=1,
        allow_spawn=False,
        shared_workspace=True,
        worker="mini_swe",
        model=MODEL,
    )
    harness = UnifiedHarness(bus=InMemoryBus(run_id), step_limit=step_limit, command_timeout=300)
    img = image_name(item.repo, item.task_id)
    res = harness.run(spec, env_factory=lambda _id, _i=img: task_environment(_i))
    write_run_outputs(res, run_name=run_name, logs_dir=logs_dir, setting="solo", model=MODEL)
    return {"duration": res.duration_seconds, "steps": res.total_steps, "tokens": 0}


def _judge_selector(spec_bundle: str):
    """Build a best-of-N selector backed by the LLM judge (pairwise, both orders).

    King-of-the-hill: keep the current best candidate and compare each next one
    against it; the survivor is submitted. The judge sees only the spec + diffs —
    never the hidden grader — so this is genuine SELF-selection. Empty diffs lose
    by default; ties keep the incumbent (cheaper, equal quality)."""
    from cooperagents.eval.judge import LLMJudge

    judge = LLMJudge(model=MODEL)

    def select(candidates: list) -> int:
        best = 0
        best_diff = candidates[0].integrated.patch if candidates[0].integrated else ""
        for i in range(1, len(candidates)):
            cur = candidates[i].integrated.patch if candidates[i].integrated else ""
            if not cur.strip():
                continue
            if not best_diff.strip():
                best, best_diff = i, cur
                continue
            verdict = judge.compare(task=spec_bundle, baseline_diff=best_diff, candidate_diff=cur)
            if verdict.winner == "candidate":
                best, best_diff = i, cur
        return best

    return select


def _mechanical_selector(img: str):
    """Q2: best-of-N selection by MECHANICAL verification, not LLM judgment.

    T6's LLM judge mis-picked because it cannot predict hidden-test outcomes.
    This selector applies each candidate's integrated diff in a fresh container
    and scores it with observable signals only: tree health (build/AST gate)
    and the repo's OWN visible test suite (never the hidden grader). At 9B the
    run-to-run variance is ±4 features on qwen-14 — sample-and-verify converts
    that variance into signal iff the verifier correlates with grading."""
    import re

    from cooperagents.harness import _seed_patch, _tree_health

    def select(candidates: list) -> int:
        # One container per candidate, kept open across two phases:
        #   1. apply the diff, read the candidate's own .cb_checks/* (Q8),
        #   2. write the POOLED checks from all candidates, then score.
        # Cross-attempt checks are the feature-sensitive signal repo tests lack:
        # a candidate that satisfies the OTHER attempt's acceptance probes
        # implemented the consensus reading of the spec.
        envs: list = []
        checks: dict[str, str] = {}
        try:
            for c in candidates:
                patch = c.integrated.patch if c.integrated else ""
                if not patch.strip():
                    envs.append(None)
                    continue
                env = task_environment(img)
                envs.append(env)
                _seed_patch(env, patch)
                names = env.execute("ls .cb_checks/*.py 2>/dev/null").stdout.split()
                for n in names:
                    body = env.execute(f"cat {n}").stdout
                    if body.strip():
                        checks.setdefault(n.strip(), body)  # first writer wins per name+attempt
            healths = [None if e is None else (1 if _tree_health(e) else 0) for e in envs]
            if os.getenv("COOPER_SELECT_FASTPATH") == "1" and sum(1 for h in healths if h == 1) == 1:
                # Fast path (opt-in): exactly one intact candidate — skip tests.
                # DEFAULT OFF: skipping verification cost ~2-4 features (rt sweep
                # flat-low at all cap values vs uncapped TK9's 17.0).
                best = healths.index(1)
                print(f"    [mechanical-select] health fast-path -> candidate {best}", flush=True)
                return best

            def _score(idx: int) -> tuple:
                env = envs[idx]
                if env is None:
                    return (-1, -1000, -1000, -1)
                xpass = 0
                for i, (_name, body) in enumerate(sorted(checks.items())):
                    env.write_file(f".cb_pool/chk_{i}.py", body)
                    r = env.execute(f"timeout 60 python3 .cb_pool/chk_{i}.py >/dev/null 2>&1; echo $?")
                    xpass += 1 if r.stdout.strip().endswith("0") else 0
                env.execute("rm -rf .cb_pool")
                res = env.execute("python3 -m pytest -q 2>&1 | tail -2", timeout=900)
                passed = sum(int(m) for m in re.findall(r"(\d+) passed", res.stdout))
                failed = sum(int(m) for m in re.findall(r"(\d+) (?:failed|error)", res.stdout))
                return (healths[idx], xpass, passed - failed, passed)

            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=max(1, len(envs))) as _ex:
                scores = list(_ex.map(_score, range(len(envs))))
        finally:
            for env in envs:
                if env is not None:
                    env.cleanup()
        best = max(range(len(scores)), key=lambda i: (scores[i], -i))  # ties -> lower index
        print(f"    [mechanical-select] pooled_checks={len(checks)} scores={scores} -> candidate {best}", flush=True)
        return best

    return select


def _planning_completion(model_name: str, *, worker: bool):
    """Bind planning to the same verified endpoint transport as mini-SWE."""
    from cooperagents.completion import CompletionBinding, CompletionSettings, MiniSweCompletionRequest
    from cooperagents.sampling import sampling_kwargs
    from cooperagents.vendor.mini_swe.models.utils.retry import retry
    from cooperagents.workers.mini_swe_worker import build_model

    model = build_model(model_name, worker_sampling=worker)
    generation = sampling_kwargs(worker=worker)
    if not generation.keys() & {"max_tokens", "max_completion_tokens"}:
        generation.update({"max_tokens": 4096} if worker else {"max_completion_tokens": 2000})
    settings = CompletionSettings(
        model=model.config.model_name,
        revision=model_name,
        generation=generation,
        timeout=float(model.config.model_kwargs["timeout"]),
    )

    def complete(request: MiniSweCompletionRequest):
        transport = copy.copy(model)
        transport.trace = request.trace
        for attempt in retry(logger=logging.getLogger("cooperagents.planning_completion"), abort_exceptions=transport.abort_exceptions):
            with attempt:
                return transport._query(
                    request.messages,
                    _tools=request.tools,
                    worker_sampling=worker,
                    verify_wire=True,
                    timeout=request.settings.timeout,
                    **(
                        {"tool_choice": request.tool_choice}
                        if request.tool_choice and os.getenv("COOPER_SEND_TOOL_CHOICE", "true").lower() != "false" else {}
                    ),
                    **request.settings.generation,
                )

    return CompletionBinding(complete, settings, complete, settings)


def run_team(
    item: WorkItem,
    *,
    run_name: str,
    logs_dir: Path,
    step_limit: int,
    max_agents: int,
    agent_time_limit: int | None = None,
    verify_fix: bool = False,
    spec_fidelity: bool = False,
    teammate_context: bool = False,
    guard_git: bool = False,
    seed_prior: bool = True,
    reverse_order: bool = False,
    completeness_review: bool = False,
    tdd_preamble: bool = False,
    mine_conventions: bool = False,
    best_of_n: int = 1,
    decompose: bool = False,
    preserve_invariants: bool = False,
    adaptive: bool = False,
    do_no_harm: bool = False,
    select: str = "judge",
    diversity_temp: float | None = None,
    coop_tools: bool = False,
    repair_integrator: bool = False,
    repair_steps: int = 25,
    contract_first: bool = False,
    live_awareness: bool = False,
    behavioral_gate: bool = False,
    tool_protocol: bool = False,
    task_board: bool = False,
    wait_protocol: bool = False,
    git_share: bool = False,
    team_roles: bool = False,
    coordinator: bool = False,
    deterministic_coordinator: bool = False,
    agent_coordinator: bool = False,
    agent_coordinator_config: AgentCoordinatorConfig | None = None,
    coordinator_notebook: bool = True,
    coordination_variant: str = "auto-prompt",
    coordination_mode: str = "direct",
    coordinator_implementation_enabled: bool = True,
    initial_plan_dir: Path | None = None,
    planning_step_limit: int = 200,
    planning_time_limit: int = 600,
    worker_reminder_interval: int = 10,
    focused_repair: bool = False,
    repair_time: int = 0,
    apply_merge: bool = False,
    claim_mode: bool = False,
    allow_spawn: bool = False,
    n_agents: int = 2,
    completion_gate: bool = False,
    presub_merge: bool = False,
    repair_attempts: int = 1,
    langfuse: bool = False,
    record_trajectory: bool = False,
    checkpoint_repair: bool = False,
    stop_after_repair_checkpoint: bool = False,
    capture_merged_state: bool = False,
    persist_worker_patches: bool = False,
    only_first_repair_checkpoint: bool = False,
    collect_source_conflicts: bool = False,
) -> dict:
    feats = sorted(item.features)
    if reverse_order:
        feats = list(reversed(feats))
    if claim_mode:
        # TK6: every agent gets the WHOLE objective; the board carries the split.
        bundle = "\n\n---\n\n".join(
            f"## Feature {f}\n\n{read_feature(item.repo, item.task_id, f)}" for f in feats
        )
        assignments = [
            Assignment(
                agent_id=f"agent{i + 1}",
                role="lead" if i == 0 else "member",
                feature_id=feats[i] if i < len(feats) else None,
                task=bundle,
            )
            for i in range(max(n_agents, 1))
        ]
    else:
        assignments = [
            Assignment(
                agent_id=f"agent{i + 1}", role="lead" if i == 0 else "member", feature_id=f, task=read_feature(item.repo, item.task_id, f)
            )
            for i, f in enumerate(feats)
        ]
    spec_bundle = "\n\n---\n\n".join(a.task for a in assignments)
    run_id = uuid.uuid4().hex[:8]
    spec = TeamSpec(
        run_id=run_id,
        repo=item.repo,
        task_id=item.task_id,
        features=feats,
        assignments=assignments,
        max_agents=max_agents,
        agent_time_limit=agent_time_limit,
        shared_workspace=True,
        worker="mini_swe",
        model=MODEL,
        verify_fix=verify_fix,
        spec_fidelity=spec_fidelity,
        teammate_context=teammate_context,
        guard_git=guard_git,
        seed_prior=seed_prior,
        completeness_review=completeness_review,
        tdd_preamble=tdd_preamble,
        mine_conventions=mine_conventions,
        best_of_n=best_of_n,
        decompose=decompose,
        preserve_invariants=preserve_invariants,
        adaptive=adaptive,
        do_no_harm=do_no_harm,
        diversity_temperature=diversity_temp,
        coop_tools=coop_tools,
        repair_integrator=repair_integrator,
        repair_step_limit=repair_steps,
        contract_first=contract_first,
        live_awareness=live_awareness,
        behavioral_gate=behavioral_gate,
        tool_protocol=tool_protocol,
        task_board=task_board,
        wait_protocol=wait_protocol,
        git_share=git_share,
        team_roles=team_roles,
        coordinator=coordinator,
        deterministic_coordinator=deterministic_coordinator,
        agent_coordinator=agent_coordinator,
        coordinator_notebook=coordinator_notebook,
        focused_repair=focused_repair,
        repair_time_limit=repair_time or None,
        apply_chain_merge=apply_merge,
        claim_mode=claim_mode,
        allow_spawn_tool=allow_spawn,
        repair_attempts=repair_attempts,
        completion_gate=(
            partial(_verification.validate, merged=presub_merge, build_artifact=None)
        ) if (completion_gate or presub_merge) else None,
        select_integration=(
            lambda patches: max(range(len(patches)), key=lambda i: len(patches[i]))
        ) if presub_merge else None,
    )
    pair_dir = _pair_dir(logs_dir / run_name, "team", item)
    notebook_path = (pair_dir / "coordination" / run_id / "notebook.md").resolve() if coordinator and coordinator_notebook else None
    if agent_coordinator and agent_coordinator_config is None:
        agent_coordinator_config = AgentCoordinatorConfig()
    harness = UnifiedHarness(
        bus=None if checkpoint_repair or capture_merged_state else InMemoryBus(run_id),
        step_limit=step_limit,
        command_timeout=300,
        coordinator_notebook_path=notebook_path,
        agent_coordinator_config=agent_coordinator_config,
        coordination_variant=coordination_variant,
        langfuse=langfuse,
        coordination_mode=coordination_mode,
        coordinator_implementation_enabled=coordinator_implementation_enabled,
        coordinator_completion=_planning_completion(spec.model, worker=False) if coordination_mode == "group-chat-planning" else None,
        worker_completion=_planning_completion(spec.model, worker=True) if coordination_mode == "group-chat-planning" else None,
        initial_plan=(InitialPlan.load(initial_plan_path(initial_plan_dir, f"{item.repo}:{item.task_id}:{','.join(map(str, feats))}"),
                                       expected_pair=f"{item.repo}:{item.task_id}:{','.join(map(str, feats))}")
                      if initial_plan_dir is not None else None),
        planning_step_limit=planning_step_limit,
        planning_time_limit_s=planning_time_limit,
        worker_reminder_interval=worker_reminder_interval,
        checkpoint_dir=pair_dir / "checkpoints" if checkpoint_repair or capture_merged_state else None,
        capture_merged_state=capture_merged_state,
        persist_worker_patches=persist_worker_patches,
        stop_after_repair_checkpoint=stop_after_repair_checkpoint,
        only_first_repair_checkpoint=only_first_repair_checkpoint,
        collect_source_conflicts=collect_source_conflicts,
    )
    img = image_name(item.repo, item.task_id)
    selector = None
    if best_of_n > 1:
        selector = _mechanical_selector(image_name(item.repo, item.task_id)) if select == "mechanical" else _judge_selector(spec_bundle)
    from cooperagents.trajectory import Trajectory

    trajectory = None
    agent_fields = {}
    if agent_coordinator_config is not None:
        gate = agent_coordinator_config.gate
        agent_fields = dict(
            agent_gate=gate.policy, agent_gate_model=gate.model, agent_gate_base_url=gate.base_url,
            agent_gate_threshold=gate.wake_threshold, agent_gate_timeout=gate.timeout_s,
            agent_wake_budget=gate.wake_budget, agent_opening=agent_coordinator_config.opening,
            agent_spec_check=agent_coordinator_config.spec_check,
            agent_random_seed=gate.random_seed if gate.policy == "random" else None,
            agent_random_wakes=gate.random_wakes, agent_random_step_horizon=gate.random_step_horizon,
        )
    if record_trajectory or checkpoint_repair:
        if best_of_n != 1 or decompose or adaptive or (not coop_tools and not capture_merged_state):
            raise ValueError("Trajectory collection requires a single coop-tools team or merged-state capture")
        trajectory = Trajectory(pair_dir / "trajectory.jsonl.gz")
        harness.trajectory = trajectory
        trajectory.emit(
            "harness",
            "pair_start",
            run_id=run_id,
            repo=item.repo,
            task_id=item.task_id,
            features=feats,
            coordinator=coordinator,
            deterministic_coordinator=deterministic_coordinator,
            agent_coordinator=agent_coordinator,
            **agent_fields,
            coordinator_notebook=coordinator and coordinator_notebook,
            coordination_variant=coordination_variant,
            coordination_mode=coordination_mode,
            coordinator_implementation_enabled=coordinator_implementation_enabled,
            planning_step_limit=planning_step_limit,
            planning_time_limit_s=planning_time_limit,
            worker_reminder_interval=worker_reminder_interval,
        )
    try:
        worker_ids = {a.agent_id for a in assignments}
        host_share = git_share and os.getenv("COOPER_RUNTIME", "docker") != "docker"
        with tempfile.TemporaryDirectory(prefix="cooper-git-share-") if host_share else nullcontext() as share_dir:
            volumes = [f"{share_dir or f'cbs{run_id}'}:/cbshared"] if git_share else []
            if team_roles:
                volumes.append(f"cbt{run_id}:/workspace/shared")
            res = harness.run(spec, env_factory=lambda _id: task_environment(
                img, volumes=volumes or None,
                coordinator_dir=notebook_path.parent if notebook_path is not None and _id in worker_ids else None,
            ), selector=selector)
        if trajectory:
            trajectory.emit("harness", "pair_end", agents={k: {"status": v.status, "steps": v.steps} for k, v in res.seeds.items()})
    finally:
        if trajectory:
            trajectory.close()
    write_run_outputs(res, run_name=run_name, logs_dir=logs_dir, setting="team", model=MODEL)
    return {"duration": res.duration_seconds, "steps": res.total_steps, "tokens": 0, "helpers": len(res.helpers)}


def _pair_dir(run_dir: Path, setting: str, item: WorkItem) -> Path:
    features = "_".join(f"f{f}" for f in sorted(item.features))
    return run_dir / setting / item.repo / str(item.task_id) / features


def _matched_gate(gate: GateConfig, match_dir: Path, item: WorkItem) -> GateConfig:
    result = _pair_dir(match_dir, "team", item) / "result.json"
    try:
        metrics = json.loads(result.read_text())["metrics"]["agent_coordinator"]
        wakes, horizon = metrics["gate"]["wake"], metrics["team_worker_steps"]
        if type(wakes) is not int or type(horizon) is not int or not 0 <= wakes <= horizon:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("random gate requires a matched agent_main result") from exc
    return replace(gate, random_wakes=wakes, random_step_horizon=horizon)


def read_eval(logs_dir: Path, run_name: str, setting: str, item: WorkItem) -> tuple[bool, int] | None:
    """Return (both_passed, n_features_passed) for a scored pair, or None if unscored.

    Feature-level counts double the measurement resolution — essential in
    low-pass-rate regimes (small models) where pair-level deltas drown in noise.
    """
    p = _pair_dir(logs_dir / run_name, setting, item) / "eval.json"
    if not p.is_file():
        return None
    e = json.loads(p.read_text())
    nfeat = sum(1 for k in ("feature1", "feature2") if (e.get(k) or {}).get("passed"))
    return bool(e.get("both_passed")), nfeat


def _write_summaries(logs_dir: Path, items: list[WorkItem], arms: list[tuple[str, str, int | None, int | None]]) -> None:
    by_run: dict[str, dict] = {}
    for setting, name, pairs_passed, features_passed in arms:
        run_dir = logs_dir / name
        durations = []
        cost = 0.0
        coordinator_metrics = []
        for item in items:
            result_path = _pair_dir(run_dir, setting, item) / "result.json"
            if not result_path.is_file():
                continue
            result = json.loads(result_path.read_text())
            durations.append(result["duration_seconds"])
            cost += result["total_cost"]
            agent_metrics = result.get("metrics", {}).get("agent_coordinator")
            if agent_metrics is not None:
                coordinator_metrics.append(agent_metrics)
        times = completion_time_summary(durations)
        summary = dict(completion_time=times, pairs_passed=pairs_passed, features_passed=features_passed, total_worker_cost=cost)
        if coordinator_metrics:
            usage = {
                actor: {
                    key: sum(metrics["usage"][actor][key] for metrics in coordinator_metrics)
                    for key in ("calls", "prompt_tokens", "cached_tokens", "completion_tokens")
                }
                for actor in ("coordinator", "coordinator_gate")
            }
            summary["agent_coordinator"] = dict(
                wakes=sum(metrics["gate"]["wake"] for metrics in coordinator_metrics),
                skips=sum(metrics["gate"]["skip"] for metrics in coordinator_metrics),
                usage=usage,
            )
        by_run.setdefault(name, {})[setting] = summary
        if pairs_passed is not None:
            percentiles = " ".join(f"{key}={times[key]:.2f}s" if times[key] is not None else f"{key}=n/a"
                                   for key in ("p50", "p90", "p95", "p99"))
            print(f"  {setting} completion time (n={times['n']}): {percentiles}")
            print(f"  {setting} total worker cost: ${cost:.6f}")
            if agent := summary.get("agent_coordinator"):
                coordinator, gate = agent["usage"]["coordinator"], agent["usage"]["coordinator_gate"]
                print(
                    f"  {setting} agent coordinator: wakes={agent['wakes']} skips={agent['skips']} "
                    f"coordinator calls={coordinator['calls']} tokens={coordinator['prompt_tokens'] + coordinator['completion_tokens']} "
                    f"gate calls={gate['calls']} tokens={gate['prompt_tokens'] + gate['completion_tokens']}"
                )
    for name, summary_by_arm in by_run.items():
        run_dir = logs_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "summary.json").write_text(json.dumps(summary_by_arm, indent=2) + "\n")


def main() -> None:
    _load_env()
    global MODEL
    MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap number of pairs (default: no limit)")
    ap.add_argument("--subset", default="flash",
                    help="CooperBench subset to load when --pairs is not given (default: flash; use 'all' for all 652 pairs)")
    ap.add_argument("--pairs", nargs="*", default=None, help="repo:task:f1,f2 ...")
    ap.add_argument("--max-agents", type=int, default=3)
    ap.add_argument("--step-limit", type=int, default=30)
    ap.add_argument("--agent-time-limit", type=int, default=0, help="per-worker wall-clock seconds; 0 disables")
    ap.add_argument("--concurrency", type=int, default=1, help="pairs to run in parallel")
    ap.add_argument("--eval-concurrency", type=int, default=4, help="parallel evals (lower under emulation)")
    ap.add_argument("--verify-fix", action="store_true", help="S5: team verify-and-fix integrator pass")
    ap.add_argument("--spec-fidelity", action="store_true", help="S8: inject spec-fidelity instruction into agent prompts")
    ap.add_argument("--teammate-context", action="store_true", help="S2: show prior teammate diff to each agent")
    ap.add_argument("--guard-git", action="store_true", help="S7: block destructive git on the shared tree")
    ap.add_argument("--no-seed", action="store_true", help="independent agents (no seeding) + integrator merge")
    ap.add_argument("--reverse-order", action="store_true", help="implement features in reverse order")
    ap.add_argument("--completeness-review", action="store_true", help="T3: reviewer pass that fills omitted features")
    ap.add_argument("--tdd-preamble", action="store_true", help="T2: in-loop self-verification (derive checks from spec first)")
    ap.add_argument("--mine-conventions", action="store_true", help="T4: in-loop convention mining before editing")
    ap.add_argument("--best-of-n", type=int, default=1, help="T6: run the team N times, judge-select the best candidate to submit")
    ap.add_argument("--decompose", action="store_true", help="G1-3: planner cuts an independence-max subtask DAG, run parallel")
    ap.add_argument("--preserve-invariants", action="store_true", help="C1: agents publish regression checks; later agents keep them green")
    ap.add_argument("--git-share", action="store_true", help="TK-git: live shared git remote between parallel agents (coop+git cell)")
    ap.add_argument("--team-roles", action="store_true",
                    help="Complete-Team cell: lead/member roles + shared scratchpad volume;"
                         " lead merges member patches and its tree is the submission")
    coordinator_family = ap.add_mutually_exclusive_group()
    coordinator_family.add_argument("--coordinator", action="store_true",
                    help="tool-call coordinator: initial and ongoing coordination, messages and a read-only shared notebook")
    coordinator_family.add_argument("--deterministic-coordinator", action="store_true",
                                    help="monitor repeated commands, errors, and overlapping edits; requires --coop-tools --no-seed")
    coordinator_family.add_argument("--agent-coordinator", action="store_true",
                                    help="event-driven coordinator; requires --coop-tools --no-seed --git-share")
    ap.add_argument("--agent-gate", choices=("model", "rule", "random"), default="model", help="wake policy (default: model)")
    ap.add_argument("--agent-gate-model", help="decision model (model gate only)")
    ap.add_argument("--agent-gate-base-url", help="Decisions API base URL (model gate only)")
    ap.add_argument("--agent-gate-api-key-env", help="name of the decision API key variable (model gate only)")
    ap.add_argument("--agent-gate-threshold", type=float, help="minimum WAKE probability (model gate only)")
    ap.add_argument("--agent-gate-timeout", type=float, help="decision timeout in seconds (model gate only)")
    ap.add_argument("--agent-wake-budget", type=int, default=8, help="maximum wakes per pair (default: 8)")
    ap.add_argument("--agent-opening", choices=("draft", "none"), default="draft", help="opening plan policy (default: draft)")
    ap.add_argument("--no-agent-spec-check", action="store_true", help="disable the coordinator's pre-submit spec check")
    ap.add_argument("--agent-random-match", help="matched agent_main run name under --log-dir (required for the random gate)")
    ap.add_argument("--agent-random-seed", type=int, default=0, help="matched schedule seed (default: 0)")
    ap.add_argument("--initial-plan-dir", type=Path, help="Reviewed plan/history checkpoint; start directly in implementation")
    ap.add_argument("--no-implementation-coordinator", action="store_true",
                    help="skip coordinator model decisions during implementation; keep planning, group chat and notebook delivery")
    ap.add_argument("--no-coordinator-notebook", action="store_true",
                    help="keep the same coordinator policy and messages, with no notebook or mount")
    ap.add_argument("--planning-step-limit", type=int, default=200)
    ap.add_argument("--planning-time-limit", type=int, default=600)
    ap.add_argument("--worker-reminder-interval", type=int, default=10, help="remind every N worker decisions; 0 disables")
    ap.add_argument("--coordination-mode", choices=("direct", "group-chat", "group-chat-planning"), default="direct")
    ap.add_argument("--coordination-variant", choices=("auto-prompt", "human-prompt"), default="auto-prompt")
    ap.add_argument("--focused-repair", action="store_true", help="R2: harness gathers merge-damage evidence into the repair brief")
    ap.add_argument("--repair-time", type=int, default=0, help="TK9f: wall-clock cap (s) for the repair agent; 0 = uncapped")
    ap.add_argument("--apply-merge", action="store_true", help="TK8: pure apply-chain merge (Q5 base) instead of 3-way-first")
    ap.add_argument("--claim-mode", action="store_true",
                    help="TK6: shared objective + unclaimed board tasks; agents self-partition via task_claim")
    ap.add_argument("--allow-spawn", action="store_true", help="TK7: spawn_helper tool (helper cap = max-agents minus seed agents)")
    ap.add_argument("--agents", type=int, default=2, help="claim-mode seed agent count")
    ap.add_argument("--task-board", action="store_true", help="TK4: shared task-board tools with fair billing + status protocol")
    ap.add_argument("--wait-protocol", action="store_true", help="TK5: blocking send_message wait:true, fairly billed")
    ap.add_argument("--tool-protocol", action="store_true",
                    help="TK3: fairly-advertised send_message (system-prompt billing + first-action protocol);"
                         " usage stays the model's choice")
    ap.add_argument("--behavioral-gate", action="store_true", help="Q10: repair-trigger = syntax + published checks + fail-fast repo tests")
    ap.add_argument("--contract-first", action="store_true",
                    help="TK1/Q6: planner writes the shared interface contract; injected into all briefs")
    ap.add_argument("--live-awareness", action="store_true", help="Legacy compatibility flag; dirty-file [team] hints have been removed")
    ap.add_argument("--repair-steps", type=int, default=25, help="step cap for the Q5 merge-repair agent")
    ap.add_argument("--repair-integrator", action="store_true",
                    help="Q5: health-gate the no-seed merge; run one repair agent only when broken")
    ap.add_argument("--completion-gate", action="store_true",
                    help="Reject each agent's finish until verification.validate() passes in the agent's own container "
                         "(equivalent to ProgramBench --completion-gate; up to 3 rejections per agent)")
    ap.add_argument("--presub-merge", action="store_true",
                    help="Before finishing, each agent merges all teammate branches and the completion gate checks the "
                         "merged tree; integration then selects the best member patch instead of re-merging "
                         "(equivalent to ProgramBench --presub-merge; requires --git-share)")
    ap.add_argument("--repair-attempts", type=int, default=1,
                    help="Maximum sequential repair passes on the merged tree (stops early once healthy); "
                         "equivalent to ProgramBench's up-to-2-repair-agents loop (default 1)")
    ap.add_argument("--coop-tools", action="store_true",
                    help="Q4: concurrent agents + bus send_message tool (CooperBench team-harness shape); use with --no-seed")
    ap.add_argument("--diversity-temp", type=float, default=None,
                    help="Q3: sample best-of-N attempts >1 at this temperature (attempt 1 stays pinned)")
    ap.add_argument("--select", choices=["judge", "mechanical"], default="judge",
                    help="best-of-N selector: LLM judge (T6) or mechanical health+repo-tests (Q2)")
    ap.add_argument("--do-no-harm", action="store_true",
                    help="Q1: discard an agent's delta if it broke a previously-healthy tree (compile/AST gate)")
    ap.add_argument("--adaptive", action="store_true", help="runtime topology: parallel, fall back to sequential on merge conflict")
    ap.add_argument("--team-only", action="store_true", help="skip the solo arm (reuse an existing solo baseline)")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--solo-name", default="cmp-solo")
    ap.add_argument("--team-name", default="cmp-team")
    ap.add_argument("--solo-only", action="store_true", help="skip the team arm (e.g. solo calibration sweeps)")
    ap.add_argument("--resume", action="store_true", help="skip pairs that already have a result.json on disk")
    ap.add_argument("--langfuse", action="store_true", help="export worker and coordinator traces to Langfuse (default: off)")
    ap.add_argument("--record-trajectory", action="store_true", help="append complete agent/coordinator I/O for replay")
    ap.add_argument("--persist-worker-patches", action="store_true", help="save each worker patch and merge state")
    ap.add_argument("--capture-merged-state", action="store_true", help="capture the merged tree and stop before evaluation")
    ap.add_argument("--checkpoint-repair", action="store_true", help="save worker and repair filesystems and full I/O for replay")
    ap.add_argument("--stop-after-repair-checkpoint", action="store_true",
                    help="stop after before-integrator1 is saved, without repair inference or official evaluation")
    ap.add_argument("--only-first-repair-checkpoint", action="store_true",
                    help="omit every checkpoint except before-integrator1; requires --stop-after-repair-checkpoint")
    ap.add_argument("--collect-source-conflicts", action="store_true",
                    help="training collection only: also capture source conflict blocks despite a healthy gate")
    ap.add_argument("--skip-eval", action="store_true", help="write generation artifacts without scoring")
    args = ap.parse_args()
    if args.persist_worker_patches and (not args.team_only or args.solo_only or not args.no_seed or args.capture_merged_state):
        ap.error("--persist-worker-patches requires --team-only --no-seed and excludes --capture-merged-state")
    if args.capture_merged_state:
        if (not args.team_only or args.solo_only or not args.no_seed or not args.record_trajectory or args.repair_integrator
            or args.focused_repair or args.verify_fix or args.completeness_review or args.checkpoint_repair
            or args.stop_after_repair_checkpoint or args.only_first_repair_checkpoint or args.collect_source_conflicts):
            ap.error("--capture-merged-state requires a no-seed team, --record-trajectory, and excludes repair flags")
        args.skip_eval = True
    if args.collect_source_conflicts and not args.stop_after_repair_checkpoint:
        ap.error("--collect-source-conflicts requires --stop-after-repair-checkpoint")
    if args.only_first_repair_checkpoint and not args.stop_after_repair_checkpoint:
        ap.error("--only-first-repair-checkpoint requires --stop-after-repair-checkpoint")
    if args.stop_after_repair_checkpoint:
        if not (args.checkpoint_repair and args.repair_integrator and args.team_only) or args.solo_only:
            ap.error("--stop-after-repair-checkpoint requires --checkpoint-repair, --repair-integrator and --team-only")
        args.skip_eval = True
    if args.no_implementation_coordinator and not args.coordinator:
        ap.error("--no-implementation-coordinator requires --coordinator")
    if args.deterministic_coordinator and (not args.coop_tools or not args.no_seed):
        ap.error("--deterministic-coordinator requires --coop-tools and --no-seed")
    agent_config = None
    decision_model_options = dict(
        model=args.agent_gate_model, base_url=args.agent_gate_base_url, api_key_env=args.agent_gate_api_key_env,
        wake_threshold=args.agent_gate_threshold, timeout_s=args.agent_gate_timeout,
    )
    if args.agent_coordinator:
        if not (args.coop_tools and args.no_seed and args.git_share) or args.solo_only:
            ap.error("--agent-coordinator requires a team with --coop-tools, --no-seed, and --git-share")
        if (
            args.team_roles or args.adaptive or args.decompose or args.best_of_n != 1 or args.contract_first
            or args.allow_spawn or args.task_board or args.claim_mode or args.wait_protocol
        ):
            ap.error(
                "--agent-coordinator excludes --team-roles, --adaptive, --decompose, --best-of-n other than 1, "
                "--contract-first, --allow-spawn, --task-board, --claim-mode, and --wait-protocol"
            )
        if args.agent_wake_budget < 0:
            ap.error("--agent-wake-budget must be nonnegative")
        gate = GateConfig(
            policy=args.agent_gate, wake_budget=args.agent_wake_budget, random_seed=args.agent_random_seed, **decision_model_options,
        )
        try:
            gate.validate()
        except ValueError as exc:
            ap.error(str(exc))
        if args.agent_gate == "random" and not args.agent_random_match:
            ap.error("random gate requires a matched agent_main result (--agent-random-match)")
        agent_config = AgentCoordinatorConfig(gate=gate, opening=args.agent_opening, spec_check=not args.no_agent_spec_check)
    elif (
        args.agent_gate != "model" or any(value is not None for value in decision_model_options.values())
        or args.agent_wake_budget != 8 or args.agent_opening != "draft" or args.no_agent_spec_check
        or args.agent_random_match is not None or args.agent_random_seed != 0
    ):
        ap.error("agent coordinator flags require --agent-coordinator")
    if args.worker_reminder_interval < 0:
        ap.error("Worker reminder interval must be nonnegative")
    if args.planning_step_limit < 1 or args.planning_time_limit < 1:
        ap.error("Planning budgets must be positive")
    if args.coordination_variant == "human-prompt" and (not args.coordinator or args.no_coordinator_notebook):
        ap.error("human-prompt requires --coordinator with notebook enabled")

    if args.coordination_mode != "direct" and (
        not args.coordinator
        or args.no_coordinator_notebook
        or not args.coop_tools
        or not args.no_seed
        or args.wait_protocol
        or args.max_agents != 2
    ):
        ap.error("group coordination requires two coop-tools no-seed workers, coordinator, notebook, and no wait-protocol")
    if args.coordination_mode == "group-chat-planning" and (args.task_board or args.claim_mode or args.allow_spawn):
        ap.error("opening planning does not support task-board, claim-mode, or helpers")
    if args.pairs:
        items = []
        for spec in args.pairs:
            repo, task, feats = spec.split(":")
            items.append(WorkItem(repo=repo, task_id=int(task), features=[int(x) for x in feats.split(",")]))
    else:
        items = load_subset(args.subset)
    if args.limit is not None:
        items = items[: args.limit]

    if args.initial_plan_dir is not None:
        if args.coordination_mode != "group-chat" or not args.team_only:
            ap.error("Initial plan injection requires group-chat and --team-only")
        if args.reverse_order or args.task_board or args.claim_mode or args.allow_spawn:
            ap.error("Initial plan injection requires the original two-worker roster")
        # Validate all selected bundles before any pair can call a model.
        for item in items:
            pair = f"{item.repo}:{item.task_id}:{','.join(map(str, sorted(item.features)))}"
            plan = InitialPlan.load(initial_plan_path(args.initial_plan_dir, pair), expected_pair=pair)
            roster = [Assignment(agent_id=f"agent{i+1}", role="member", feature_id=f, task=read_feature(item.repo, item.task_id, f))
                      for i, f in enumerate(sorted(item.features))]
            plan.validate_assignments(item.repo, item.task_id, roster)

    logs_dir = Path(args.log_dir).resolve()
    configs = {}
    if agent_config is not None:
        for item in items:
            if len(item.features) < 2:
                ap.error("--agent-coordinator requires at least two feature assignments per pair")
            config = agent_config
            if config.gate.policy == "random":
                try:
                    config = replace(config, gate=_matched_gate(config.gate, logs_dir / args.agent_random_match, item))
                except ValueError as exc:
                    ap.error(f"{exc} for {item.repo}/{item.task_id} {sorted(item.features)}")
            configs[_pair_dir(logs_dir / args.team_name, "team", item)] = config

    def _result_exists(run_name: str, setting: str, item: WorkItem) -> bool:
        return (_pair_dir(logs_dir / run_name, setting, item) / "result.json").is_file()

    def do_pair(item):
        tag = f"{item.repo}/{item.task_id} {sorted(item.features)}"
        t0 = time.time()
        s = {"duration": 0.0, "steps": 0, "tokens": 0}
        t = {"duration": 0.0, "steps": 0, "tokens": 0}
        solo_skip = args.resume and not args.team_only and _result_exists(args.solo_name, "solo", item)
        team_skip = args.resume and not args.solo_only and _result_exists(args.team_name, "team", item)
        if solo_skip and (args.solo_only or team_skip):
            print(f"  skip (done) {tag}", flush=True)
            return (item, s, t, 0.0)
        try:
            if not args.team_only and not solo_skip:
                s = run_solo(item, run_name=args.solo_name, logs_dir=logs_dir, step_limit=args.step_limit,
                             agent_time_limit=args.agent_time_limit or None)
            if not args.solo_only and not team_skip:
                t = run_team(
                    item,
                    run_name=args.team_name,
                    logs_dir=logs_dir,
                    step_limit=args.step_limit,
                    agent_time_limit=args.agent_time_limit or None,
                    max_agents=args.max_agents,
                    verify_fix=args.verify_fix,
                    spec_fidelity=args.spec_fidelity,
                    teammate_context=args.teammate_context,
                    guard_git=args.guard_git,
                    seed_prior=not args.no_seed,
                    reverse_order=args.reverse_order,
                    completeness_review=args.completeness_review,
                    tdd_preamble=args.tdd_preamble,
                    mine_conventions=args.mine_conventions,
                    best_of_n=args.best_of_n,
                    decompose=args.decompose,
                    preserve_invariants=args.preserve_invariants,
                    adaptive=args.adaptive,
                    do_no_harm=args.do_no_harm,
                    select=args.select,
                    diversity_temp=args.diversity_temp,
                    coop_tools=args.coop_tools,
                    repair_integrator=args.repair_integrator,
                    repair_steps=args.repair_steps,
                    contract_first=args.contract_first,
                    live_awareness=args.live_awareness,
                    behavioral_gate=args.behavioral_gate,
                    tool_protocol=args.tool_protocol,
                    task_board=args.task_board,
                    wait_protocol=args.wait_protocol,
                    git_share=args.git_share,
                    team_roles=args.team_roles,
                    langfuse=args.langfuse,
                    coordinator=args.coordinator,
                    deterministic_coordinator=args.deterministic_coordinator,
                    agent_coordinator=args.agent_coordinator,
                    agent_coordinator_config=configs.get(_pair_dir(logs_dir / args.team_name, "team", item)),
                    coordinator_notebook=not args.no_coordinator_notebook,
                    coordination_variant=args.coordination_variant,
                    coordination_mode=args.coordination_mode,
                    coordinator_implementation_enabled=not args.no_implementation_coordinator,
                    initial_plan_dir=args.initial_plan_dir,
                    planning_step_limit=args.planning_step_limit,
                    planning_time_limit=args.planning_time_limit,
                    worker_reminder_interval=args.worker_reminder_interval,
                    focused_repair=args.focused_repair,
                    repair_time=args.repair_time,
                    apply_merge=args.apply_merge,
                    claim_mode=args.claim_mode,
                    allow_spawn=args.allow_spawn,
                    n_agents=args.agents,
                    completion_gate=args.completion_gate,
                    presub_merge=args.presub_merge,
                    repair_attempts=args.repair_attempts,
                    record_trajectory=args.record_trajectory,
                    checkpoint_repair=args.checkpoint_repair,
                    capture_merged_state=args.capture_merged_state,
                    persist_worker_patches=args.persist_worker_patches,
                    stop_after_repair_checkpoint=args.stop_after_repair_checkpoint,
                    only_first_repair_checkpoint=args.only_first_repair_checkpoint,
                    collect_source_conflicts=args.collect_source_conflicts,
                )
        except Exception as e:  # noqa: BLE001 - one bad pair (e.g. missing/arch-incompatible image) must not abort the run
            print(f"  SKIP {tag}: {type(e).__name__}: {str(e)[:160]}", flush=True)
            return None
        print(f"  done {tag}: solo {s['duration']:.0f}s | team {t['duration']:.0f}s", flush=True)
        return (item, s, t, time.time() - t0)

    print(f"running {len(items)} pair(s) x2 arms, concurrency={args.concurrency}...", flush=True)
    if args.concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            rows = list(ex.map(do_pair, items))
    else:
        rows = [do_pair(item) for item in items]

    rows = [r for r in rows if r is not None]  # drop skipped pairs (bad/incompatible images)
    if not rows or len(rows) != len(items):
        raise RuntimeError(f"Incomplete generation: {len(rows)}/{len(items)} pairs completed")
    if args.skip_eval:
        print("generation complete; official evaluation not run")
        _write_summaries(logs_dir, items, [
            (setting, name, None, None)
            for enabled, setting, name in (
                (not args.team_only, "solo", args.solo_name), (not args.solo_only, "team", args.team_name)
            )
            if enabled
        ])
        return

    print("\nscoring with CooperBench (this builds/pulls images + runs tests)...")
    for enabled, name in ((not args.team_only, args.solo_name), (not args.solo_only, args.team_name)):
        if enabled:
            evaluation = run_eval(name, logs_dir=logs_dir, backend=os.getenv("COOPER_RUNTIME", "docker"),
                                  force=True, concurrency=args.eval_concurrency)
            print(evaluation.stdout or "", flush=True)
            print(evaluation.stderr or "", flush=True)
            evaluation.check_returncode()

    print("\n================ RESULTS ================")
    solo_pass = team_pass = solo_feats = team_feats = 0
    for item, s, t, _ in rows:
        sp = None if args.team_only else read_eval(logs_dir, args.solo_name, "solo", item)
        tp = None if args.solo_only else read_eval(logs_dir, args.team_name, "team", item)
        solo_pass += int(bool(sp and sp[0]))
        team_pass += int(bool(tp and tp[0]))
        solo_feats += sp[1] if sp else 0
        team_feats += tp[1] if tp else 0
        tag = f"{item.repo}/{item.task_id} {sorted(item.features)}"
        ss = "PASS" if sp and sp[0] else (f"{sp[1]}/2 " if sp else "none")
        ts = "PASS" if tp and tp[0] else (f"{tp[1]}/2 " if tp else "none")
        print(f"  {tag:<45} solo={ss:<4} ({s['duration']:.0f}s)  team={ts:<4} ({t['duration']:.0f}s)")
    n = len(rows)
    print(f"\n  pass-rate:  solo {solo_pass}/{n}   team {team_pass}/{n}")
    print(f"  features :  solo {solo_feats}/{2 * n}   team {team_feats}/{2 * n}")
    print(f"  avg time :  solo {sum(r[1]['duration'] for r in rows) / n:.0f}s   team {sum(r[2]['duration'] for r in rows) / n:.0f}s")
    _write_summaries(logs_dir, items, [
        (setting, name, passed, features)
        for enabled, setting, name, passed, features in (
            (not args.team_only, "solo", args.solo_name, solo_pass, solo_feats),
            (not args.solo_only, "team", args.team_name, team_pass, team_feats),
        )
        if enabled
    ])


if __name__ == "__main__":
    main()
