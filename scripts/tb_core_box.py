"""Terminal-Bench-core box sweep: solo / solo-iso / team(N) over a curated
light subset, scored with the adapter's own pytest verifier.

Mirrors ``scripts/scaling_box.py``'s arm structure and durable-capture/resume,
but drives the Docker ADAPTER path (``TerminalBenchCoreAdapter``) instead of a
StateBenchmark. For each task it reuses ``bench_programbench.run_team_once`` as
the per-task engine (already adapter- and arm-aware), writes the agent's git
diff as ``<arm>/<instance>/integrated.patch`` via ``ADAPTER.submit``, then scores
every patch for an arm in a separate phase with ``ADAPTER.evaluate`` (fresh
container, ``git apply`` + the task's own pytest).

Arms (mirror box_sweep):
  solo       1 agent, ``step_limit`` steps.
  solo-iso   1 agent, ``step_limit * team_size`` steps (equal-total-compute control).
  team       ``team_size`` agents + coordinator + git-share; integration SELECTS
             the lead's non-empty tree (best-of-N). TB tasks are single-objective
             (every agent solves the whole task), so selection — not a 3-way merge
             of independent full solutions — is the correct team integrator.

Usage:
  uv run python scripts/tb_core_box.py --model sap/gemini-2.5-flash --team-size 3 \
      --step-limit 50 --out-dir runs/scaling/tbcore_light
  Add --resume <out-dir> to reuse finished agent runs and run only the rest.
  Add --tasks t1,t2 to override the curated subset (e.g. --tasks hello-world for a dry run).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

# Load SAP AI Core credentials from .env (same as scaling_box.py); never printed.
_DOTENV = _ROOT / ".env"
if _DOTENV.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_DOTENV, override=False)
    except Exception:  # noqa: BLE001 - dotenv optional; env may already be set
        pass

# Curated light subset: flat Dockerfiles on shared multi-arch base images, short
# timeouts, no x86 pins, no kernel/qemu/ML/large-download. Excludes the 5
# nested-layout tasks that ADAPTER.image() cannot build.
LIGHT_TASKS = [
    "hello-world",
    "fix-permissions",
    "fibonacci-server",
    "nginx-request-logging",
    "openssl-selfsigned-cert",
    "organization-json-generator",
    "heterogeneous-dates",
    "fix-git",
    "grid-pattern-transform",
    "sanitize-git-repo",
    "new-encrypt-command",
    "jupyter-notebook-server",
    "vim-terminal-task",
    "csv-to-parquet",
    "polyglot-c-py",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sap/gemini-2.5-flash")
    ap.add_argument("--tasks", default=None,
                    help="comma-separated task list; default = curated light subset")
    ap.add_argument("--arms", default="solo,solo-iso,team",
                    help="comma-separated subset of solo,solo-iso,team")
    ap.add_argument("--team-size", type=int, default=3)
    ap.add_argument("--step-limit", type=int, default=50)
    ap.add_argument("--agent-time-limit", type=int, default=600,
                    help="wall-clock cap (s) per agent; capped further by the task manifest")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--resume", default=None,
                    help="reuse an existing out-dir: skip tasks whose integrated.patch exists")
    args = ap.parse_args()

    # Resolve the adapter AFTER setting COOPER_BENCHMARK (bench_programbench binds
    # ADAPTER at import time) and pin the model for the mini-swe worker.
    os.environ["COOPER_BENCHMARK"] = "terminalbench-core"
    os.environ["AZURE_OPENAI_DEPLOYMENT"] = args.model

    import bench_programbench as bpb  # noqa: E402 - must follow the env setup above
    from cooperagents.adapters import get_adapter  # noqa: E402

    adapter = get_adapter("terminalbench-core")

    tasks = [t.strip() for t in args.tasks.split(",")] if args.tasks else list(LIGHT_TASKS)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    out_dir = Path(args.resume or args.out_dir
                   or _ROOT / "runs" / "scaling" / f"tbcore_{time.strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"benchmark": "terminalbench-core", "model": args.model, "tasks": tasks,
           "arms": arms, "team_size": args.team_size, "step_limit": args.step_limit}
    (out_dir / "run_config.json").write_text(json.dumps(cfg, indent=2))

    # Per-arm step limits (solo-iso gets the equal-total-compute budget).
    def arm_call(arm: str, inst: str):
        if arm == "solo":
            return dict(arm="solo", team_size=1, step_limit=args.step_limit, presub_merge=False)
        if arm == "solo-iso":
            return dict(arm="solo", team_size=1, step_limit=args.step_limit * args.team_size,
                        presub_merge=False)
        # team: coopgitc2 + best-of-N lead selection (presub_merge).
        return dict(arm="coopgitc2", team_size=args.team_size, step_limit=args.step_limit,
                    presub_merge=True)

    records: list[dict] = []
    for arm in arms:
        arm_dir = out_dir / arm
        arm_dir.mkdir(exist_ok=True)
        print(f"\n=== {args.model} | {arm} ({len(tasks)} tasks) ===", flush=True)
        for idx, inst in enumerate(tasks):
            inst_dir = arm_dir / inst
            patch_file = inst_dir / "integrated.patch"
            if patch_file.exists():
                # Agent run already done (resume): keep a record so the scoring
                # phase attaches a reward instead of dropping the task.
                records.append({"benchmark": "terminalbench-core", "model": args.model,
                                "mode": arm, "instance_id": inst, "index": idx,
                                "patch_bytes": patch_file.stat().st_size, "cached": True,
                                "error": None})
                print(f"  [{idx+1}/{len(tasks)}] {inst}  CACHED (agent run reused)", flush=True)
                continue
            kw = arm_call(arm, inst)
            atl = min(args.agent_time_limit, adapter.agent_timeout_s(inst))
            t0 = time.time()
            meta = {"benchmark": "terminalbench-core", "model": args.model, "mode": arm,
                    "instance_id": inst, "index": idx, "step_limit": kw["step_limit"],
                    "team_size": kw["team_size"]}
            try:
                patch, res = bpb.run_team_once(
                    kw["arm"], inst, step_limit=kw["step_limit"], agent_time_limit=atl,
                    presub_merge=kw["presub_merge"], team_size=kw["team_size"])
                adapter.submit(inst, patch or "", inst_dir)
                integ = getattr(res, "integrated", None)
                meta.update(duration_s=round(time.time() - t0, 1),
                            steps=getattr(integ, "steps", None),
                            cost=getattr(integ, "cost", None),
                            patch_bytes=len(patch or ""), error=None)
                print(f"  [{idx+1}/{len(tasks)}] {inst}  ran {meta['duration_s']}s "
                      f"patch={meta['patch_bytes']}B", flush=True)
            except Exception as e:  # noqa: BLE001 - keep the sweep going; record the failure
                inst_dir.mkdir(parents=True, exist_ok=True)
                (inst_dir / "integrated.patch").write_text("")  # empty → reward 0, still scored
                meta.update(duration_s=round(time.time() - t0, 1), steps=None, cost=None,
                            patch_bytes=0, error=f"{type(e).__name__}: {e}")
                print(f"  [{idx+1}/{len(tasks)}] {inst}  ERROR {meta['error']}", flush=True)
            records.append(meta)

        # Scoring phase: evaluate every patch in this arm dir (fresh containers).
        print(f"  scoring {arm} ...", flush=True)
        try:
            rewards = adapter.evaluate(arm_dir)
        except Exception as e:  # noqa: BLE001
            print(f"  [evaluate error] {type(e).__name__}: {e}", flush=True)
            rewards = {}
        for rec in records:
            if rec["mode"] == arm:
                r = rewards.get(rec["instance_id"])
                rec["success"] = bool(r and r >= 1.0)
                rec["reward"] = r

    # Durable capture + human-readable results, same shape as scaling_box.py.
    (out_dir / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    by_arm: dict[str, list[dict]] = {}
    for r in records:
        by_arm.setdefault(r["mode"], []).append(r)
    lines = [f"=== terminalbench-core ({len(tasks)} light tasks) ==="]
    rates = {}
    for arm in arms:
        rs = by_arm.get(arm, [])
        scored = [r for r in rs if r.get("reward") is not None]
        rate = sum(1 for r in scored if r["success"]) / len(scored) if scored else 0.0
        rates[arm] = rate
        lines.append(f"{args.model} {arm}: {rate:.3f}  (n={len(scored)}/{len(rs)})")
    solo, iso, team = rates.get("solo"), rates.get("solo-iso"), rates.get("team")
    if solo is not None and team is not None and solo > 0:
        lines.append(f"rel(team vs solo) {100 * (team - solo) / solo:+.1f}%")
    if iso is not None and team is not None and iso > 0:
        lines.append(f"rel(team vs solo-iso) {100 * (team - iso) / iso:+.1f}%")
    report = "\n".join(lines)
    print("\n" + report, flush=True)
    (out_dir / "results.txt").write_text(report + "\n")
    (out_dir / "summary.json").write_text(json.dumps(
        {"config": cfg, "rates": rates, "records": records}, indent=2))
    print(f"\nwrote {out_dir}/results.txt, summary.json, tasks.jsonl", flush=True)


if __name__ == "__main__":
    main()
