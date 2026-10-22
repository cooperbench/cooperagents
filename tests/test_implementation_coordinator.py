"""Implementation ablation skips decisions without disabling planning or peer delivery."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cooperagents.harness import UnifiedHarness, _TeammatePoller
from cooperagents.types import TeamSpec
from test_coordinator import coordinator, message, tool
from test_planning import planning, publish_plan
from test_worker_reminder import worker


@pytest.mark.parametrize("mode", ["direct", "group-chat"])
@pytest.mark.parametrize("enabled", [True, False])
def test_initial_and_ongoing_implementation_decisions(tmp_path, monkeypatch, mode, enabled):
    calls = []
    events = []
    c = coordinator(
        coordination_mode=mode,
        notebook_path=tmp_path / "notebook.md",
        coordinator_implementation_enabled=enabled,
        complete=lambda prompt: calls.append(prompt) or [message("model advice")],
        trace=lambda event, **data: events.append({"event": event, **data}),
    )
    if not enabled:
        monkeypatch.setattr(c, "_dirty_files", lambda _: pytest.fail("Disabled decision collected code observations"))
    c.decide(initial=True)
    c.decide(initial=True)  # Also cover later forced/initial-labelled calls.
    assert len(calls) == c._coordinator_calls == (2 if enabled else 0)
    if not enabled:
        assert not any(e["event"] in ("observation", "decision") for e in events)
        assert c._version == 0 and not c._chat
    assert c.planning_metrics()["coordinator_implementation_enabled"] is enabled


@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
def test_real_worker_queries_receive_peer_chat_and_notebook_without_decisions(tmp_path, variant):
    events = []
    c = coordinator(
        coordination_mode="group-chat", coordination_variant=variant,
        notebook_path=tmp_path / "notebook.md", coordinator_implementation_enabled=False,
        complete=lambda _: pytest.fail("Coordinator model called during implementation"),
        trace=lambda event, **data: events.append({"event": event, **data}),
    )
    a, calls = worker(c)
    peer, peer_calls = worker(c, "agent1")
    for agent in (a, peer):
        agent.team_poller = _TeammatePoller(agent.agent_id, c._envs)
        agent.team_poller.watch_coordinator(c)
        agent.execute_actions = lambda _: []
    original = c._notebook_path.read_text()
    c.publish("agent1", "agent2", "I own the initializer; please implement tests.")
    c.publish("agent2", "group", "Confirmed: I will implement tests.")
    c.decide(initial=True)
    a.step()
    peer.step()
    for request in (calls[-1], peer_calls[-1]):
        text = json.dumps(request)
        assert "I own the initializer" in text and "Confirmed: I will implement tests" in text
        assert "/coordination/notebook.md" in text
    assert c._notebook_path.read_text() == original
    assert c.worker_reminder("agent2", 10) is not None
    assert c._coordinator_calls == 0
    deliveries = [e for e in events if e["event"] == "group_delivery"]
    assert {e["target"] for e in deliveries} == {"agent1", "agent2"}


@pytest.mark.parametrize("forced", [False, True])
def test_planning_and_finalization_still_call_model_then_stop(tmp_path, forced):
    calls = []
    c = planning(tmp_path, coordinator_implementation_enabled=False, complete=lambda p: calls.append(p) or [])
    c.decide(initial=True)
    if forced:
        c._planning_steps["agent1"] = 200
        c._complete = lambda p: calls.append(p) or [tool("update_notebook", content="Final plan: independent files; verify imports.")]
    else:
        publish_plan(c)
        for aid in c._assignments:
            assert c.confirm_plan(aid, {"version": c._version}, sole=True)["returncode"] == 0
        c._complete = lambda p: calls.append(p) or [tool("finish_planning", version=c._version)]
    c.decide()
    assert c.phase == "implementation"
    assert c._planning_outcome == ("forced" if forced else "agreed")
    assert len(calls) == 2
    final = dict(c._final_plan)
    with c._condition:
        for aid, agent in c._agents.items():
            c._restore_plan_locked(aid, agent)
    for aid, agent in c._agents.items():
        c.before_query(aid, agent)
        assert "[IMPLEMENTATION MODE]" in json.dumps(agent.messages)
        assert final["content"] in json.dumps(agent.messages)
    c.publish("agent1", "group", "Starting implementation within the agreed ownership.")
    c.decide(initial=True)
    c.decide()
    assert len(calls) == c._coordinator_calls == 2
    assert c._final_plan == final
    assert "Starting implementation" in "\n".join(c.drain("agent2"))


@pytest.mark.parametrize("enabled", [True, False])
def test_benchmark_forwards_switch(monkeypatch, tmp_path, enabled):
    spec = importlib.util.spec_from_file_location("bench_compare", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    seen = []
    monkeypatch.setattr(bench, "read_feature", lambda *args: "original feature")
    monkeypatch.setattr(bench, "write_run_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bench.UnifiedHarness, "run",
        lambda self, *args, **kwargs: (
            seen.append(self.coordinator_implementation_enabled)
            or SimpleNamespace(duration_seconds=0, total_steps=0, helpers={}, integrated=SimpleNamespace(patch=""))
        ),
    )
    bench.run_team(
        bench.WorkItem("go_chi_task", 26, [1, 2]), run_name="ablation", logs_dir=tmp_path,
        step_limit=30, max_agents=2, coordinator=True, coordinator_implementation_enabled=enabled,
    )
    assert seen == [enabled]


def test_harness_rejects_switch_without_coordinator():
    spec = TeamSpec(run_id="invalid", repo="test", task_id=1, features=[1, 2], worker="mini_swe", objective="task")
    with pytest.raises(ValueError, match="requires coordinator enabled"):
        UnifiedHarness(coordinator_implementation_enabled=False).run(spec, env_factory=lambda _: pytest.fail("Created environment"))


@pytest.mark.parametrize(
    "script,arguments",
    [
        ("scripts/bench_compare.py", []),
        ("scripts/nlp_cluster/submit.py", ["--mode", "real", "--no-coordinator"]),
        ("scripts/nlp_cluster/submit.py", []),
    ],
)
def test_cli_rejects_ineffective_switch(script, arguments):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / script), "--no-implementation-coordinator", *arguments],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2 and "--no-implementation-coordinator requires" in result.stderr


@pytest.mark.parametrize("enabled", [None, "0", "1"])
def test_job_forwards_switch_using_default_enabled(enabled):
    text = (Path(__file__).parents[1] / "scripts/nlp_cluster/job.sh").read_text()
    line = next(line for line in text.splitlines() if "COOPER_COORDINATOR_IMPLEMENTATION_ENABLED" in line)
    setup = (
        "unset COOPER_COORDINATOR_IMPLEMENTATION_ENABLED\n"
        if enabled is None else f"COOPER_COORDINATOR_IMPLEMENTATION_ENABLED={enabled}\n"
    )
    result = subprocess.run(
        ["bash", "-c", setup + "args=()\n" + line + '\nprintf "%s" "${args[*]}"'],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == ("--no-implementation-coordinator" if enabled == "0" else "")
