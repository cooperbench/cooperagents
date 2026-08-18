"""LLM-as-a-judge for the self-improvement loop.

Pass/fail on 10 pairs is coarse and noisy — a seam change can improve solution
quality, reduce wasted/conflicting work, or fix near-misses without flipping a
binary pass yet.  The judge adds a finer, lower-variance signal:

  * :meth:`LLMJudge.score`   — absolute rubric scores for one run's diff.
  * :meth:`LLMJudge.compare` — pairwise A/B preference (baseline vs candidate),
    run in BOTH orders to cancel position bias.  This is the primary signal for
    the loop because relative judgments are more reliable than absolute ones.

The judge is deliberately injectable (``complete_fn``) so the test suite and
offline use need no API key.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_MAX_DIFF = 12000  # keep prompts bounded


def _truncate(text: str, limit: int = _MAX_DIFF) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[diff truncated {len(text) - limit} chars]...\n{text[-half:]}"


@dataclass
class JudgeScores:
    """Absolute rubric scores (1–5) for a single run's solution."""

    completeness: int  # does the diff implement BOTH features per spec
    correctness: int  # likelihood it's functionally correct / compiles
    efficiency: int  # minimal, non-redundant, no wasted/conflicting change
    overall: int
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "completeness": self.completeness,
            "correctness": self.correctness,
            "efficiency": self.efficiency,
            "overall": self.overall,
            "rationale": self.rationale,
        }


@dataclass
class Comparison:
    """Pairwise verdict between baseline (A) and candidate (B)."""

    winner: str  # "baseline" | "candidate" | "tie"
    rationale: str = ""
    order_votes: list[str] = field(default_factory=list)


_SCORE_PROMPT = """You are grading one solution to a software task that bundles TWO features.
Score the unified diff against the spec on a 1–5 scale (5 best).

## Task spec
{task}

## Submitted diff
{diff}

Return ONLY a JSON object:
{{"completeness": <1-5: implements BOTH features per spec>,
  "correctness": <1-5: likely compiles & functionally correct>,
  "efficiency": <1-5: minimal & focused; no wasted, duplicated, or conflicting edits>,
  "overall": <1-5>,
  "rationale": "<one or two sentences>"}}"""

_COMPARE_PROMPT = """You are comparing TWO candidate solutions (A and B) to the same task,
which bundles two features. Decide which better implements the spec with fewer
wasted/conflicting changes. Judge quality, not length.

## Task spec
{task}

## Solution A
{diff_a}

## Solution B
{diff_b}

Return ONLY a JSON object: {{"winner": "A" | "B" | "tie", "rationale": "<one sentence>"}}"""


def _parse_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start == -1:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj if isinstance(obj, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


def _default_complete(model: str, base_url: str, api_key: str) -> Callable[[str], str]:
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key)

    def complete(prompt: str) -> str:
        resp = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}])
        return resp.choices[0].message.content or ""

    return complete


class LLMJudge:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        complete_fn: Callable[[str], str] | None = None,
    ) -> None:
        if complete_fn is not None:
            self._complete = complete_fn
        else:
            m = str(model or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao"))
            b = str(base_url or os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "")
            k = str(api_key or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
            self._complete = _default_complete(m, b, k)

    def score(self, *, task: str, diff: str) -> JudgeScores:
        raw = self._complete(_SCORE_PROMPT.format(task=_truncate(task, 6000), diff=_truncate(diff) or "(empty diff)"))
        d = _parse_json(raw)

        def g(k: str) -> int:
            try:
                return max(1, min(5, int(d.get(k, 1))))
            except (TypeError, ValueError):
                return 1

        return JudgeScores(
            completeness=g("completeness"),
            correctness=g("correctness"),
            efficiency=g("efficiency"),
            overall=g("overall"),
            rationale=str(d.get("rationale", "")),
        )

    def _one_compare(self, task: str, diff_a: str, diff_b: str) -> str:
        raw = self._complete(
            _COMPARE_PROMPT.format(task=_truncate(task, 6000), diff_a=_truncate(diff_a) or "(empty)", diff_b=_truncate(diff_b) or "(empty)")
        )
        w = str(_parse_json(raw).get("winner", "tie")).strip().upper()
        return w if w in {"A", "B", "TIE"} else "TIE"

    def compare(self, *, task: str, baseline_diff: str, candidate_diff: str) -> Comparison:
        """Pairwise preference, run in both orders to cancel position bias.

        Vote 1: A=baseline, B=candidate. Vote 2: A=candidate, B=baseline.
        Agreement → that winner; disagreement → tie.
        """
        v1 = self._one_compare(task, baseline_diff, candidate_diff)  # A=baseline
        v2 = self._one_compare(task, candidate_diff, baseline_diff)  # A=candidate
        # Map each vote to baseline/candidate.
        m1 = {"A": "baseline", "B": "candidate", "TIE": "tie"}[v1]
        m2 = {"A": "candidate", "B": "baseline", "TIE": "tie"}[v2]
        if m1 == m2:
            winner = m1
        elif "tie" in (m1, m2):
            winner = m1 if m2 == "tie" else m2
        else:
            winner = "tie"  # flipped with order → no real preference
        return Comparison(winner=winner, order_votes=[m1, m2])


__all__ = ["JudgeScores", "Comparison", "LLMJudge"]
