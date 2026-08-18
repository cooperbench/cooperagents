"""Evaluate a candidate harness change against a baseline.

The DECIDE step of the self-improvement loop. Reads two prior runs (by label,
on the same fixed pair set), builds composite scorecards (pass-rate +
efficiency + LLM judge), and prints a keep/drop verdict.

    # objective only (no API):
    uv run python scripts/evaluate_improvement.py --baseline base-team --candidate s1-team
    # add the LLM judge (uses the Azure endpoint from .env):
    uv run python scripts/evaluate_improvement.py --baseline base-team --candidate s1-team --judge
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cooperagents.eval.dataset import WorkItem, load_subset
from cooperagents.eval.judge import LLMJudge
from cooperagents.eval.scorecard import compare, format_report, load_scorecard

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
    ap.add_argument("--baseline", required=True, help="baseline run label (without -solo/-team suffix)")
    ap.add_argument("--candidate", required=True, help="candidate run label")
    ap.add_argument("--setting", default="team", choices=["team", "solo"])
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--judge", action="store_true", help="run the LLM judge (needs API creds)")
    ap.add_argument("--all-flash", action="store_true", help="use the full flash set instead of the fixed 10")
    args = ap.parse_args()

    if args.all_flash:
        items = load_subset("flash")
    else:
        items = [WorkItem(repo=r, task_id=t, features=f) for r, t, f in FIXED_PAIRS]

    base = load_scorecard(args.log_dir, f"{args.baseline}-{args.setting}", args.setting, items)
    cand = load_scorecard(args.log_dir, f"{args.candidate}-{args.setting}", args.setting, items)
    judge = LLMJudge() if args.judge else None
    verdict = compare(base, cand, judge=judge)
    print(format_report(base, cand, verdict))


if __name__ == "__main__":
    main()
