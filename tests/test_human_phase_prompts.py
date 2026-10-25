"""Human phase prompts change without altering auto-prompt or feature requirements."""

import copy
import socket

import pytest

from cooperagents.env.local import LocalEnv
from cooperagents.workers.mini_swe_worker import BusComm, run_mini_swe_agent
from test_coordinator import coordinator, tool


@pytest.mark.parametrize("variant", ["human-prompt", "auto-prompt"])
def test_worker_transition_compaction_and_later_notebook(tmp_path, monkeypatch, variant):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    c = coordinator(coordination_mode="group-chat-planning", coordination_variant=variant, notebook_path=tmp_path / "notebook.md")
    c.start_planning()
    monkeypatch.setattr(c, "mark_finished", lambda *args: None)  # Capture construction without ending the test actors.
    workers = []
    feature = "Exact original feature requirement: Keep CASE and {{literal braces}} unchanged."

    class Captured(Exception):
        pass

    def capture(agent):
        workers.append(agent)
        raise Captured

    env = LocalEnv.fresh()
    try:
        for aid in c._assignments:
            with pytest.raises(Captured):
                run_mini_swe_agent(
                    env,
                    task=feature,
                    agent_id=aid,
                    role="member",
                    model_name="dummy",
                    step_limit=20,
                    cost_limit=0,
                    monitor=c,
                    comm=BusComm(c._bus, aid, group=c),
                    git_share=True,
                    on_start=capture,
                )
        first = workers[0]
        original = copy.deepcopy(first.messages[:2])
        for w in workers:
            c.publish(w.agent_id, "group", "Inspected relevant source; propose independent edits.")
            w.add_messages(w.model.format_message(role="assistant", content="Inspected source; future shared plan."))
        c.update_notebook("One writer per definition; independent files; agent1 verifies combined imports.")
        for w in workers:
            c.before_query(w.agent_id, w)
            assert feature in w.messages[1]["content"]
            if variant == "human-prompt":
                assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in w.messages[1]["content"]
            assert c.confirm_plan(w.agent_id, {"version": c._version}, sole=True)["returncode"] == 0
        c._observed_confirmations = dict(c._confirmations)
        c.finish_planning(c._version)
        with c._condition:
            for w in workers:
                c._restore_plan_locked(w.agent_id, w)
        assert c.phase == "implementation"
        for w in workers:
            assert feature in w.messages[1]["content"]
            assert "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in w.messages[1]["content"]
            assert any(m["content"] == "Inspected source; future shared plan." for m in w.messages)
        if variant == "auto-prompt":
            assert first.messages[:2] == original
        else:
            assert "CURRENT PHASE — IMPLEMENTATION" in first.messages[0]["content"]
            assert not any(m["content"].startswith("[PLANNING MODE]") for m in first.messages)
        c._complete = lambda _: [tool("update_notebook", content="Revised implementation plan; agent2 verifies combined imports.")]
        c.decide()
        assert c._notebook.startswith("Revised implementation plan")
        assert c._version > c._final_plan["version"]
        # Compaction keeps the system/task prefix; the host restores only the current phase.
        first.messages[:] = first.messages[:2]
        first._compaction_count += 1
        c.before_query(first.agent_id, first)
        assert "[IMPLEMENTATION MODE]" in first.messages[-1]["content"]
        if variant == "human-prompt":
            assert "follow the latest notebook" in first.messages[-1]["content"].lower()
            assert "CURRENT PHASE — IMPLEMENTATION" in first.messages[0]["content"]
            assert all("[PLANNING MODE]" not in m["content"] for m in first.messages)
        assert c._notebook.startswith("Revised implementation plan")
    finally:
        env.cleanup()


def test_human_coordinator_uses_phase_specific_protocol(tmp_path):
    from test_planning import planning, publish_plan

    requests = []
    c = planning(tmp_path, coordination_variant="human-prompt", complete=lambda p: requests.append(p) or [])
    c.decide(initial=True)
    first = requests[-1].split("OBSERVATION:")[0]
    assert "No worker has entered implementation yet" in first
    assert "Keep implementation moving" not in first
    assert "Before requesting a revert" in first  # Evidence safety is retained.
    assert "planning.candidate_version and planning.confirmations" in first
    assert "immutable" not in first
    publish_plan(c)
    for aid in c._assignments:
        c.confirm_plan(aid, {"version": c._version}, sole=True)
    c._observed_confirmations = dict(c._confirmations)
    c.finish_planning(c._version)
    c.decide(initial=True)
    last = requests[-1].split("OBSERVATION:")[0]
    assert "Keep implementation moving" in last
    assert "PLANNING PROTOCOL" not in last
    assert "finish_planning" not in last
    assert "Workers have not yet inspected" not in last
    assert "The coordinator may update the notebook" in last
