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
) -> dict:
    feats = sorted(item.features)
    if reverse_order:
        feats = list(reversed(feats))
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
    )
    harness = UnifiedHarness(bus=InMemoryBus(run_id), step_limit=step_limit, command_timeout=300)
    img = image_name(item.repo, item.task_id)
    selector = _judge_selector(spec_bundle) if best_of_n > 1 else None
    res = harness.run(spec, env_factory=lambda _id, _i=img: DockerEnv(_i), selector=selector)
    write_run_outputs(res, run_name=run_name, logs_dir=logs_dir, setting="team", model=MODEL)
    return {"duration": res.duration_seconds, "steps": res.total_steps, "tokens": 0, "helpers": len(res.helpers)}


def read_eval(logs_dir: Path, run_name: str, setting: str, item: WorkItem) -> bool | None:
    feature_str = "_".join(f"f{f}" for f in sorted(item.features))
    p = logs_dir / run_name / setting / item.repo / str(item.task_id) / feature_str / "eval.json"
    if not p.is_file():
        return None
    return bool(json.loads(p.read_text()).get("both_passed"))


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
    ap.add_argument("--decompose", action="store_true", help="G1+G2+G3: planner cuts an independence-maximizing subtask DAG, run in parallel")
    ap.add_argument("--preserve-invariants", action="store_true", help="C1: each agent publishes a regression check; later agents must keep all prior checks green")
    ap.add_argument("--team-only", action="store_true", help="skip the solo arm (reuse an existing solo baseline)")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--solo-name", default="cmp-solo")
    ap.add_argument("--team-name", default="cmp-team")
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
        if not args.team_only:
            s = run_solo(item, run_name=args.solo_name, logs_dir=logs_dir, step_limit=args.step_limit)
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
        )
        print(f"  done {tag}: solo {s['duration']:.0f}s | team {t['duration']:.0f}s", flush=True)
        return (item, s, t, time.time() - t0)

    print(f"running {len(items)} pair(s) x2 arms, concurrency={args.concurrency}...", flush=True)
    if args.concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            rows = list(ex.map(do_pair, items))
    else:
        rows = [do_pair(item) for item in items]

    print("\nscoring with CooperBench (this builds/pulls images + runs tests)...")
    if not args.team_only:
        run_eval(args.solo_name, logs_dir=logs_dir, backend="docker", force=True, concurrency=args.eval_concurrency)
    run_eval(args.team_name, logs_dir=logs_dir, backend="docker", force=True, concurrency=args.eval_concurrency)

    print("\n================ RESULTS ================")
    solo_pass = team_pass = 0
    for item, s, t, _ in rows:
        sp = None if args.team_only else read_eval(logs_dir, args.solo_name, "solo", item)
        tp = read_eval(logs_dir, args.team_name, "team", item)
        solo_pass += int(bool(sp))
        team_pass += int(bool(tp))
        tag = f"{item.repo}/{item.task_id} {sorted(item.features)}"
        ss, ts = ("PASS" if sp else "fail"), ("PASS" if tp else "fail")
        print(f"  {tag:<45} solo={ss:<4} ({s['duration']:.0f}s)  team={ts:<4} ({t['duration']:.0f}s)")
    n = len(rows)
    print(f"\n  pass-rate:  solo {solo_pass}/{n}   team {team_pass}/{n}")
    print(f"  avg time :  solo {sum(r[1]['duration'] for r in rows) / n:.0f}s   team {sum(r[2]['duration'] for r in rows) / n:.0f}s")


if __name__ == "__main__":
    main()
