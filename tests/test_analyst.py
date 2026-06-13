"""Analyst: failure-evidence → new hypotheses (scripted, no API)."""

from __future__ import annotations

import json

from cooperagents.eval.analyst import Failure, gather_failures, propose_hypotheses
from cooperagents.eval.dataset import WorkItem


def test_propose_parses_and_flags():
    canned = json.dumps(
        {
            "diagnoses": [{"pair": "r/1 [1, 2]", "failure_mode": "spec-ambiguous", "seam_addressable": False}],
            "proposals": [
                {
                    "hypothesis": "Have the team probe the public API surface first",
                    "seam_addressable": True,
                    "rationale": "wrong names recur",
                    "test_plan": "A/B vs baseline",
                    "failure_modes": ["wrong API name"],
                },
            ],
        }
    )
    out = propose_hypotheses([Failure("r", 1, [1, 2], "spec", "diff", "undefined: Params")], complete_fn=lambda _p: canned)
    assert out["diagnoses"][0]["seam_addressable"] is False
    assert len(out["proposals"]) == 1
    assert out["proposals"][0].seam_addressable is True
    assert "API" in out["proposals"][0].hypothesis


def test_gather_failures_reads_evidence(tmp_path):
    d = tmp_path / "run" / "team" / "go_chi_task" / "26" / "f1_f2"
    d.mkdir(parents=True)
    (d / "eval.json").write_text(
        json.dumps({"both_passed": False, "feature1": {"passed": True}, "feature2": {"passed": False, "test_output": "undefined: Params"}})
    )
    (d / "integrated.patch").write_text("diff --git a/x b/x\n+code\n")
    items = [WorkItem("go_chi_task", 26, [1, 2])]
    # cooperbench dir absent in tmp → spec read fails gracefully to ""
    fails = gather_failures(tmp_path, "run", "team", items, cooperbench_dir=str(tmp_path))
    assert len(fails) == 1
    assert "Params" in fails[0].error
    assert "code" in fails[0].diff


def test_passing_pairs_excluded(tmp_path):
    d = tmp_path / "run" / "team" / "r" / "1" / "f1_f2"
    d.mkdir(parents=True)
    (d / "eval.json").write_text(json.dumps({"both_passed": True, "feature1": {"passed": True}, "feature2": {"passed": True}}))
    fails = gather_failures(tmp_path, "run", "team", [WorkItem("r", 1, [1, 2])], cooperbench_dir=str(tmp_path))
    assert fails == []
