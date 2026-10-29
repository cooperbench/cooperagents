"""Build an x86_64 SWE-bench grade JSONL from the official Verified dataset.

Reads the space-separated instance ids in ``$SWEB_IDS`` (empty = all 500) and
emits one grade row per id to stdout, with ``image`` rewritten to the canonical
``swebench/sweb.eval.x86_64.{key}`` tag (``key`` = id with ``__`` -> ``_1776_``).
On an x86_64 host this is the leaderboard-canonical grading arch and every
Verified instance has an image.

Usage:
  SWEB_IDS="pallets__flask-5014 sympy__sympy-22914" uv run python deploy/build_grade_set.py > data/swebench_grade_remote.jsonl
  SWEB_IDS="" uv run python deploy/build_grade_set.py > data/swebench_grade_full.jsonl
"""

from __future__ import annotations

import json
import os

from datasets import load_dataset


def main() -> None:
    ds = load_dataset(os.getenv("SWEBENCH_DATASET", "SWE-bench/SWE-bench_Verified"), split="test")
    want = set((os.getenv("SWEB_IDS", "") or "").split())
    for r in ds:
        if want and r["instance_id"] not in want:
            continue
        row = dict(r)
        key = row["instance_id"].replace("__", "_1776_")
        row["image"] = f"swebench/sweb.eval.x86_64.{key}:latest"
        print(json.dumps(row))


if __name__ == "__main__":
    main()
