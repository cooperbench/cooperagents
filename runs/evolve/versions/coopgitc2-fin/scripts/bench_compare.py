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
import json
import os
import time
import uuid
from pathlib import Path

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.docker import DockerEnv
from cooperagents.eval.cooperbench import run_eval, write_run_outputs
from cooperagents.eval.dataset import WorkItem, image_name, load_subset, read_feature
from cooperagents.harness import UnifiedHarness
from cooperagents.types import Assignment, TeamSpec


def _load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao")


def run_solo(item: WorkItem, *, run_name: str, logs_dir: Path, step_limit: int) -> dict:
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
        max_agents=1,
        allow_spawn=False,
        shared_workspace=True,
        worker="mini_swe",
        model=MODEL,
    )
    harness = UnifiedHarness(bus=InMemoryBus(run_id), step_limit=step_limit, command_timeout=300)
    img = image_name(item.repo, item.task_id)
    res = harness.run(spec, env_factory=lambda _id, _i=img: DockerEnv(_i))
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
                env = DockerEnv(img)
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


def run_team(
    item: WorkItem,
    *,
    run_name: str,
    logs_dir: Path,
    step_limit: int,
    max_agents: int,
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
    focused_repair: bool = False,
    repair_time: int = 0,
    apply_merge: bool = False,
    claim_mode: bool = False,
    allow_spawn: bool = False,
    n_agents: int = 2,
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
        focused_repair=focused_repair,
        repair_time_limit=repair_time or None,
        apply_chain_merge=apply_merge,
        claim_mode=claim_mode,
        allow_spawn_tool=allow_spawn,
    )
    harness = UnifiedHarness(bus=InMemoryBus(run_id), step_limit=step_limit, command_timeout=300)
    img = image_name(item.repo, item.task_id)
    selector = None
    if best_of_n > 1:
        selector = _mechanical_selector(image_name(item.repo, item.task_id)) if select == "mechanical" else _judge_selector(spec_bundle)
    vols = ([f"cbs{run_id}:/cbshared"] if git_share else []) + ([f"cbt{run_id}:/workspace/shared"] if team_roles else [])
    vols = vols or None
    res = harness.run(spec, env_factory=lambda _id, _i=img, _v=vols: DockerEnv(_i, volumes=_v), selector=selector)
    write_run_outputs(res, run_name=run_name, logs_dir=logs_dir, setting="team", model=MODEL)
    return {"duration": res.duration_seconds, "steps": res.total_steps, "tokens": 0, "helpers": len(res.helpers)}


def read_eval(logs_dir: Path, run_name: str, setting: str, item: WorkItem) -> tuple[bool, int] | None:
    """Return (both_passed, n_features_passed) for a scored pair, or None if unscored.

    Feature-level counts double the measurement resolution — essential in
    low-pass-rate regimes (small models) where pair-level deltas drown in noise.
    """
    feature_str = "_".join(f"f{f}" for f in sorted(item.features))
    p = logs_dir / run_name / setting / item.repo / str(item.task_id) / feature_str / "eval.json"
    if not p.is_file():
        return None
    e = json.loads(p.read_text())
    nfeat = sum(1 for k in ("feature1", "feature2") if (e.get(k) or {}).get("passed"))
    return bool(e.get("both_passed")), nfeat


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2)
    ap.add_argument("--pairs", nargs="*", default=None, help="repo:task:f1,f2 ...")
    ap.add_argument("--max-agents", type=int, default=3)
    ap.add_argument("--step-limit", type=int, default=30)
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
    ap.add_argument("--coordinator", action="store_true",
                    help="C2: live monitor — mechanical loop/stall/collision triggers, LLM-composed nudges via poller")
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
    ap.add_argument("--live-awareness", action="store_true", help="TK2/Q9: push '[team] X is editing ...' notes into each agent's context")
    ap.add_argument("--repair-steps", type=int, default=25, help="step cap for the Q5 merge-repair agent")
    ap.add_argument("--repair-integrator", action="store_true",
                    help="Q5: health-gate the no-seed merge; run one repair agent only when broken")
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
    args = ap.parse_args()

    if args.pairs:
        items = []
        for spec in args.pairs:
            repo, task, feats = spec.split(":")
            items.append(WorkItem(repo=repo, task_id=int(task), features=[int(x) for x in feats.split(",")]))
    else:
        items = load_subset("flash")[: args.limit]

    logs_dir = Path(args.log_dir).resolve()

    def do_pair(item):
        tag = f"{item.repo}/{item.task_id} {sorted(item.features)}"
        t0 = time.time()
        s = {"duration": 0.0, "steps": 0, "tokens": 0}
        try:
            if not args.team_only:
                s = run_solo(item, run_name=args.solo_name, logs_dir=logs_dir, step_limit=args.step_limit)
            t = {"duration": 0.0, "steps": 0, "tokens": 0}
            if not args.solo_only:
                t = run_team(
                item,
                run_name=args.team_name,
                logs_dir=logs_dir,
                step_limit=args.step_limit,
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
                coordinator=args.coordinator,
                focused_repair=args.focused_repair,
                repair_time=args.repair_time,
                apply_merge=args.apply_merge,
                claim_mode=args.claim_mode,
                allow_spawn=args.allow_spawn,
                n_agents=args.agents,
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
    if not rows:
        print("no pairs completed.")
        return

    print("\nscoring with CooperBench (this builds/pulls images + runs tests)...")
    if not args.team_only:
        run_eval(args.solo_name, logs_dir=logs_dir, backend="docker", force=True, concurrency=args.eval_concurrency)
    if not args.solo_only:
        run_eval(args.team_name, logs_dir=logs_dir, backend="docker", force=True, concurrency=args.eval_concurrency)

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


if __name__ == "__main__":
    main()
