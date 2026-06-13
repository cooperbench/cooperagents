"""Independence-maximizing task decomposition (G1) for the unified harness.

Round 6 finding: a parallel multi-agent system's value is bounded by the
*independence* of its subtasks — coordination cannot recover a bad cut. So the
decomposer's objective IS separability. Given the work (one objective, or N
feature specs), the planner proposes a DAG of subtasks chosen to minimize
cross-subtask coupling: work that touches the same code/state is MERGED into one
subtask (one agent, no interference); large independent work may be SPLIT across
agents. Edges encode genuine "B needs A's output" dependencies; the scheduler
(harness) seeds a subtask only along those edges and runs independent subtasks
in parallel.

The model call is injectable (``complete_fn``) so tests/offline runs need no API
key. A safe fallback (one subtask per feature, no dependencies) is used whenever
the planner is unavailable or returns something invalid — that degrades to the
plain parallel team, never to a crash.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from cooperagents.eval.judge import _parse_json
from cooperagents.types import SubTask

_PLAN_PROMPT = """You are the PLANNER of a multi-agent coding team. You decompose work into subtasks for
parallel agents, each in its OWN isolated container; independent subtasks run concurrently and their
diffs are merged.

Your objective is ZERO-CONFLICT parallelism. The unit that matters is the WRITE-SET — the specific
files AND regions (functions/classes/blocks) a piece of work edits. Two subtasks conflict ONLY if
their write-sets OVERLAP at the region level. Crucially:
- Two subtasks editing the SAME FILE but DIFFERENT functions/regions do NOT conflict — SPLIT them and
  give each a disjoint `owns` region list. Do not merge work just because it shares a file.
- MERGE into one subtask ONLY when the work truly edits the SAME function/region (a real overlap that
  cannot be divided), or when one piece strictly needs the other's new code (then use depends_on).
- Each subtask's `owns` must be disjoint from every other subtask's `owns`. Predict the write-set from
  the spec (it usually names the functions/classes/files involved); be specific.
- Add a dependency edge (B depends_on A) ONLY when B calls/extends A's NEW code. Prefer 0 edges.
- Use at most {max_subtasks} subtasks.

## Work to decompose
{work}

Return ONLY JSON:
{{"subtasks": [
   {{"id": "t1", "task": "<self-contained instruction>",
     "owns": ["<path/to/file.py: funcA(), ClassB>", "..."],
     "depends_on": [], "features": [<feature numbers>]}},
   ...
 ],
 "rationale": "<one sentence: how the write-sets are disjoint>"}}"""


def _validate(raw: list, feature_ids: list[int], max_subtasks: int) -> list[SubTask] | None:
    if not isinstance(raw, list) or not raw:
        return None
    subs: list[SubTask] = []
    ids: set[str] = set()
    for i, s in enumerate(raw[:max_subtasks]):
        if not isinstance(s, dict):
            return None
        sid = str(s.get("id") or f"t{i + 1}")
        if sid in ids:
            sid = f"{sid}_{i}"
        ids.add(sid)
        task = str(s.get("task", "")).strip()
        if not task:
            return None
        deps = [str(d) for d in s.get("depends_on", []) if isinstance(d, (str, int))]
        feats = [int(f) for f in s.get("features", []) if isinstance(f, (int, str)) and str(f).lstrip("-").isdigit()]
        owns = [str(o) for o in s.get("owns", []) if isinstance(o, str) and o.strip()]
        subs.append(SubTask(id=sid, task=task, depends_on=deps, features=feats, owns=owns))
    # drop edges to unknown ids; break cycles by keeping only backward edges (topo by order)
    known = {s.id for s in subs}
    order = {s.id: i for i, s in enumerate(subs)}
    for s in subs:
        s.depends_on = [d for d in s.depends_on if d in known and order[d] < order[s.id]]
    # ensure every requested feature is covered somewhere; else fall back
    covered = {f for s in subs for f in s.features}
    if feature_ids and not set(feature_ids).issubset(covered):
        # don't reject outright — attach uncovered features to the first subtask
        missing = [f for f in feature_ids if f not in covered]
        subs[0].features = sorted(set(subs[0].features) | set(missing))
    return subs


def fallback_plan(specs: list[tuple[int, str]]) -> list[SubTask]:
    """Degrade to the plain parallel team: one independent subtask per feature."""
    return [SubTask(id=f"t{i + 1}", task=text, depends_on=[], features=[fid]) for i, (fid, text) in enumerate(specs)]


def plan_decomposition(
    specs: list[tuple[int, str]],
    *,
    objective: str | None = None,
    max_subtasks: int = 4,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    complete_fn: Callable[[str], str] | None = None,
) -> tuple[list[SubTask], str]:
    """Return (subtasks, rationale). Never raises — falls back to one-per-feature."""
    feature_ids = [fid for fid, _ in specs]
    if objective:
        work = objective
    else:
        work = "\n\n---\n\n".join(f"### Feature {fid}\n{text}" for fid, text in specs)

    if complete_fn is None:
        try:
            from openai import OpenAI

            m = str(model or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao"))
            b = str(base_url or os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "")
            k = str(api_key or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
            _client = OpenAI(base_url=b, api_key=k)

            def complete_fn(prompt: str) -> str:
                resp = _client.chat.completions.create(
                    model=m, messages=[{"role": "user", "content": prompt}], max_completion_tokens=4000
                )
                return resp.choices[0].message.content or ""
        except Exception:  # noqa: BLE001 - no creds / no lib → fallback
            return fallback_plan(specs), "fallback: planner unavailable"

    try:
        raw = complete_fn(_PLAN_PROMPT.format(work=work[:8000], max_subtasks=max_subtasks))
        data = _parse_json(raw)
        subs = _validate(data.get("subtasks", []), feature_ids, max_subtasks)
        if subs is None:
            return fallback_plan(specs), "fallback: invalid plan"
        return subs, str(data.get("rationale", ""))
    except Exception:  # noqa: BLE001 - any planner error → safe fallback
        return fallback_plan(specs), "fallback: planner error"


def topo_levels(subs: list[SubTask]) -> list[list[SubTask]]:
    """Group subtasks into dependency levels (each level runs in parallel)."""
    done: set[str] = set()
    levels: list[list[SubTask]] = []
    remaining = list(subs)
    while remaining:
        ready = [s for s in remaining if all(d in done for d in s.depends_on)]
        if not ready:  # cycle safety: force-take the rest as one level
            ready = remaining
        levels.append(ready)
        done |= {s.id for s in ready}
        remaining = [s for s in remaining if s.id not in done]
    return levels


def ancestors(sub: SubTask, by_id: dict[str, SubTask]) -> list[str]:
    """Transitive dependency ids of ``sub`` (for seeding its container)."""
    seen: list[str] = []
    stack = list(sub.depends_on)
    while stack:
        d = stack.pop()
        if d in seen or d not in by_id:
            continue
        seen.append(d)
        stack.extend(by_id[d].depends_on)
    return seen


__all__ = ["plan_decomposition", "fallback_plan", "topo_levels", "ancestors"]
