"""A frozen harness snapshot must be exactly the live repo with args applied.

Three properties:
  1. CONTENT   — every snapshotted file is byte-identical to the live repo.
  2. ISOLATION — the snapshot's entry resolves imports from the snapshot's
                 own src/, not the live repo's.
  3. BEHAVIOR  — the fully-resolved run configuration (``--dry-run`` JSON)
                 from the snapshot equals the live repo's for the same
                 arguments, modulo the src_path (which MUST differ, each
                 pointing into its own tree).
"""

from __future__ import annotations

import filecmp
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

FIN_ARGS = [
    "--arm", "coopgitc2", "--team-size", "3", "--step-limit", "1000",
    "--agent-time-limit", "3600", "--repair", "--completion-gate",
    "--env-brief", "--presub-merge",
]


def _make_snapshot(tmp_path: Path):
    sys.path.insert(0, str(REPO / "scripts"))
    from team_harness_evolve import TeamHarness

    return TeamHarness.snapshot("equiv-test", tmp_path, source_repo=REPO,
                                args={"arm": "coopgitc2"})


def _assert_trees_identical(a: Path, b: Path) -> None:
    cmp = filecmp.dircmp(a, b, ignore=["__pycache__"])
    assert not cmp.left_only and not cmp.right_only, (
        f"{a} vs {b}: only-left={cmp.left_only} only-right={cmp.right_only}")
    assert not cmp.diff_files, f"{a} vs {b}: differing={cmp.diff_files}"
    for sub in cmp.common_dirs:
        _assert_trees_identical(a / sub, b / sub)


def _dry_run(entry: Path) -> dict:
    out = subprocess.run([PY, str(entry), *FIN_ARGS, "--dry-run"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_snapshot_content_identical(tmp_path):
    snap = _make_snapshot(tmp_path)
    for item in ("src", "scripts"):
        _assert_trees_identical(REPO / item, snap.root / item)
    assert (snap.root / "pyproject.toml").read_bytes() == (REPO / "pyproject.toml").read_bytes()


def test_snapshot_is_self_contained_and_behaviorally_equal(tmp_path):
    snap = _make_snapshot(tmp_path)
    live = _dry_run(REPO / "scripts" / "bench_programbench.py")
    frozen = _dry_run(snap.root / "scripts" / "bench_programbench.py")

    # isolation: each tree imports its own package
    assert live["src_path"].startswith(str(REPO)), live["src_path"]
    assert frozen["src_path"].startswith(str(snap.root)), (
        f"snapshot leaked to live code: {frozen['src_path']}")

    # behavior: identical resolved configuration otherwise
    live.pop("src_path"); frozen.pop("src_path")
    assert live == frozen, f"config drift:\nlive={live}\nfrozen={frozen}"
