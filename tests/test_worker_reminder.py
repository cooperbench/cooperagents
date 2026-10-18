"""Fixed-cadence reminders use real worker query hooks, without model APIs."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from cooperagents.harness import UnifiedHarness
from cooperagents.types import Assignment
from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
from test_coordinator import coordinator


def worker(c, aid="agent2", *, keep=2):
    calls = []
    model = SimpleNamespace(
        format_message=lambda **kwargs: kwargs,
        query=lambda messages: (
            calls.append(copy.deepcopy(messages)) or {"role": "assistant", "content": "continue"}
        ),
    )
    agent = DefaultAgent(
        model, SimpleNamespace(get_template_vars=lambda: {}), agent_id=aid,
        system_template="system", instance_template="task", cost_limit=0,
        compaction_enabled=False, compaction_keep_recent_turns=keep,
    )
    agent.add_messages({"role": "system", "content": "system"}, {"role": "user", "content": "original task"})
    if c is not None:
        agent.coordinator = c
        c.register(aid, agent)
    return agent, calls


def setup(tmp_path, **kwargs):
    c = coordinator(notebook_path=tmp_path / "notebook.md", **kwargs)
    c._assignments = {
        aid: Assignment(agent_id=aid, task=f"original feature {fid}", feature_id=fid)
        for aid, fid in [("agent1", 6), ("agent2", 7)]
    }
    if c._planning:
        c.start_planning()
    return c


def reminders(messages):
    return [m for m in messages if m.get("role") == "user" and m.get("content", "").startswith("[HARNESS REMINDER]")]


@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
@pytest.mark.parametrize("mode", ["direct", "group-chat", "group-chat-planning"])
def test_periodic_query_injection_and_independent_workers(tmp_path, variant, mode):
    c = setup(tmp_path, coordination_variant=variant, coordination_mode=mode)
    a, calls = worker(c)
    peer, peer_calls = worker(c, "agent1")
    inserted = []
    for decision in range(1, 31):
        before = len(reminders(a.messages))
        a.query()
        if len(reminders(a.messages)) > before:
            inserted.append(decision)
            card = calls[-1][-1]
            assert card["role"] == "user"
            assert "You are agent2, a worker." in card["content"]
            assert "Your assigned task is feature 7." in card["content"]
            assert f"Current phase: {c.phase}." in card["content"]
            assert "- Version: 0" in card["content"]
            assert "- Path: /coordination/notebook.md" in card["content"]
            assert c._notebook not in card["content"]
    assert inserted == [10, 20, 30]
    assert peer.n_calls == 0
    for _ in range(10):
        peer.query()
    assert len(reminders(peer.messages)) == 1
    assert "You are agent1" in peer_calls[-1][-1]["content"]
    assert "feature 6" in peer_calls[-1][-1]["content"]


def test_update_phase_and_compaction_do_not_add_or_reset_reminders(tmp_path):
    c = setup(tmp_path, coordination_mode="group-chat-planning")
    a, calls = worker(c)
    peer, _ = worker(c, "agent1")
    for _ in range(9):
        a.query()
    for aid in c._assignments:
        c.publish(aid, "group", "Inspected source; independent regions")
    c.update_notebook("OWNERSHIP MARKER: agent1 initializer; agent2 card tests")
    a.query()
    assert "- Version: 1" in calls[-1][-1]["content"]
    assert "Current phase: planning." in calls[-1][-1]["content"]
    peer.query()
    for aid in c._assignments:
        assert c.confirm_plan(aid, {"version": 1}, sole=True)["returncode"] == 0
    c.decide()  # Coordinator must observe both confirmations before finishing.
    c.finish_planning(1)
    # Deliver the existing final notebook to both before requesting implementation.
    with c._condition:
        for aid, agent in c._agents.items():
            c._restore_plan_locked(aid, agent)
    a.query()
    assert a.n_calls == 11 and len(reminders(a.messages)) == 1
    assert not calls[-1][-1]["content"].startswith("[HARNESS REMINDER]")
    c.update_notebook("OWNERSHIP UPDATED: same responsibilities")
    assert c._version == 2
    for _ in range(8):
        a.query()
    assert a.n_calls == 19 and len(reminders(a.messages)) == 1
    # Force the real proactive compaction path exactly at decision 20.
    a.config.compaction_enabled = True
    a._last_prompt_tokens = 30000
    a.model.summarize_context = lambda *args, **kwargs: {"role": "assistant", "content": "compacted history"}
    a.query()
    assert a._compaction_count == 1 and a.n_calls == 20
    assert calls[-1][-1]["content"].startswith("[HARNESS REMINDER]")
    assert "- Version: 2" in calls[-1][-1]["content"]
    assert "Current phase: implementation." in calls[-1][-1]["content"]
    # A compaction at decision 21 must not add a reminder.
    a._last_prompt_tokens = 30000
    a.query()
    assert a._compaction_count == 2 and a.n_calls == 21
    assert not calls[-1][-1]["content"].startswith("[HARNESS REMINDER]")


@pytest.mark.parametrize("decision", [9, 10])
@pytest.mark.parametrize("keep", [0, 2])
def test_overflow_retry_preserves_only_scheduled_reminder(tmp_path, decision, keep):
    c = setup(tmp_path, coordination_mode="group-chat")
    a, _ = worker(c, keep=keep)
    a.n_calls = decision - 1
    a.add_messages({"role": "assistant", "content": "previous decision"})
    calls = []

    def query(messages):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            raise RuntimeError("context length exceeded")
        return {"role": "assistant", "content": "retry completed"}

    a.model.query = query
    a.query()
    assert a.n_calls == decision and len(calls) == 2
    assert len(reminders(calls[0])) == len(reminders(calls[1])) == (1 if decision == 10 else 0)
    if decision == 10:
        assert reminders(calls[0]) == reminders(calls[1])


@pytest.mark.parametrize("interval", [0, 3])
def test_disabled_and_custom_interval(tmp_path, interval):
    c = setup(tmp_path, worker_reminder_interval=interval)
    a, _ = worker(c)
    for _ in range(10):
        a.query()
    assert len(reminders(a.messages)) == (0 if interval == 0 else 3)
    assert UnifiedHarness(worker_reminder_interval=interval).worker_reminder_interval == interval


def test_no_coordinator_or_notebook_and_missing_feature():
    a, calls = worker(None)
    for _ in range(10):
        a.query()
    assert not reminders(a.messages)
    c = coordinator()  # no notebook or feature IDs
    a, calls = worker(c)
    a.n_calls = 9
    a.query()
    card = calls[-1][-1]["content"]
    assert "No coordination notebook is enabled." in card
    assert "/coordination/notebook.md" not in card
    assert "Version:" not in card and "feature None" not in card
    assert "original task provided at the start" in card


def test_negative_interval_rejected():
    for factory in [UnifiedHarness, coordinator]:
        with pytest.raises(ValueError, match="nonnegative"):
            factory(worker_reminder_interval=-1)
    assert UnifiedHarness().worker_reminder_interval == 10


@pytest.mark.parametrize("interval", [0, 7])
def test_benchmark_passes_interval_to_harness(monkeypatch, tmp_path, interval):
    spec = importlib.util.spec_from_file_location("bench_compare", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    seen = []
    monkeypatch.setattr(bench, "read_feature", lambda *args: "original feature")
    monkeypatch.setattr(bench, "write_run_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bench.UnifiedHarness, "run",
        lambda self, *args, **kwargs: (
            seen.append(self.worker_reminder_interval)
            or SimpleNamespace(duration_seconds=0, total_steps=0, helpers={}, integrated=SimpleNamespace(patch=""))
        ),
    )
    bench.run_team(
        bench.WorkItem("go_chi_task", 26, [1, 2]), run_name="reminder", logs_dir=tmp_path,
        step_limit=30, max_agents=2, coordinator=True, worker_reminder_interval=interval,
    )
    assert seen == [interval]
