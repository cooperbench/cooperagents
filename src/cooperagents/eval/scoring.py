"""Scoring for non-code benchmarks.

The git benchmarks are graded by shelling out to an official evaluator over a
patched file tree (``adapters.*.evaluate``).  Non-code benchmarks grade a
task-native deliverable — an answer string, a final DB state, an action
trajectory — so scoring is a function of the submitted text and the task's
ground truth, not a diff.

A :class:`Scorer` maps ``(instance, submission)`` to a :class:`TaskScore`.  Each
benchmark supplies its own (WorkBench's DB-state comparator, PlanCraft's
Evaluator, an LLM judge for BrowseComp/Finance); the generic scorers here cover
tests and simple exact-match cases.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskScore:
    """The grade for one task instance under one system configuration."""

    instance_id: str
    success: float  # 0.0..1.0 (1.0 == fully correct)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.success >= 1.0


class Scorer(abc.ABC):
    """Grades one submitted deliverable against a task instance."""

    name: str = "scorer"

    @abc.abstractmethod
    def score(self, instance: Any, submission: str) -> TaskScore:
        """Return the :class:`TaskScore` for ``submission`` on ``instance``."""


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


class ExactMatchScorer(Scorer):
    """Normalized exact-match against ``instance.answer`` (whitespace/case-insensitive)."""

    name = "exact_match"

    def score(self, instance: Any, submission: str) -> TaskScore:
        gold = getattr(instance, "answer", instance if isinstance(instance, str) else "")
        instance_id = str(getattr(instance, "instance_id", ""))
        ok = bool(submission) and _normalize(submission) == _normalize(str(gold))
        return TaskScore(instance_id=instance_id, success=1.0 if ok else 0.0, detail={"gold": gold})


class FunctionScorer(Scorer):
    """Adapt an arbitrary callable ``(instance, submission) -> float|bool`` into a
    :class:`Scorer`. Lets a benchmark plug its official evaluator without
    subclassing (the callable shells out to the real scorer)."""

    def __init__(self, fn: Callable[[Any, str], float | bool], *, name: str = "function") -> None:
        self._fn = fn
        self.name = name

    def score(self, instance: Any, submission: str) -> TaskScore:
        raw = self._fn(instance, submission)
        instance_id = str(getattr(instance, "instance_id", ""))
        return TaskScore(instance_id=instance_id, success=float(raw), detail={})


__all__ = ["TaskScore", "Scorer", "ExactMatchScorer", "FunctionScorer"]
