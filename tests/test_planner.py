"""Independence-maximizing planner: validation, fallback, DAG ordering."""

from __future__ import annotations

import json

from cooperagents.planner import ancestors, fallback_plan, plan_decomposition, topo_levels
from cooperagents.types import SubTask


def test_fallback_one_subtask_per_feature():
    subs = fallback_plan([(1, "feat one"), (2, "feat two")])
    assert [s.id for s in subs] == ["t1", "t2"]
    assert all(s.depends_on == [] for s in subs)  # fully parallel
    assert [s.features for s in subs] == [[1], [2]]


def test_plan_uses_injected_model_and_parses_dag():
    def fake(_prompt: str) -> str:
        return json.dumps(
            {
                "subtasks": [
                    {"id": "a", "task": "do A", "depends_on": [], "features": [1]},
                    {"id": "b", "task": "do B", "depends_on": [], "features": [2]},
                    {"id": "c", "task": "merge", "depends_on": ["a", "b"], "features": [1, 2]},
                ],
                "rationale": "a,b independent; c integrates",
            }
        )

    subs, rationale = plan_decomposition([(1, "f1"), (2, "f2")], complete_fn=fake, max_subtasks=4)
    assert [s.id for s in subs] == ["a", "b", "c"]
    assert subs[2].depends_on == ["a", "b"]
    assert "independent" in rationale


def test_plan_parses_owns_writesets():
    # region-aware: same file, disjoint functions → two subtasks with disjoint owns
    def fake(_p: str) -> str:
        return json.dumps(
            {
                "subtasks": [
                    {"id": "a", "task": "do A", "owns": ["termui.py: prompt()"], "depends_on": [], "features": [1]},
                    {"id": "b", "task": "do B", "owns": ["termui.py: confirm()"], "depends_on": [], "features": [2]},
                ],
                "rationale": "disjoint functions in the same file",
            }
        )

    subs, _ = plan_decomposition([(1, "f1"), (2, "f2")], complete_fn=fake, max_subtasks=4)
    assert subs[0].owns == ["termui.py: prompt()"]
    assert subs[1].owns == ["termui.py: confirm()"]
    # same file, disjoint regions → fully parallel (no edges)
    assert all(s.depends_on == [] for s in subs)


def test_plan_falls_back_on_garbage():
    subs, rationale = plan_decomposition([(1, "f1"), (2, "f2")], complete_fn=lambda _p: "not json", max_subtasks=4)
    assert [s.id for s in subs] == ["t1", "t2"]  # fallback
    assert "fallback" in rationale


def test_validate_drops_forward_and_unknown_edges():
    # forward edge (a depends on later b) and unknown edge are stripped → acyclic
    def fake(_p: str) -> str:
        return json.dumps(
            {"subtasks": [
                {"id": "a", "task": "A", "depends_on": ["b", "zzz"], "features": [1]},
                {"id": "b", "task": "B", "depends_on": [], "features": [2]},
            ]}
        )

    subs, _ = plan_decomposition([(1, "f1"), (2, "f2")], complete_fn=fake, max_subtasks=4)
    a = next(s for s in subs if s.id == "a")
    assert a.depends_on == []  # forward edge to b (order 1) and unknown zzz both dropped


def test_topo_levels_and_ancestors():
    subs = [
        SubTask(id="a", task="A"),
        SubTask(id="b", task="B"),
        SubTask(id="c", task="C", depends_on=["a", "b"]),
        SubTask(id="d", task="D", depends_on=["c"]),
    ]
    levels = topo_levels(subs)
    assert [sorted(s.id for s in lvl) for lvl in levels] == [["a", "b"], ["c"], ["d"]]
    by_id = {s.id: s for s in subs}
    assert sorted(ancestors(by_id["d"], by_id)) == ["a", "b", "c"]


def test_uncovered_feature_attached_to_first_subtask():
    def fake(_p: str) -> str:
        return json.dumps({"subtasks": [{"id": "a", "task": "A", "depends_on": [], "features": [1]}]})

    subs, _ = plan_decomposition([(1, "f1"), (2, "f2")], complete_fn=fake, max_subtasks=4)
    assert 2 in subs[0].features  # feature 2 was uncovered → attached
