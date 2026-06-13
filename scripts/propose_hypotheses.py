"""Use the LLM analyst to propose NEW hypotheses from a run's failures.

uv run python scripts/propose_hypotheses.py --run iso-s2-team
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cooperagents.eval.analyst import gather_failures, propose_hypotheses
from cooperagents.eval.dataset import WorkItem

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
    ap.add_argument("--run", required=True, help="run label (e.g. iso-s2-team)")
    ap.add_argument("--setting", default="team")
    ap.add_argument("--log-dir", default="logs")
    args = ap.parse_args()

    items = [WorkItem(r, t, f) for r, t, f in FIXED_PAIRS]
    failures = gather_failures(args.log_dir, args.run, args.setting, items)
    print(f"analyzing {len(failures)} failed pair(s) from '{args.run}'...\n")
    out = propose_hypotheses(failures)

    print("=== failure diagnoses ===")
    for d in out["diagnoses"]:
        print(f"  {d.get('pair', '?'):<40} {d.get('failure_mode', '?'):<22} seam-addressable={d.get('seam_addressable')}")
    print("\n=== proposed new hypotheses ===")
    for i, p in enumerate(out["proposals"], 1):
        flag = "SEAM" if p.seam_addressable else "capability/spec"
        print(f"\nH-new{i} [{flag}] {p.hypothesis}")
        print(f"   why : {p.rationale}")
        print(f"   test: {p.test_plan}")
        if p.failure_modes:
            print(f"   targets: {', '.join(p.failure_modes)}")


if __name__ == "__main__":
    main()
