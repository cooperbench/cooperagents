"""Composite evaluation for the self-improvement loop.

Combines the three signals into one comparable scorecard and a keep/drop
verdict:

  1. **pass-rate** (ground truth, from CooperBench ``eval.json``) — primary.
  2. **efficiency** (wall-clock + LLM calls, from ``result.json``) — tie-breaker,
     measured on tasks BOTH variants solve (time-to-success).
  3. **LLM judge** (pairwise candidate-vs-baseline on the submitted diffs) — a
     finer quality signal that moves even when binary pass-rate ties.

The verdict implements the loop's DECIDE rule: pass-rate dominates; when it's
within noise (±1 pair at n=10), efficiency + judge break the tie.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cooperagents.eval.dataset import WorkItem, read_feature
from cooperagents.eval.judge import LLMJudge

# A 1-pair swing at n=10 is within noise; require more than that to call it on
# pass-rate alone.
_PASS_NOISE = 0.101


@dataclass
class PairScore:
    item: WorkItem
    passed: bool
    features_passed: int  # 0..2
    duration: float
    steps: int
    diff: str = ""


@dataclass
class Scorecard:
    label: str
    setting: str
    pairs: list[PairScore] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.pairs)

    @property
    def passed(self) -> int:
        return sum(1 for p in self.pairs if p.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.n if self.n else 0.0

    @property
    def feature_rate(self) -> float:
        return sum(p.features_passed for p in self.pairs) / (2 * self.n) if self.n else 0.0

    @property
    def avg_duration(self) -> float:
        return sum(p.duration for p in self.pairs) / self.n if self.n else 0.0

    @property
    def avg_steps(self) -> float:
        return sum(p.steps for p in self.pairs) / self.n if self.n else 0.0


def _submitted_diff(log_dir: Path, setting: str, features: list[int]) -> str:
    feats = sorted(features)
    for name in ("integrated.patch", "solo.patch", f"agent{feats[0]}.patch"):
        p = log_dir / name
        if p.is_file() and p.read_text().strip():
            return p.read_text()
    return ""


def load_scorecard(logs_dir: str | Path, label: str, setting: str, items: list[WorkItem]) -> Scorecard:
    """Read run artifacts (eval.json + result.json + submitted diff) into a scorecard."""
    root = Path(logs_dir) / label / setting
    card = Scorecard(label=label, setting=setting)
    for it in items:
        feat_str = "_".join(f"f{f}" for f in sorted(it.features))
        d = root / it.repo / str(it.task_id) / feat_str
        ev = d / "eval.json"
        rj = d / "result.json"
        passed = False
        fpassed = 0
        if ev.is_file():
            e = json.loads(ev.read_text())
            passed = bool(e.get("both_passed"))
            fpassed = sum(1 for k in ("feature1", "feature2") if e.get(k, {}).get("passed"))
        dur = steps = 0.0
        if rj.is_file():
            r = json.loads(rj.read_text())
            dur = float(r.get("duration_seconds", 0) or 0)
            steps = float(r.get("total_steps", 0) or 0)
        card.pairs.append(
            PairScore(
                item=it,
                passed=passed,
                features_passed=fpassed,
                duration=dur,
                steps=int(steps),
                diff=_submitted_diff(d, setting, it.features),
            )
        )
    return card


@dataclass
class ThreeMetrics:
    """The three headline metrics for one run: success, efficiency, judge."""

    label: str
    success: float  # both_passed pass-rate (0..1)
    feature_rate: float
    avg_duration: float  # seconds/pair — efficiency (lower = better)
    avg_steps: float
    judge: float | None = None  # mean absolute judge overall (1..5), if judged


def mean_judge_score(
    card: Scorecard,
    judge: LLMJudge,
    *,
    cooperbench_dir: str | None = None,
    task_for: Callable[[WorkItem], str] | None = None,
) -> float:
    """Mean absolute judge overall (1–5) across pairs (empty diff → 1)."""
    resolve_task = task_for or _default_task_for(cooperbench_dir)
    scores: list[int] = []
    for p in card.pairs:
        if not p.diff.strip():
            scores.append(1)
            continue
        task = resolve_task(p.item)
        if not task:
            continue
        scores.append(judge.score(task=task, diff=p.diff).overall)
    return sum(scores) / len(scores) if scores else 0.0


def summarize(
    card: Scorecard,
    *,
    judge: LLMJudge | None = None,
    cooperbench_dir: str | None = None,
    task_for: Callable[[WorkItem], str] | None = None,
) -> ThreeMetrics:
    """Compute the three headline metrics (judge only if a judge is given)."""
    return ThreeMetrics(
        label=card.label,
        success=card.pass_rate,
        feature_rate=card.feature_rate,
        avg_duration=card.avg_duration,
        avg_steps=card.avg_steps,
        judge=(mean_judge_score(card, judge, cooperbench_dir=cooperbench_dir, task_for=task_for) if judge else None),
    )


@dataclass
class Verdict:
    pass_delta: float  # candidate - baseline, in pass-rate
    feature_delta: float
    duration_delta: float  # candidate - baseline avg seconds (negative = faster)
    judge_wins: int = 0
    judge_losses: int = 0
    judge_ties: int = 0
    decision: str = "inconclusive"  # keep | drop | inconclusive
    rationale: str = ""

    @property
    def judge_winrate(self) -> float:
        decided = self.judge_wins + self.judge_losses
        return self.judge_wins / decided if decided else 0.5


def _default_task_for(cooperbench_dir: str | None) -> Callable[[WorkItem], str]:
    def task_for(it: WorkItem) -> str:
        try:
            specs = [read_feature(it.repo, it.task_id, f, cooperbench_dir=cooperbench_dir) for f in sorted(it.features)]
        except (FileNotFoundError, OSError):
            return ""
        return "\n\n---\n\n".join(specs)

    return task_for


def compare(
    baseline: Scorecard,
    candidate: Scorecard,
    *,
    judge: LLMJudge | None = None,
    cooperbench_dir: str | None = None,
    task_for: Callable[[WorkItem], str] | None = None,
) -> Verdict:
    """Composite keep/drop verdict for candidate vs baseline on the same pairs."""
    pass_delta = candidate.pass_rate - baseline.pass_rate
    feature_delta = candidate.feature_rate - baseline.feature_rate
    duration_delta = candidate.avg_duration - baseline.avg_duration

    v = Verdict(pass_delta=pass_delta, feature_delta=feature_delta, duration_delta=duration_delta)
    resolve_task = task_for or _default_task_for(cooperbench_dir)

    # Pairwise judge over pairs where the submitted diffs actually differ.
    if judge is not None:
        b_by = {(p.item.repo, p.item.task_id, tuple(sorted(p.item.features))): p for p in baseline.pairs}
        for cp in candidate.pairs:
            key = (cp.item.repo, cp.item.task_id, tuple(sorted(cp.item.features)))
            bp = b_by.get(key)
            if bp is None or cp.diff.strip() == bp.diff.strip():
                continue
            task = resolve_task(cp.item)
            if not task:
                continue
            res = judge.compare(task=task, baseline_diff=bp.diff, candidate_diff=cp.diff)
            if res.winner == "candidate":
                v.judge_wins += 1
            elif res.winner == "baseline":
                v.judge_losses += 1
            else:
                v.judge_ties += 1

    # --- DECIDE -------------------------------------------------------
    faster = duration_delta <= 0
    if pass_delta >= _PASS_NOISE:
        v.decision = "keep"
        v.rationale = f"pass-rate up {pass_delta:+.0%}"
    elif pass_delta <= -_PASS_NOISE:
        if judge is not None and v.judge_winrate >= 0.7 and faster:
            v.decision = "inconclusive"
            v.rationale = (
                f"pass-rate down {pass_delta:+.0%} but judge favors candidate ({v.judge_wins}-{v.judge_losses}) & faster — recheck at n=30"
            )
        else:
            v.decision = "drop"
            v.rationale = f"pass-rate down {pass_delta:+.0%}"
    else:
        # Pass-rate tied within noise → efficiency + judge decide.
        judge_pos = judge is not None and v.judge_winrate > 0.5 and (v.judge_wins + v.judge_losses) > 0
        judge_neg = judge is not None and v.judge_winrate < 0.4 and (v.judge_wins + v.judge_losses) > 0
        if (judge_pos and faster) or (judge_pos and feature_delta > 0):
            v.decision = "keep"
            v.rationale = (
                f"pass-rate tied; judge favors candidate ({v.judge_wins}-{v.judge_losses}), {'faster' if faster else 'feature-rate up'}"
            )
        elif judge_neg or (duration_delta > 0 and feature_delta <= 0 and not judge_pos):
            v.decision = "drop"
            v.rationale = "pass-rate tied; no quality/efficiency gain (or judge favors baseline)"
        else:
            v.decision = "inconclusive"
            v.rationale = (
                f"pass-rate tied; signals mixed (judge {v.judge_wins}-{v.judge_losses}, Δtime {duration_delta:+.0f}s) — recheck at n=30"
            )
    return v


def format_report(baseline: Scorecard, candidate: Scorecard, verdict: Verdict) -> str:
    lines = [
        f"baseline '{baseline.label}'  vs  candidate '{candidate.label}'  (n={candidate.n})",
        f"  pass-rate : {baseline.pass_rate:.0%}  ->  {candidate.pass_rate:.0%}   (Δ {verdict.pass_delta:+.0%})",
        f"  feature   : {baseline.feature_rate:.0%}  ->  {candidate.feature_rate:.0%}   (Δ {verdict.feature_delta:+.0%})",
        f"  avg time  : {baseline.avg_duration:.0f}s -> {candidate.avg_duration:.0f}s  (Δ {verdict.duration_delta:+.0f}s)",
        f"  avg steps : {baseline.avg_steps:.0f}  -> {candidate.avg_steps:.0f}",
    ]
    if verdict.judge_wins + verdict.judge_losses + verdict.judge_ties:
        jw, jl, jt = verdict.judge_wins, verdict.judge_losses, verdict.judge_ties
        lines.append(f"  judge     : candidate {jw}W / {jl}L / {jt}T (winrate {verdict.judge_winrate:.0%})")
    lines.append(f"  VERDICT   : {verdict.decision.upper()} — {verdict.rationale}")
    return "\n".join(lines)


__all__ = ["PairScore", "Scorecard", "Verdict", "load_scorecard", "compare", "format_report"]
