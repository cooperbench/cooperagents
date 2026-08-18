"""ProgramBench runner: solo vs coordinator-team, adapter-driven.

All ProgramBench-specific behavior (task prompt, cleanroom environment,
fitness probes, completion gates, repair evidence, submission format) lives
in ``cooperagents.adapters.programbench.ProgramBenchAdapter``; this script
is the generic orchestration: build a team, run it, repair, package.
Retired arms and pre-adapter history: git log before commit 0e73bdf.

Arms
  solo       1 mini-swe agent + the mechanical tail.
  coopgit    N agents (default 2), same task, shared git remote.
  coopgitc2  coopgit + the coordinator (loop/stall/collision nudges).

Mechanism flags (each measured; record in docs/SEAM_BACKLOG.md)
  --repair            gate the integrated tree; on failure run up to 2
                      repair agents; submit the mechanically best candidate.
  --completion-gate   reject an agent's finish until the adapter's gate
                      passes in the agent's own container.
  --env-brief         prepend the adapter's environment brief to the task.
  --presub-merge      team gate = merge teammate branches first, gate the
                      MERGED tree; integration then SELECTS the best member
                      tree by adapter score() instead of re-merging.

Usage (full current stack):
  set -a; source .env.qwen; set +a
  .venv/bin/python scripts/bench_programbench.py \
      --instance abishekvashok__cmatrix.5c082c6 --arm coopgitc2 --rep r1 \
      --step-limit 1000 --repair --agent-time-limit 3600 \
      --completion-gate --env-brief --presub-merge [--team-size 3]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooperagents import verification
from cooperagents.adapters import get_adapter
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.docker import DockerEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.types import Assignment, TeamSpec

ADAPTER = get_adapter("programbench")


def _score(instance: str, patch: str) -> tuple:
    """Harness verification.score bound to this adapter's declared facts."""
    def env_factory():
        e = DockerEnv(ADAPTER.image(instance), **ADAPTER.env_kwargs())
        ADAPTER.setup_env(e, "score")
        return e
    return verification.score(ADAPTER.image(instance), patch, env_factory=env_factory,
                              build_artifact=ADAPTER.build_artifact,
                              reference_binary=ADAPTER.reference_binary)


def build_gate_and_repair(instance: str, patch: str, *, model: str, step_limit: int = 150,
                          time_limit_s: int = 2400, command_timeout: int = 300) -> tuple[str, dict]:
    """Generic repair tail: adapter gate -> repair agents -> mechanical pick.

    The repair agent's self-report is never trusted: every candidate
    (pre-repair included) is ranked by adapter score() and the max wins.
    """
    from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

    env = DockerEnv(ADAPTER.image(instance), **ADAPTER.env_kwargs())
    try:
        ADAPTER.setup_env(env, "integration")
        if patch.strip():
            env.write_file("/tmp/final.patch", patch)
            env.execute(
                "git apply --whitespace=nowarn /tmp/final.patch 2>/dev/null"
                " || git apply --3way /tmp/final.patch 2>/dev/null"
                " || git apply --reject /tmp/final.patch 2>/dev/null || true"
            )
        repair_prompt = verification.repair(
            env, build_artifact=ADAPTER.build_artifact,
            reference_binary=ADAPTER.reference_binary)
        if repair_prompt is None:
            return patch, {"repair": "not_needed"}
        meta = {"repair": "ran", "attempts": []}
        candidates = [patch]
        for attempt in range(2):
            res = run_mini_swe_agent(
                env,
                task=repair_prompt,
                agent_id=f"repair{attempt + 1}",
                role="integrator",
                model_name=model,
                step_limit=step_limit,
                cost_limit=5.0,
                command_timeout=command_timeout,
                time_limit_s=time_limit_s,
            )
            env.execute(r"find . -path ./.git -prune -o \( -name '*.rej' -o -name '*.orig' \) -print0 | xargs -0 -r rm -f")
            candidates.append(env.git_diff())
            repair_prompt = verification.repair(
            env, build_artifact=ADAPTER.build_artifact,
            reference_binary=ADAPTER.reference_binary)
            meta["attempts"].append({"steps": res.steps, "status": res.status,
                                     "gate_after": 0 if repair_prompt is None else "fail"})
            if repair_prompt is None:
                break
        scores = [_score(instance, c) for c in candidates]
        best = max(range(len(candidates)), key=lambda i: scores[i])
        meta["candidate_scores"] = scores
        meta["chosen"] = best
        return candidates[best], meta
    finally:
        env.cleanup()


def run_team_once(arm: str, instance: str, *, step_limit: int, agent_time_limit: int | None,
                  gate: bool = False, brief: str = "", presub_merge: bool = False,
                  team_size: int = 2):
    """One full run for `arm`; returns (integrated_patch, RunResult).

    Own run_id -> own bus and git-share volume, so concurrent calls are safe.
    """
    run_id = uuid.uuid4().hex[:8]
    n = 1 if arm == "solo" else team_size
    assignments = [
        Assignment(agent_id=f"agent{i+1}", role="lead" if i == 0 else "member",
                   feature_id=None,
                   task=brief + ADAPTER.task_for(instance, i, n))
        for i in range(n)
    ]
    spec = TeamSpec(
        run_id=run_id,
        repo=ADAPTER.name,
        task_id=0,
        features=[],
        assignments=assignments,
        shared_workspace=True,
        worker="mini_swe",
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Qwen/Qwen3.5-9B"),
        seed_prior=False,
        coop_tools=arm != "solo",
        agent_time_limit=agent_time_limit,
        git_share=arm != "solo",
        coordinator=arm == "coopgitc2",
        temperature=0.0,
        completion_gate=(lambda env, _m=presub_merge: verification.validate(
                            env, merged=_m, build_artifact=ADAPTER.build_artifact))
                        if (gate or presub_merge) else None,
        # With the merge gate on, every member tree already contains the
        # merged team work — select the best one instead of re-merging.
        select_integration=(lambda patches: max(range(len(patches)),
                            key=lambda i: _score(instance, patches[i]))) if presub_merge else None,
    )
    vols = [f"cbs{run_id}:/cbshared"] if spec.git_share else []

    def make_env(_id: str, _v=vols or None) -> DockerEnv:
        env = DockerEnv(ADAPTER.image(instance), volumes=_v, **ADAPTER.env_kwargs())
        ADAPTER.setup_env(env, _id)
        return env

    harness = UnifiedHarness(bus=InMemoryBus(run_id), step_limit=step_limit, command_timeout=300)
    res = harness.run(spec, env_factory=make_env)
    return (res.integrated.patch or ""), res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="abishekvashok__cmatrix.5c082c6")
    ap.add_argument("--arm", choices=["solo", "coopgit", "coopgitc2"], required=True)
    ap.add_argument("--rep", default="a")
    ap.add_argument("--step-limit", type=int, default=1000)
    ap.add_argument("--team-size", type=int, default=2,
                    help="number of agents in coop arms (scalability axis)")
    ap.add_argument("--agent-time-limit", type=int, default=0,
                    help="wall-clock cap (s) per agent; 0 = uncapped")
    ap.add_argument("--repair", action="store_true")
    ap.add_argument("--completion-gate", action="store_true")
    ap.add_argument("--env-brief", action="store_true")
    ap.add_argument("--presub-merge", action="store_true")
    ap.add_argument("--runs-dir", default="runs")
    args = ap.parse_args()

    run_name = f"pb-{args.arm}-{args.rep}"
    out_root = Path(args.runs_dir) / run_name / args.instance
    t0 = time.time()
    brief = ADAPTER.brief(args.instance) if args.env_brief else ""

    patch, res = run_team_once(args.arm, args.instance,
                               step_limit=args.step_limit,
                               agent_time_limit=args.agent_time_limit or None,
                               gate=args.completion_gate, brief=brief,
                               presub_merge=args.presub_merge,
                               team_size=args.team_size)

    repair_meta = {}
    if args.repair:
        patch, repair_meta = build_gate_and_repair(
            args.instance, patch, model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Qwen/Qwen3.5-9B"))
        print(f"[{run_name}] repair: {repair_meta}")
    dur = time.time() - t0
    print(f"[{run_name}] duration={dur:.0f}s steps={res.total_steps} patch_bytes={len(patch)}")
    if not patch.strip():
        print(f"[{run_name}] EMPTY PATCH — writing empty submission for the record")
    ADAPTER.submit(args.instance, patch, out_root)

    import json as _json
    traj_dir = out_root / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    for aid, r in res.seeds.items():
        (traj_dir / f"{aid}.json").write_text(_json.dumps({
            "agent_id": aid, "status": r.status, "steps": r.steps,
            "error": r.error, "messages": r.messages,
            "segments": r.segments,
        }, indent=1))
    (out_root / "integrated.patch").write_text(patch)
    (out_root / "metrics.json").write_text(_json.dumps({**res.metrics, **repair_meta}, indent=1, default=str))
    (out_root / "run_meta.txt").write_text(
        f"arm={args.arm} rep={args.rep} duration_s={dur:.0f} steps={res.total_steps} "
        f"patch_bytes={len(patch)}\n"
    )
    print(f"[{run_name}] submission at {out_root}/submission.tar.gz")


# Back-compat aliases for analysis snippets that import from this module.
image_for = ADAPTER.image
pb_score = _score


if __name__ == "__main__":
    main()
