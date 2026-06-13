"""Ablation report — the three metrics (success, efficiency, judge) per variant.

Compares a baseline against one or more candidate runs on the fixed pair set
and prints a table of the three headline metrics, plus a keep/drop verdict for
each candidate vs the baseline.

    uv run python scripts/ablation_report.py \
        --baseline cmp --candidates s8 s2 combo --setting team --judge
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cooperagents.eval.dataset import WorkItem, load_subset
from cooperagents.eval.judge import LLMJudge
from cooperagents.eval.scorecard import compare, load_scorecard, summarize

FIXED_PAIRS = [
    ("dottxt_ai_outlines_task", 1655, [6, 7]),
    ("dottxt_ai_outlines_task", 1655, [7, 10]),
    ("dottxt_ai_outlines_task", 1706, [4, 6]),
    ("dottxt_ai_outlines_task", 1706, [5, 8]),
    ("dspy_task", 8394, [3, 4]),
    ("dspy_task", 8394, [3, 5]),
    ("go_chi_task", 26, [1, 2]),
    ("go_chi_task", 56, [1, 5]),
    ("huggingface_datasets_task", 3997, [2, 4]),
    ("huggingface_datasets_task", 6252, [4, 6]),
]


def _load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="baseline run label (without -setting suffix)")
    ap.add_argument("--candidates", nargs="+", required=True, help="candidate run labels")
    ap.add_argument("--setting", default="team", choices=["team", "solo"])
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--judge", action="store_true", help="compute the absolute judge metric (needs API)")
    ap.add_argument("--all-flash", action="store_true")
    args = ap.parse_args()

    items = load_subset("flash") if args.all_flash else [WorkItem(r, t, f) for r, t, f in FIXED_PAIRS]
    judge = LLMJudge() if args.judge else None

    labels = [args.baseline, *args.candidates]
    cards = {lbl: load_scorecard(args.log_dir, f"{lbl}-{args.setting}", args.setting, items) for lbl in labels}
    metrics = {lbl: summarize(cards[lbl], judge=judge) for lbl in labels}

    jhead = "  judge/5" if args.judge else ""
    print(f"\nThree metrics on {len(items)} pairs (setting={args.setting})\n")
    print(f"  {'variant':<14} {'SUCCESS':>10} {'features':>9} {'EFFIC(s)':>9} {'steps':>7}{jhead}")
    print("  " + "-" * (60 + (9 if args.judge else 0)))
    for lbl in labels:
        m = metrics[lbl]
        tag = "baseline" if lbl == args.baseline else lbl
        jstr = f"  {m.judge:>6.2f}" if (args.judge and m.judge is not None) else ""
        n = cards[lbl].n
        npass = int(round(m.success * n))
        succ = f"{m.success * 100:>6.0f}% {npass}/{n}"
        print(f"  {tag:<14} {succ:>10} {m.feature_rate * 100:>8.0f}% {m.avg_duration:>8.0f} {m.avg_steps:>7.0f}{jstr}")

    print("\n  Verdicts vs baseline:")
    for lbl in args.candidates:
        v = compare(cards[args.baseline], cards[lbl], judge=judge)
        print(f"    {lbl:<12} {v.decision.upper():<13} {v.rationale}")


if __name__ == "__main__":
    main()
