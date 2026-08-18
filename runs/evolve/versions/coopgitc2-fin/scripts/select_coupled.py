"""Select a COORDINATION-SENSITIVE benchmark set from CooperBench gold patches.

The fixed 10-pair flash set plateaus at ~50% across four seam mechanism families
(see docs/SEAM_BACKLOG.md). One hypothesis: that set isn't where coordination
*matters*. This script ranks feature pairs by how much their GOLD solutions
overlap — shared files (weight 2) + shared changed symbols (weight 1) — i.e. how
coupled the two features are. High-coupling pairs are where the team must reuse
each other's APIs and avoid conflicts, so they are the regime where coordination
seams (S2 teammate-context, S3 interface contracts, S1 region partitioning)
should pay off if they ever do.

Note: CooperBench's gold_conflict_report.json has a binary has_conflict flag, but
it is too coarse (74% of all pairs "conflict", and it misses file overlap that
merges cleanly). This graded score is the sharper instrument.

Usage: uv run python scripts/select_coupled.py [lite|flash|core] [n] [cap_per_repo]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DS = Path("CooperBench/dataset")


def patch_files_syms(p: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    syms: set[str] = set()
    if not p.is_file():
        return files, syms
    txt = p.read_text(errors="ignore")
    for m in re.finditer(r"^\+\+\+ b/(.+)$", txt, re.M):
        files.add(m.group(1).strip())
    for line in txt.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for m in re.finditer(r"\b(?:def|class|func|const|let|var|function)\s+([A-Za-z_][A-Za-z0-9_]*)", line):
                syms.add(m.group(1))
    return files, syms


def feat_patch(repo: str, tid: int, f: int) -> Path:
    return DS / repo / f"task{tid}" / f"feature{f}" / "feature.patch"


def coupling(repo: str, tid: int, f1: int, f2: int) -> tuple[int, int, int]:
    fa, sa = patch_files_syms(feat_patch(repo, tid, f1))
    fb, sb = patch_files_syms(feat_patch(repo, tid, f2))
    sf, ss = len(fa & fb), len(sa & sb)
    return sf * 2 + ss, sf, ss


def main() -> None:
    subset = sys.argv[1] if len(sys.argv) > 1 else "lite"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 14
    cap = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    man = json.load(open(DS / "subsets" / f"{subset}.json"))
    rows = []
    for t in man["tasks"]:
        for p in t["pairs"]:
            f1, f2 = sorted(p)
            cs, sf, ss = coupling(t["repo"], t["task_id"], f1, f2)
            rows.append((cs, sf, ss, t["repo"], t["task_id"], f1, f2))
    rows.sort(reverse=True)
    seen: dict[str, int] = {}
    pick = []
    for cs, sf, ss, repo, tid, f1, f2 in rows:
        if cs < 2 or seen.get(repo, 0) >= cap:
            continue
        seen[repo] = seen.get(repo, 0) + 1
        pick.append((cs, sf, ss, repo, tid, f1, f2))
        if len(pick) >= n:
            break
    for cs, sf, ss, repo, tid, f1, f2 in pick:
        print(f"  coupling={cs} (files={sf},syms={ss})  {repo}:{tid}:{f1},{f2}")
    print('\nPAIRS="' + " ".join(f"{r[3]}:{r[4]}:{r[5]},{r[6]}" for r in pick) + '"')


if __name__ == "__main__":
    main()
