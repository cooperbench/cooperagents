"""Offline end-to-end coverage of the study's concurrent coop-tools path."""

import gzip
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.types import Assignment, TeamSpec


def load_bench():
    spec = importlib.util.spec_from_file_location("bench_persist_test", Path(__file__).parents[1] / "scripts/bench_compare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_concurrent_persist_and_message_trajectory(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("COOPER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    bench = load_bench()
    monkeypatch.setattr(bench, "_load_env", lambda: None)
    monkeypatch.setattr(bench, "read_feature", lambda repo, task, fid: f"TASK_FEATURE_{fid}")
    monkeypatch.setattr(bench, "image_name", lambda *a: "fake")
    source = LocalEnv.fresh()
    monkeypatch.setattr(bench, "task_environment", lambda *a, **k: LocalEnv.clone(source.repo_path))
    evaluations = []
    monkeypatch.setattr(bench, "run_eval", lambda *a, **k: evaluations.append((a, k)) or SimpleNamespace(
        stdout="", stderr="", check_returncode=lambda: None))
    barrier = threading.Barrier(2, timeout=8)
    sent = threading.Event()
    lock = threading.Lock()
    counts = {"agent1": 0, "agent2": 0}
    observations = []
    content = "coordination: café ✓"

    def complete(**kwargs):
        messages = kwargs["messages"]
        all_text = " ".join(str(m.get("content", "")) for m in messages)
        actor = "agent1" if "TASK_FEATURE_1" in all_text else "agent2"
        with lock:
            counts[actor] += 1
            number = counts[actor]
        if number == 1:
            barrier.wait()
        if actor == "agent1" and number == 1:
            name, args = "send_message", {"recipient": "agent2", "content": content}
        elif actor == "agent1":
            sent.set()
            name, args = "bash", {"command": "printf 'agent1\\n' > agent1.txt; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}
        elif number == 1:
            assert sent.wait(8), "sender did not reach its next call"
            name, args = "bash", {"command": "printf 'agent2\\n' > agent2.txt"}
        else:
            observations.append(all_text)
            name, args = "bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "work", "tool_calls": [{
                "id": f"{actor}-{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}
            }]}}], usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11})

    monkeypatch.setattr(litellm, "completion", complete)
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", "--pairs", "demo:1:1,2", "--team-only", "--max-agents", "2",
                                     "--coop-tools", "--no-seed", "--record-trajectory", "--persist-worker-patches",
                                     "--team-name", "study", "--log-dir", str(tmp_path)])
    try:
        bench.main()
    finally:
        source.cleanup()
    path = tmp_path / "study/team/demo/1/f1_f2"
    assert counts == {"agent1": 2, "agent2": 2}
    assert evaluations, "official evaluation was skipped"
    assert not (path / "checkpoints").exists()
    for actor in ("agent1", "agent2"):
        assert f"{actor}.txt" in (path / f"{actor}.raw.patch").read_text()
        assert f"{actor}.txt" in (path / f"{actor}.stripped.patch").read_text()
        assert f"{actor}.txt" in (path / "integrated.patch").read_text()
    metrics = json.loads((path / "result.json").read_text())["metrics"]
    assert metrics["merged_state"] == {"conflict": False, "merge_mode": "3way_clean"}
    assert "merged_state_capture" not in metrics
    assert any(f"[Message from agent1]: {content}" in text for text in observations)
    with gzip.open(path / "trajectory.jsonl.gz", "rt") as handle:
        rows = [json.loads(line) for line in handle]
    starts = [r["seq"] for r in rows if r["event"] == "agent_start"]
    ends = [r["seq"] for r in rows if r["event"] == "agent_end"]
    assert len(starts) == len(ends) == 2 and max(starts) < min(ends)
    requests = [r for r in rows if r["event"] == "request" and r["actor"] in counts and "model" in r["data"]["request"]]
    assert len(requests) == 4
    assert all("send_message" in [t["function"]["name"] for t in r["data"]["request"]["tools"]] for r in requests)
    sent_rows = [r for r in rows if r["event"] == "message_sent"]
    delivered = [r for r in rows if r["event"] == "message_delivered"]
    assert len(sent_rows) == len(delivered) == 1
    assert sent_rows[0]["actor"] == "agent1" and delivered[0]["actor"] == "agent2"
    assert sent_rows[0]["data"]["recipient"] == "agent2" and delivered[0]["data"]["sender"] == "agent1"
    for row in sent_rows + delivered:
        assert row["data"]["content"] == content and row["data"]["bytes"] == len(content.encode("utf-8"))
        assert isinstance(row["data"]["step"], int)


@pytest.mark.parametrize("flags", [
    ["--team-only", "--no-seed", "--capture-merged-state"],
    ["--team-only"], ["--solo-only", "--no-seed"], ["--no-seed"],
])
def test_persist_cli_rejects_invalid_combinations(monkeypatch, flags):
    bench = load_bench()
    monkeypatch.setattr(bench, "_load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", "--persist-worker-patches", *flags])
    with pytest.raises(SystemExit):
        bench.main()


def test_harness_rejects_seeded_or_combined_capture(tmp_path):
    spec = TeamSpec(run_id="test", repo="demo", task_id=1, features=[1, 2], shared_workspace=True,
                    assignments=[Assignment("agent1", "one", feature_id=1), Assignment("agent2", "two", feature_id=2)])
    with pytest.raises(ValueError, match="no-seed"):
        UnifiedHarness(persist_worker_patches=True).run(spec, env_factory=lambda _: pytest.fail("must reject before work"))
    with pytest.raises(ValueError, match="mutually exclusive"):
        UnifiedHarness(persist_worker_patches=True, capture_merged_state=True, checkpoint_dir=tmp_path / "cp")


@pytest.mark.parametrize("coop_tools, conflict", [(False, False), (True, True)])
def test_persist_reports_real_merge_modes(tmp_path, coop_tools, conflict):
    from cooperagents.eval.cooperbench import write_run_outputs
    from cooperagents.llm import Action, ScriptedLLM

    assignments = [Assignment("agent1", "one", feature_id=1), Assignment("agent2", "two", feature_id=2)]
    spec = TeamSpec(run_id="merge", repo="demo", task_id=1, features=[1, 2], assignments=assignments,
                    shared_workspace=True, seed_prior=False, coop_tools=coop_tools)
    scripts = {}
    for i in (1, 2):
        name = "shared.txt" if conflict else f"agent{i}.txt"
        scripts[f"agent{i}"] = [Action(tool="write_file", args={"path": name, "content": f"worker {i}\n"}), Action(tool="finish")]
    source = LocalEnv.fresh()
    try:
        result = UnifiedHarness(persist_worker_patches=True).run(spec, env_factory=lambda _: LocalEnv.clone(source.repo_path),
                                                                llm=ScriptedLLM(scripts))
    finally:
        source.cleanup()
    assert result.metrics["merged_state"] == {"conflict": conflict, "merge_mode": "apply_chain" if conflict else "3way_clean"}
    out = write_run_outputs(result, run_name="persist", logs_dir=tmp_path)
    for i in (1, 2):
        assert f"worker {i}" in (out / f"agent{i}.raw.patch").read_text()
        assert f"worker {i}" in (out / f"agent{i}.stripped.patch").read_text()
    assert not (out / "checkpoints").exists()
