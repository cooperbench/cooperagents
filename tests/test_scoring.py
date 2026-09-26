"""Non-code scoring: Scorer interface + generic scorers."""

from __future__ import annotations

from dataclasses import dataclass

from cooperagents.eval.scoring import ExactMatchScorer, FunctionScorer, TaskScore


@dataclass
class _Inst:
    instance_id: str
    answer: str


def test_task_score_passed_threshold():
    assert TaskScore("a", 1.0).passed
    assert not TaskScore("a", 0.5).passed


def test_exact_match_normalizes():
    s = ExactMatchScorer()
    inst = _Inst("q1", "Paris")
    assert s.score(inst, "  paris ").passed
    assert not s.score(inst, "Lyon").passed
    assert not s.score(inst, "").passed
    assert s.score(inst, "paris").instance_id == "q1"


def test_function_scorer_wraps_callable():
    calls = []

    def fake_official(inst, submission):
        calls.append((inst.instance_id, submission))
        return submission == "correct"

    s = FunctionScorer(fake_official, name="official")
    assert s.score(_Inst("t1", ""), "correct").passed
    assert not s.score(_Inst("t2", ""), "wrong").passed
    assert calls == [("t1", "correct"), ("t2", "wrong")]
