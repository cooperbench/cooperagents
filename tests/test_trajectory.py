"""Collection and replay require no containers, network, or paid inference."""

import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from types import SimpleNamespace

import litellm
import pytest

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import ExecResult
from cooperagents.harness import _Coordinator
from cooperagents.trajectory import Trajectory, record_call, replay
from cooperagents.types import Assignment
from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModel
from cooperagents.workers.mini_swe_worker import run_mini_swe_agent


def test_worker_raw_io_compaction_and_replay(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")
    journal = Trajectory(tmp_path / "trajectory.jsonl")
    trace = partial(journal.emit, "integrator1")
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        if kwargs.get("tools") is None:
            message = {"role": "assistant", "content": "summary"}
        else:
            message = {
                "role": "assistant",
                "content": "act",
                "tool_calls": [
                    {
                        "id": f"call{len(calls)}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": "echo hello"})},
                    }
                ],
            }
        return litellm.ModelResponse(
            choices=[{"message": message}], usage={"prompt_tokens": 30000, "completion_tokens": 10, "total_tokens": 30010}
        )

    monkeypatch.setattr(litellm, "completion", complete)
    model = LitellmModel(model_name="openai/dummy", cost_tracking="ignore_errors", model_kwargs={"api_key": "secret-test-key"})
    model.trace = trace
    agent = DefaultAgent(
        model,
        SimpleNamespace(get_template_vars=lambda: {}),
        compaction_keep_recent_turns=1,
        system_template="system",
        instance_template="task",
    )
    agent.trace = trace
    agent.add_messages({"role": "system", "content": "system"}, {"role": "user", "content": "task"})
    agent.query()
    agent.add_messages({"role": "tool", "content": "early output", "tool_call_id": "call1"})
    agent.query()
    agent.add_messages({"role": "tool", "content": "later output", "tool_call_id": "call2"})
    before = journal._seq
    agent._compact_messages()
    agent._emergency_truncate()
    journal.close()
    raw = journal.path.read_text()
    assert "early output" in raw and "secret-test-key" not in raw
    assert any(call.get("tools") is None for call in calls)
    state = replay(journal.path)
    assert state["contexts"]["integrator1"] == agent.messages
    assert not state["pending_calls"]
    earlier = replay(journal.path, at_seq=before)
    assert any(m.get("content") == "early output" for m in earlier["contexts"]["integrator1"])
    rows = [json.loads(line) for line in raw.splitlines()]
    assert replay(journal.path, at_time=rows[before - 1]["time"])["seq"] == before


@pytest.mark.parametrize("remote", [False, True])
def test_actual_worker_finish_and_coordinator_io(monkeypatch, tmp_path, remote):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    journal = Trajectory(tmp_path / "trajectory.jsonl")
    sink = journal
    if remote:
        from test_observability import memory_trace

        sink, exporter = memory_trace(journal)
    responses = []

    def complete(**kwargs):
        message = {
            "role": "assistant",
            "content": "done",
            "tool_calls": [
                {
                    "id": "finish",
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})},
                }
            ],
        }
        response = litellm.ModelResponse(
            choices=[{"message": message}], usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
        )
        responses.append(response)
        return response

    monkeypatch.setattr(litellm, "completion", complete)
    env = SimpleNamespace(repo_path="/repo", execute=lambda *args, **kwargs: ExecResult("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", 0))
    result = run_mini_swe_agent(
        env,
        task="task",
        agent_id="agent1",
        role="lead",
        model_name="dummy",
        step_limit=3,
        cost_limit=5,
        trace=partial(sink.emit, "agent1"),
    )
    assert result.status == "submitted"
    import openai

    arguments = json.dumps({"recipient": "agent1", "content": "nudge"})
    tool_call = SimpleNamespace(function=SimpleNamespace(name="send_message", arguments=arguments))
    def completion(**kwargs):
        assert kwargs["tool_choice"] == "auto"
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == ["send_message"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))],
            model_dump=lambda **kw: {
                "choices": [{"message": {"content": None, "tool_calls": [{"function": {"name": "send_message", "arguments": arguments}}]}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            },
        )

    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda *a, **k: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion))),
    )
    coordinator = _Coordinator(
        {"agent1": env},
        "dummy",
        assignments=[Assignment(agent_id="agent1", role="lead", task="feature")],
        bus=InMemoryBus("test"),
        trace=partial(sink.emit, "coordinator"),
    )
    coordinator.register("agent1", SimpleNamespace(messages=result.messages))
    coordinator.decide(initial=True)
    assert coordinator.drain("agent1") == ["[coordinator] nudge"]
    if remote:
        sink.close()
        spans = exporter.get_finished_spans()
        roots = {s.name: s for s in spans if s.attributes.get("langfuse.internal.as_root")}
        assert set(roots) == {"agent1", "coordinator"}
        generations = [s for s in spans if s.attributes.get("langfuse.observation.type") == "generation"]
        assert len(generations) == 2
        assert all(json.loads(s.attributes["langfuse.observation.usage_details"])["input"] > 0 for s in generations)
        assert {s.parent.span_id for s in generations} == {s.context.span_id for s in roots.values()}
        assert all(s.attributes["session.id"] == sink.session_id for s in spans)
        assert all(s.attributes["langfuse.observation.type"] == "agent" for s in roots.values())
        assert any("prompt_tokens" in s.attributes.get("langfuse.observation.output", "") for s in generations)
    journal.close()
    state = replay(journal.path)
    assert state["contexts"]["agent1"] == result.messages
    assert not state["pending_calls"]
    rows = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert any(r["event"] == "response" and r["data"]["response"].get("stdout") for r in rows)
    assert {"request", "response", "decision", "nudge", "delivery"} <= {r["event"] for r in rows if r["actor"] == "coordinator"}


def test_concurrent_failures_and_recording_failure(tmp_path):
    journal = Trajectory(tmp_path / "trajectory.jsonl")

    def work(i):
        return record_call(partial(journal.emit, str(i)), lambda value: value, value=i)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(work, range(20))) == list(range(20))

    def fail():
        raise ValueError("failure")

    with pytest.raises(ValueError):
        record_call(partial(journal.emit, "coordinator"), fail)
    journal.close()
    assert not replay(journal.path)["pending_calls"]
    with pytest.raises(FileExistsError):
        Trajectory(journal.path)
    broken = Trajectory(tmp_path / "broken.jsonl")
    with pytest.raises(TypeError):
        broken.emit("worker", "invalid", value=object())
    with pytest.raises(RuntimeError, match="recording failed"):
        broken.close()


@pytest.mark.parametrize("n_workers", [2, 3])
def test_two_repair_attempts_and_collection_audit(monkeypatch, tmp_path, n_workers):
    import importlib.util
    from pathlib import Path

    from cooperagents.env.local import LocalEnv
    from cooperagents.eval.cooperbench import write_run_outputs
    from cooperagents.harness import UnifiedHarness
    from cooperagents.types import Assignment, TeamSpec

    def complete(**kwargs):
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "tool_calls": [
                            {
                                "id": "finish",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": json.dumps({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}),
                                },
                            }
                        ],
                    }
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 1},
        )

    monkeypatch.setattr(litellm, "completion", complete)
    monkeypatch.setattr("cooperagents.harness._tree_health", lambda env: False)
    spec = TeamSpec(
        run_id="test",
        repo="demo",
        task_id=1,
        features=[1, 2],
        worker="mini_swe",
        model="dummy",
        shared_workspace=True,
        seed_prior=False,
        coop_tools=True,
        repair_integrator=True,
        repair_attempts=2,
        assignments=[
            Assignment(agent_id=f"agent{i}", role="lead" if i == 1 else "member", feature_id=i, task="finish")
            for i in range(1, n_workers + 1)
        ],
    )
    directory = tmp_path / "logs/real/team/demo/1/f1_f2"
    journal = Trajectory(directory / "trajectory.jsonl.gz")
    journal.emit("harness", "pair_start")
    result = UnifiedHarness(trajectory=journal, checkpoint_dir=directory / "checkpoints").run(
        spec, env_factory=lambda _: LocalEnv.fresh()
    )
    journal.emit("harness", "pair_end")
    journal.close()
    assert set(result.seeds) == {*(f"agent{i}" for i in range(1, n_workers + 1)), "integrator1", "integrator2"}
    write_run_outputs(result, run_name="real", logs_dir=tmp_path / "logs")
    (tmp_path / "metadata.json").write_text(json.dumps({"pairs": ["demo:1:1,2"], "checkpoint_repair": True}))
    module_spec = importlib.util.spec_from_file_location("audit", Path(__file__).parents[1] / "scripts/audit_trajectories.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    audited = module.audit(tmp_path)
    assert audited["workers"] == n_workers
    if n_workers == 3:
        checkpoint = directory / "checkpoints/worker-agent3"
        manifest = checkpoint / "manifest.json"
        saved = manifest.read_bytes()
        manifest.unlink()
        with pytest.raises(FileNotFoundError):
            module.audit(tmp_path)
        manifest.write_bytes(saved)
        patch = checkpoint / "raw.patch"
        saved = patch.read_bytes()
        patch.write_text("corrupted")
        with pytest.raises(ValueError, match="checksum mismatch"):
            module.audit(tmp_path)
        patch.write_bytes(saved)
        assert "worker-agent3" in audited["entries"][0]["checkpoints"]
    trajectory = directory / "agent1_traj.json"
    damaged = json.loads(trajectory.read_text())
    damaged["messages"] = []
    trajectory.write_text(json.dumps(damaged))
    with pytest.raises(ValueError, match="replay differs"):
        module.audit(tmp_path)
