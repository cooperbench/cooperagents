#!/usr/bin/env python
"""Run the cooperagents model-sweep on a non-code benchmark.

Produces, per model, the solo (SAS) and team success rates that form the
cooperagents box on a Scaling-Agent-Systems benchmark panel, and prints the
scorer-version-robust relative solo->team delta.

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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cooperagents.benchmarks import get_benchmark  # noqa: E402
from cooperagents.benchmarks.runner import box_sweep  # noqa: E402
from cooperagents.llm import LiteLLMClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="cooperagents non-code benchmark model-sweep")
    ap.add_argument("--benchmark", required=True, choices=["plancraft", "workbench", "browsecomp", "finance"])
    ap.add_argument("--split", default="val.small", help="benchmark split (plancraft: val.small; workbench: email/calendar/...)")
    ap.add_argument("--models", default=os.getenv("COOPER_MODELS", "gemini/gemini-2.5-flash"), help="comma-separated litellm model ids")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of task instances")
    ap.add_argument("--team-size", type=int, default=3)
    ap.add_argument("--step-limit", type=int, default=30)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    bench = get_benchmark(args.benchmark)

    def make_llm(model: str) -> LiteLLMClient:
        return LiteLLMClient(model)

    results = box_sweep(
        bench,
        models=models,
        make_llm=make_llm,
        split=args.split,
        limit=args.limit,
        team_size=args.team_size,
        step_limit=args.step_limit,
    )

    rows = [{"model": r.model, "mode": r.mode, "success_rate": r.success_rate, "n": r.n} for r in results]
    by_model: dict[str, dict[str, float]] = {}
    for r in results:
        by_model.setdefault(r.model, {})[r.mode] = r.success_rate
    print(f"=== {args.benchmark} ({args.split}) ===")
    for model, d in by_model.items():
        solo, team = d.get("solo"), d.get("team")
        line = f"{model}: solo {solo:.3f}  team {team:.3f}" if solo is not None and team is not None else str(d)
        if solo is not None and team is not None and solo > 0:
            line += f"  rel {100 * (team - solo) / solo:+.1f}%"
        print(line)

    out = args.out or f"scaling_box_{args.benchmark}.json"
    Path(out).write_text(json.dumps(rows, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
