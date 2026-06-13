"""Stage 1 validation against the real CooperBench flash set.

These run the *actual* flash feature specs through the full unified harness
(LocalEnv + DemoPolicy, so no API key / Docker), write CooperBench-compatible
outputs, and assert the layout is exactly what ``cooperbench eval`` discovers
and scores.  Skipped automatically when CooperBench isn't checked out.
"""

from __future__ import annotations

import json
import uuid

import pytest

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.local import LocalEnv
from cooperagents.eval.cooperbench import write_run_outputs
from cooperagents.harness import UnifiedHarness
from cooperagents.policies import DemoPolicy
from cooperagents.types import Assignment, TeamSpec

try:
    from cooperagents.eval.dataset import find_cooperbench, load_subset, read_feature

    find_cooperbench()
    HAVE_CB = True
except Exception:  # noqa: BLE001
    HAVE_CB = False

pytestmark = pytest.mark.skipif(not HAVE_CB, reason="CooperBench checkout not available")


def test_flash_subset_loads():
    items = load_subset("flash")
    assert len(items) == 50  # the flash set is 50 pairs
    assert all(len(it.features) == 2 for it in items)


def _run_one(item, *, max_agents):
    run_id = uuid.uuid4().hex[:8]
    feats = sorted(item.features)
    assignments = [
        Assignment(
            agent_id=f"agent{i + 1}",
            role="lead" if i == 0 else "member",
            feature_id=f,
            task=read_feature(item.repo, item.task_id, f),
        )
        for i, f in enumerate(feats)
    ]
    spec = TeamSpec(run_id=run_id, repo=item.repo, task_id=item.task_id, features=feats, assignments=assignments, max_agents=max_agents)
    harness = UnifiedHarness(bus=InMemoryBus(run_id))
    return harness.run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())


def test_first_flash_pairs_run_end_to_end(tmp_path):
    items = load_subset("flash")[:3]
    for item in items:
        res = _run_one(item, max_agents=3)  # allow a helper
        assert all(r.status == "submitted" for r in res.seeds.values())
        # Each feature owner produced a non-empty diff.
        for r in res.seeds.values():
            assert r.patch_lines > 0
        # The lead recruited exactly one helper.
        assert len(res.helpers) == 1

        log_dir = write_run_outputs(res, run_name="flash-validate", logs_dir=tmp_path, model="demo")
        # Layout matches CooperBench discovery: <run>/team/<repo>/<task>/f{a}_f{b}/
        assert log_dir.parent.parent.parent.name == "team"
        for f in res.features:
            assert (log_dir / f"agent{f}.patch").exists()
        data = json.loads((log_dir / "result.json").read_text())
        assert data["setting"] == "team"
        assert data["features"] == res.features


def test_layout_is_discoverable_by_cooperbench(tmp_path):
    """Use CooperBench's *own* discover_runs to confirm it finds our output."""
    cb = find_cooperbench()
    import subprocess

    item = load_subset("flash")[0]
    res = _run_one(item, max_agents=2)
    write_run_outputs(res, run_name="disc", logs_dir=tmp_path)

    # Run discover_runs inside CooperBench's environment (it isn't installed
    # in ours), so we shell into its checkout.
    script = (
        "import json,sys;"
        "from cooperbench.eval.runs import discover_runs;"
        f"runs=discover_runs('disc', logs_dir={str(tmp_path)!r}, dataset_dir={str(cb / 'dataset')!r});"
        "print(json.dumps([{'repo':r['repo'],'features':r['features'],'setting':r['setting']} for r in runs]))"
    )
    proc = subprocess.run(["uv", "run", "python", "-c", script], cwd=str(cb), capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"could not run CooperBench discover_runs: {proc.stderr[-300:]}")
    discovered = json.loads(proc.stdout.strip().splitlines()[-1])
    assert any(d["repo"] == item.repo and sorted(d["features"]) == res.features for d in discovered)
