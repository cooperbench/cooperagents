"""Judge + composite scorecard + verdict (no API — scripted judge)."""

from __future__ import annotations

import json

from cooperagents.eval.dataset import WorkItem
from cooperagents.eval.judge import LLMJudge
from cooperagents.eval.scorecard import PairScore, Scorecard, compare, load_scorecard


def _write_run(tmp, label, setting, repo, task, feats, *, both_passed, f1, f2, duration, steps, diff):
    feat_str = "_".join(f"f{f}" for f in sorted(feats))
    d = tmp / label / setting / repo / str(task) / feat_str
    d.mkdir(parents=True)
    (d / "eval.json").write_text(json.dumps({"both_passed": both_passed, "feature1": {"passed": f1}, "feature2": {"passed": f2}}))
    (d / "result.json").write_text(json.dumps({"duration_seconds": duration, "total_steps": steps}))
    (d / "integrated.patch").write_text(diff)


class TestJudge:
    def test_score_parses_rubric(self):
        j = LLMJudge(complete_fn=lambda p: '{"completeness":5,"correctness":4,"efficiency":3,"overall":4,"rationale":"ok"}')
        s = j.score(task="t", diff="diff")
        assert (s.completeness, s.correctness, s.efficiency, s.overall) == (5, 4, 3, 4)

    def test_score_clamps_and_defaults(self):
        j = LLMJudge(complete_fn=lambda p: "garbage no json")
        s = j.score(task="t", diff="d")
        assert 1 <= s.overall <= 5

    def test_compare_agreement_both_orders(self):
        # Judge always prefers whichever solution is "B" -> flips with order -> tie.
        j = LLMJudge(complete_fn=lambda p: '{"winner":"B"}')
        c = j.compare(task="t", baseline_diff="x", candidate_diff="y")
        assert c.winner == "tie"

    def test_compare_consistent_preference(self):
        # Prefer the diff containing CAND regardless of position -> candidate wins.
        def fn(prompt: str) -> str:
            # find which of A/B block contains 'CAND'
            a_idx = prompt.index("Solution A")
            b_idx = prompt.index("Solution B")
            a_block = prompt[a_idx:b_idx]
            return '{"winner":"A"}' if "CAND" in a_block else '{"winner":"B"}'

        j = LLMJudge(complete_fn=fn)
        c = j.compare(task="t", baseline_diff="BASE only", candidate_diff="CAND only")
        assert c.winner == "candidate"


class TestScorecard:
    def test_load_and_rates(self, tmp_path):
        items = [WorkItem("go_chi_task", 26, [1, 2]), WorkItem("dspy_task", 8394, [3, 4])]
        _write_run(
            tmp_path, "exp-team", "team", "go_chi_task", 26, [1, 2], both_passed=True, f1=True, f2=True, duration=100, steps=20, diff="A"
        )
        _write_run(
            tmp_path, "exp-team", "team", "dspy_task", 8394, [3, 4], both_passed=False, f1=True, f2=False, duration=200, steps=30, diff="B"
        )
        card = load_scorecard(tmp_path, "exp-team", "team", items)
        assert card.n == 2
        assert card.pass_rate == 0.5
        assert card.feature_rate == 0.75  # 3 of 4 features
        assert card.avg_duration == 150


class TestVerdict:
    def _cards(self, base_pass, cand_pass, *, cand_dur=100.0, base_dur=100.0):
        items = [WorkItem("r", i, [1, 2]) for i in range(10)]
        base = Scorecard(
            "b", "team", [PairScore(it, i < base_pass, 2 if i < base_pass else 0, base_dur, 10, "base") for i, it in enumerate(items)]
        )
        cand = Scorecard(
            "c", "team", [PairScore(it, i < cand_pass, 2 if i < cand_pass else 0, cand_dur, 10, "cand") for i, it in enumerate(items)]
        )
        return base, cand

    def test_pass_rate_improvement_keeps(self):
        base, cand = self._cards(3, 5)
        assert compare(base, cand).decision == "keep"

    def test_pass_rate_regression_drops(self):
        base, cand = self._cards(5, 3)
        assert compare(base, cand).decision == "drop"

    def test_tie_with_judge_win_and_faster_keeps(self):
        base, cand = self._cards(4, 4, cand_dur=50, base_dur=100)
        judge = LLMJudge(
            complete_fn=lambda p: '{"winner":"A"}' if "cand" in p[p.index("Solution A") : p.index("Solution B")] else '{"winner":"B"}'
        )
        v = compare(base, cand, judge=judge, task_for=lambda it: "task spec")
        assert v.judge_wins > 0
        assert v.decision == "keep"

    def test_tie_no_gain_drops(self):
        base, cand = self._cards(4, 4, cand_dur=200, base_dur=100)  # slower, no judge
        assert compare(base, cand).decision == "drop"
