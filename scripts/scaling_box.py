#!/usr/bin/env python
"""Run the cooperagents model-sweep on a non-code benchmark.

Produces, per model, the solo (SAS) and team success rates that form the
cooperagents box on a Scaling-Agent-Systems benchmark panel, and prints the
scorer-version-robust relative solo->team delta.

Every task is captured durably: as soon as an instance finishes, one JSON line
(success, steps, cost, duration, scorer detail, submission excerpt, trajectory
pointer) is appended and fsync'd to ``<out-dir>/tasks.jsonl``, so a crash or a
machine sleep mid-sweep never loses completed work. Full trajectories are
written under ``<out-dir>/trajectories/`` for inspecting measurement faults.

Examples (zsh):
  uv run python scripts/scaling_box.py --benchmark plancraft --split val.small --limit 20
  uv run python scripts/scaling_box.py --benchmark workbench --split email --models gemini/gemini-2.5-flash
  COOPER_MODELS="gemini/gemini-2.5-flash,gemini/gemini-2.5-pro" uv run python scripts/scaling_box.py --benchmark plancraft
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_ROOT = Path(__file__).resolve().parents[1]
_DOTENV = _ROOT / ".env"
if _DOTENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_DOTENV, override=False)
    except ImportError:
        pass

from cooperagents.benchmarks import get_benchmark  # noqa: E402
from cooperagents.benchmarks.runner import box_sweep  # noqa: E402
from cooperagents.llm import LiteLLMClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="cooperagents non-code benchmark model-sweep")
    ap.add_argument(
        "--benchmark",
        required=True,
        choices=["plancraft", "plancraft-coop", "workbench", "browsecomp", "browsecomp-coop", "finance"],
    )
    ap.add_argument("--split", default="val.small", help="benchmark split (plancraft: val.small; workbench: email/calendar/...)")
    ap.add_argument("--models", default=os.getenv("COOPER_MODELS", "gemini/gemini-2.5-flash"), help="comma-separated litellm model ids")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of task instances")
    ap.add_argument("--team-size", type=int, default=3)
    ap.add_argument("--step-limit", type=int, default=30)
    ap.add_argument("--out-dir", default=None, help="durable capture directory (default runs/scaling/<benchmark>_<split>_<ts>)")
    ap.add_argument("--out", default=None, help="summary JSON path (default <out-dir>/summary.json)")
    ap.add_argument("--no-save-trajectories", action="store_true", help="skip writing per-task trajectory files")
    ap.add_argument("--resume", default=None, help="resume an existing capture directory: reuse its finished tasks, run only the rest")
    ap.add_argument("--no-coordinator", action="store_true", help="disable the team coordinator (loop/stall nudges)")
    ap.add_argument("--no-task-board", action="store_true", help="disable the team task-board protocol")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    bench = get_benchmark(args.benchmark)

    cfg = {
        "benchmark": args.benchmark,
        "split": args.split,
        "models": sorted(models),
        "limit": args.limit,
        "team_size": args.team_size,
        "step_limit": args.step_limit,
    }

    completed: dict[str, dict] = {}
    if args.resume:
        out_dir = Path(args.resume)
        cfg_path = out_dir / "run_config.json"
        if not cfg_path.exists():
            raise SystemExit(f"--resume: {cfg_path} not found (not a capture directory)")
        prior = json.loads(cfg_path.read_text())
        # `limit` is excluded from the match: it only selects HOW MANY instances
        # run, never how an individual task executes, so a task finished under a
        # smaller limit is validly reused when the limit is raised (e.g. extending
        # a cap-30 run to the full split). Every other field must match, since it
        # changes per-task execution and would mix incomparable results.
        cmp_keys = [k for k in cfg if k != "limit"]
        prior_cfg = {k: prior.get(k) for k in cmp_keys}
        prior_cfg["models"] = sorted(prior_cfg.get("models") or [])
        cur_cfg = {k: cfg[k] for k in cmp_keys}
        if prior_cfg != cur_cfg:
            raise SystemExit(
                "--resume: run configuration differs from the prior run; refusing to mix "
                f"incomparable results.\n  prior:   {prior_cfg}\n  current: {cur_cfg}"
            )
        tasks_path = out_dir / "tasks.jsonl"
        if tasks_path.exists():
            for line in tasks_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn final line from a hard kill
                key = f"{rec['model']}\x1f{rec['mode']}\x1f{rec['instance_id']}"
                completed[key] = rec
        print(f"resuming {out_dir}: {len(completed)} finished tasks will be reused")
        traj_dir = out_dir / "trajectories"
        if not args.no_save_trajectories:
            traj_dir.mkdir(exist_ok=True)
        tasks_fp = tasks_path.open("a")  # APPEND — never truncate prior results
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = Path(args.out_dir or _ROOT / "runs" / "scaling" / f"{args.benchmark}_{args.split}_{ts}")
        out_dir.mkdir(parents=True, exist_ok=True)
        traj_dir = out_dir / "trajectories"
        if not args.no_save_trajectories:
            traj_dir.mkdir(exist_ok=True)
        tasks_path = out_dir / "tasks.jsonl"
        tasks_fp = tasks_path.open("w")
        (out_dir / "run_config.json").write_text(json.dumps({**cfg, "models": models, "started": ts}, indent=2))

    def sink(rec: dict) -> None:
        """Durable per-task capture: dump the integrated and per-agent trajectories
        to side files, append a compact record to tasks.jsonl, and fsync so a
        sleep/crash cannot lose it."""
        rec = dict(rec)
        traj = rec.pop("trajectory", "")
        if traj and not args.no_save_trajectories:
            fn = traj_dir / f"{rec['mode']}__{rec['instance_id']}__{rec['index']}.txt"
            fn.write_text(traj)
            rec["trajectory_file"] = str(fn.relative_to(out_dir))
        # Per-agent trajectories (team arms): write each agent's own log to a file
        # and keep only line counts + a pointer in the JSONL so it stays compact.
        agents = rec.get("agents")
        if isinstance(agents, list) and not args.no_save_trajectories:
            compact = []
            for a in agents:
                a = dict(a)
                atraj = a.pop("trajectory", "")
                if atraj:
                    afn = traj_dir / f"{rec['mode']}__{rec['instance_id']}__{rec['index']}__{a['agent_id']}.txt"
                    afn.write_text(atraj)
                    a["trajectory_file"] = str(afn.relative_to(out_dir))
                compact.append(a)
            rec["agents"] = compact
        tasks_fp.write(json.dumps(rec) + "\n")
        tasks_fp.flush()
        os.fsync(tasks_fp.fileno())

    def make_llm(model: str) -> LiteLLMClient:
        # State benchmarks are not code tasks; give a neutral, team/tool-framed
        # system prompt instead of the default software-engineer one so the agent
        # engages the task's own tools (including shared-notebook coordination).
        system = (
            "You are an autonomous agent working as part of a team to accomplish a task. "
            "Use ONLY the tools listed to make progress; when shared/coordination tools are "
            "available, use them to exchange information with teammates and converge on a "
            "shared result. At each step respond with a SINGLE JSON object "
            '{"thought": "...", "tool": "<name>", "args": {...}} and nothing else.'
        )
        return LiteLLMClient(model, system_prompt=system)

    print(f"capturing to {out_dir}")
    try:
        results = box_sweep(
            bench,
            models=models,
            make_llm=make_llm,
            split=args.split,
            limit=args.limit,
            team_size=args.team_size,
            step_limit=args.step_limit,
            record_fn=sink,
            completed=completed,
            coordinator=not args.no_coordinator,
            task_board=not args.no_task_board,
        )
    finally:
        tasks_fp.close()

    def _slim(rec: dict) -> dict:
        """Drop the heavy trajectory text (top-level and per-agent) from a record
        for the summary; the full logs live in the trajectories/ side files."""
        out = {k: v for k, v in rec.items() if k != "trajectory"}
        if isinstance(out.get("agents"), list):
            out["agents"] = [{k: v for k, v in a.items() if k != "trajectory"} for a in out["agents"]]
        return out

    rows = [
        {
            "model": r.model,
            "mode": r.mode,
            "success_rate": r.success_rate,
            "n": r.n,
            "records": [_slim(rec) for rec in r.records],
        }
        for r in results
    ]

    by_model: dict[str, dict[str, float]] = {}
    for r in results:
        by_model.setdefault(r.model, {})[r.mode] = r.success_rate

    lines = [f"=== {args.benchmark} ({args.split}) ==="]
    for model, d in by_model.items():
        parts = [f"{k} {v:.3f}" for k, v in d.items()]
        line = f"{model}: " + "  ".join(parts)
        solo, iso, team = d.get("solo"), d.get("solo-iso"), d.get("team")
        if solo is not None and team is not None and solo > 0:
            line += f"  rel(vs solo) {100 * (team - solo) / solo:+.1f}%"
        if iso is not None and team is not None and iso > 0:
            line += f"  rel(vs solo-iso) {100 * (team - iso) / iso:+.1f}%"
        lines.append(line)
    report = "\n".join(lines)
    print(report)
    (out_dir / "results.txt").write_text(report + "\n")

    out = Path(args.out) if args.out else out_dir / "summary.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"wrote {out}")
    print(f"per-task records: {tasks_path}")


if __name__ == "__main__":
    main()
