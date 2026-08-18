"""LLM analyst — turn failure evidence into NEW hypotheses to test.

The judge scores; the analyst *generates*. It reads each failing pair's real
evidence (feature spec, the team's submitted diff, and the grader's actual
test/build error), diagnoses the failure mode, decides whether a team×agent
**seam** change could plausibly fix it (vs. an agent-capability / spec-ambiguity
limit that no coordination can fix), and proposes concrete, novel,
testable hypotheses — closing the self-improvement loop's reflect→generate step.

Injectable ``complete_fn`` so tests/offline runs need no API key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cooperagents.eval.judge import _parse_json

_MAX = 6000


def _clip(s: str, n: int = _MAX) -> str:
    return s if len(s) <= n else s[: n // 2] + f"\n...[clipped {len(s) - n}]...\n" + s[-n // 2 :]


@dataclass
class Failure:
    repo: str
    task_id: int
    features: list[int]
    spec: str
    diff: str
    error: str  # grader test/build output (the why)


def gather_failures(
    logs_dir: str | Path,
    label: str,
    setting: str,
    items: list,
    *,
    cooperbench_dir: str | None = None,
    limit: int = 8,
) -> list[Failure]:
    """Collect evidence for pairs that did NOT both-pass in a run."""
    from cooperagents.eval.dataset import read_feature

    root = Path(logs_dir) / label / setting
    out: list[Failure] = []
    for it in items:
        fs = "_".join(f"f{f}" for f in sorted(it.features))
        d = root / it.repo / str(it.task_id) / fs
        ev = d / "eval.json"
        if not ev.is_file():
            continue
        e = json.loads(ev.read_text())
        if e.get("both_passed"):
            continue
        # the failing feature's grader output is the most informative
        err = ""
        for k in ("feature1", "feature2"):
            o = e.get(k, {})
            if not o.get("passed"):
                err += (o.get("test_output", "") or "")[-2500:] + "\n"
        diff = ""
        for name in ("integrated.patch", "solo.patch", f"agent{sorted(it.features)[0]}.patch"):
            p = d / name
            if p.is_file() and p.read_text().strip():
                diff = p.read_text()
                break
        try:
            spec = "\n\n".join(read_feature(it.repo, it.task_id, f, cooperbench_dir=cooperbench_dir) for f in sorted(it.features))
        except (FileNotFoundError, OSError):
            spec = ""
        out.append(Failure(it.repo, it.task_id, sorted(it.features), spec, diff, err))
        if len(out) >= limit:
            break
    return out


@dataclass
class Proposal:
    hypothesis: str
    seam_addressable: bool
    rationale: str
    test_plan: str
    failure_modes: list[str] = field(default_factory=list)


_DIAGNOSE = """You analyze failures of a MULTI-AGENT coding harness to propose how to improve it.

Setup: a team of agents (each in its own container, mini-swe-agent loop) implements two features in
one repo; a hidden test suite grades them. The "seam" is the boundary between the TEAM layer
(how agents coordinate, are assigned work, share context, integrate) and the AGENT loop (its
prompt, tools, control flow) — both are editable.

Already tried (do NOT re-propose): shared code substrate (kept), live teammate-diff context,
verify-and-fix integrator, spec-fidelity prompt, step-budget, destructive-git guard,
feature-ordering, independent-agents+integrator. These showed no robust gain.

Below are REAL failed attempts: the feature spec, the team's submitted diff, and the grader's
error output. For each, the failure is usually one of: build/compile error, wrong public API name
or signature, incomplete implementation, cross-feature conflict, or SPEC-AMBIGUOUS (the exact
identifier the test wants is not derivable from the spec — no coordination can fix this).

{cases}

Return ONLY JSON:
{{"diagnoses": [{{"pair": "<repo/task feats>", "failure_mode": "<one of the above>",
   "seam_addressable": true|false}}],
  "proposals": [{{"hypothesis": "<concrete NEW seam change, 1 sentence>",
   "seam_addressable": true|false, "rationale": "<tied to the observed failures>",
   "test_plan": "<one line: how to A/B it>", "failure_modes": ["<modes it targets>"]}}]}}
Propose 3–6 proposals, novel vs the tried list, ranked by expected impact. Be honest: if most
failures are spec-ambiguous / agent-capability bound, say so and propose accordingly (e.g.,
evidence-gathering or capability seams), not generic coordination tweaks."""


def propose_hypotheses(
    failures: list[Failure],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    complete_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Ask the analyst to diagnose failures and propose new hypotheses."""
    import os

    if complete_fn is None:
        from openai import OpenAI

        m = str(model or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao"))
        b = str(base_url or os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "")
        k = str(api_key or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
        _client = OpenAI(base_url=b, api_key=k)

        def complete_fn(prompt: str) -> str:  # generous cap: reasoning model + long JSON answer
            resp = _client.chat.completions.create(model=m, messages=[{"role": "user", "content": prompt}], max_completion_tokens=8000)
            return resp.choices[0].message.content or ""

    cases = []
    for i, f in enumerate(failures, 1):
        cases.append(
            f"### Case {i}: {f.repo}/{f.task_id} features {f.features}\n"
            f"-- spec --\n{_clip(f.spec, 2500)}\n-- submitted diff --\n{_clip(f.diff, 3500)}\n"
            f"-- grader error --\n{_clip(f.error, 2000)}"
        )
    raw = complete_fn(_DIAGNOSE.format(cases="\n\n".join(cases)))
    data = _parse_json(raw)
    proposals = [
        Proposal(
            hypothesis=str(p.get("hypothesis", "")),
            seam_addressable=bool(p.get("seam_addressable", False)),
            rationale=str(p.get("rationale", "")),
            test_plan=str(p.get("test_plan", "")),
            failure_modes=list(p.get("failure_modes", [])),
        )
        for p in data.get("proposals", [])
    ]
    return {"diagnoses": data.get("diagnoses", []), "proposals": proposals}


__all__ = ["Failure", "Proposal", "gather_failures", "propose_hypotheses"]
