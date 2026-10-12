"""Curated plans bypass model planning while retaining native input and delivery guarantees."""

import copy
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import ExecResult
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness, _Coordinator
from cooperagents.initial_plan import InitialPlan
from cooperagents.trajectory import Trajectory, open_events
from cooperagents.types import Assignment, TeamSpec


@pytest.fixture
def bundle():
    return dict(
        schema_version=1,
        synthetic=True,
        pair="fixture_task:1:1,2",
        source_base_commit="a" * 40,
        feature_sha256={str(i): hashlib.sha256(f"feature{i}".encode()).hexdigest() for i in (1, 2)},
        content="agent1 owns a.py; agent2 owns b.py. No shared APIs. Verify combined imports.",
        worker_summaries={aid: f"## CURRENT PLAN\n{aid}'s planning memory. No edits or tests yet."
                          for aid in ("agent1", "agent2")},
        history=[
            dict(sender=s, addressed_to="group", content=f"{s} accepts the shared contract; tests pending.")
            for s in ("coordinator", "agent1", "agent2")
        ],
    )


def save_plan(tmp_path, bundle):
    p = tmp_path / "fixture_task-1-f1_f2.json"
    p.write_text(json.dumps(bundle))
    return InitialPlan.load(p, expected_pair="fixture_task:1:1,2")


@pytest.mark.parametrize(
    "change",
    [
        {"synthetic": False},
        {"pair": "fixture_task:2:1,2"},
        {"content": "x" * 8001},
        {"source_base_commit": "main"},
        {"worker_summaries": {"agent1": "missing peer"}},
        {"worker_summaries": {"agent1": " ", "agent2": "text"}},
        {"worker_summaries": {"agent1": "x" * 8001, "agent2": "text"}},
        {"worker_summaries": ["wrong shape"]},
        {"feature_sha256": {"1": "a" * 64}},
        {"history": [dict(sender="agent1", addressed_to="private", content="text")]},
        {"history": [dict(sender="agent1", addressed_to="group", content=" ")]},
    ],
)
def test_invalid_bundles_fail_before_run(tmp_path, bundle, change):
    bundle.update(change)
    with pytest.raises(ValueError):
        save_plan(tmp_path, bundle)


def test_stale_spec_or_wrong_arm_fails_before_environment(tmp_path, bundle):
    plan = save_plan(tmp_path, bundle)
    with pytest.raises(ValueError, match="auto-prompt group-chat"):
        UnifiedHarness(initial_plan=plan)
    h = UnifiedHarness(initial_plan=plan, coordination_mode="group-chat", coordinator_notebook_path=tmp_path / "notebook.md")
    spec = TeamSpec(
        run_id="stale",
        repo="fixture_task",
        task_id=1,
        features=[1, 2],
        worker="mini_swe",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        coordinator=True,
        assignments=[Assignment(agent_id=f"agent{i}", role="member", feature_id=i, task="changed") for i in (1, 2)],
    )
    with pytest.raises(ValueError, match="different feature"):
        h.run(spec, env_factory=lambda _: pytest.fail("must validate before environment creation"))


@pytest.mark.parametrize("inject", [False, True])
def test_real_loop_starts_with_clean_base_full_budget_and_native_tools(tmp_path, monkeypatch, bundle, inject):
    import litellm

    source = LocalEnv.fresh()
    try:
        bundle["source_base_commit"] = source._base_commit
        plan = save_plan(tmp_path, bundle) if inject else None
        path = tmp_path / "coordination/notebook.md"
        trace = Trajectory(tmp_path / "trajectory.jsonl.gz")
        requests, envs, coordinator_calls = [], {}, []
        roster = [Assignment(agent_id=f"agent{i}", role="member", feature_id=i, task=f"feature{i}") for i in (1, 2)]

        def complete(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            content = json.dumps(kwargs["messages"])
            assert {t["function"]["name"] for t in kwargs["tools"]} == {"bash", "send_message"}
            assert ("[IMPLEMENTATION MODE]" in content) == inject
            assert "synthetic" not in content.lower()
            assert "injected" not in content.lower()
            assert "author-selected" not in content.lower()
            if inject:
                assert bundle["content"] in content
                own = next(a.agent_id for a in roster if a.task in kwargs["messages"][1]["content"])
                summaries = [m for m in kwargs["messages"] if m.get("role") == "assistant"]
                assert len(summaries) == 1 and summaries[0]["content"] == bundle["worker_summaries"][own]
                other = "agent2" if own == "agent1" else "agent1"
                assert bundle["worker_summaries"][other] not in content
                summary_index = kwargs["messages"].index(summaries[0])
                assert kwargs["messages"][summary_index + 1]["content"].startswith("[IMPLEMENTATION MODE]")
            assert all(not e.execute("git status --porcelain").stdout.strip() for e in envs.values())
            return litellm.ModelResponse(
                choices=[
                    {
                        "message": {
                            "role": "assistant",
                            "content": "offline submit",
                            "tool_calls": [
                                {
                                    "id": "submit",
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

        def factory(aid):
            env = LocalEnv.clone(source.repo_path)
            execute = env.execute
            env.execute = lambda command, **kwargs: (
                ExecResult(path.read_text(), 0) if command == "cat /coordination/notebook.md" else execute(command, **kwargs)
            )
            envs[aid] = env
            return env

        monkeypatch.setenv("OPENAI_API_KEY", "offline-only")
        monkeypatch.setattr(litellm, "completion", complete)
        h = UnifiedHarness(
            step_limit=400,
            command_timeout=10,
            coordination_mode="group-chat",
            initial_plan=plan,
            coordinator_implementation_enabled=False,
            coordinator_notebook_path=path,
            coordinator_complete=lambda p: coordinator_calls.append(p) or [],
            trajectory=trace,
        )
        spec = TeamSpec(
            run_id="injection",
            repo="fixture_task",
            task_id=1,
            features=[1, 2],
            assignments=roster,
            worker="mini_swe",
            shared_workspace=True,
            coop_tools=True,
            seed_prior=False,
            coordinator=True,
            agent_time_limit=1200,
        )
        result = h.run(spec, env_factory=factory)
        assert result.metrics["coordinator_calls"] == 0
        trace.close()
        rows = [json.loads(line) for line in open_events(tmp_path / "trajectory.jsonl.gz")]
        calls = [r for r in rows if r["event"] == "worker_request"]
        assert len(requests) == len(calls) == 2
        assert all(c["data"]["phase"] == "implementation" and c["data"]["steps_used"] == 0 for c in calls)
        assert all(1190 <= c["data"]["seconds_remaining"] <= 1200 for c in calls)
        assert all(a.status == "submitted" and a.steps == 1 for a in result.seeds.values())
        if inject:
            assert coordinator_calls == []  # no initial planning-model decision
            deliveries = [r for r in rows if r["event"] == "plan_delivery"]
            assert len(deliveries) == 2 and max(r["seq"] for r in deliveries) < min(r["seq"] for r in calls)
            assert len({r["data"]["sha256"] for r in deliveries}) == 1
            memories = [r for r in rows if r["event"] == "worker_summary_delivery"]
            assert len(memories) == 2 and {r["data"]["target"] for r in memories} == {"agent1", "agent2"}
            assert max(r["seq"] for r in memories) < min(r["seq"] for r in calls)
            assert not any(r["event"] == "plan_confirmation" for r in rows)
            assert result.metrics["planning"]["outcome"] == "injected"
            assert result.metrics["planning"]["steps"] == {"agent1": 0, "agent2": 0}
            module_spec = importlib.util.spec_from_file_location(
                "audit_fixture", Path(__file__).parents[1] / "scripts/audit_trajectories.py"
            )
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            checked = module.audit_coordination(rows, {"metrics": result.metrics})
            assert checked["initial_plan_bundle_sha256"] == plan.bundle_sha256
            module.audit_worker_summaries(rows, plan)
            corrupted = copy.deepcopy(rows)
            next(r for r in corrupted if r["event"] == "worker_summary_delivery")["data"]["content"] = "wrong memory"
            with pytest.raises(ValueError, match="Worker summary differs"):
                module.audit_worker_summaries(corrupted, plan)
        else:
            assert coordinator_calls == []
            assert "planning" not in result.metrics
            assert not any(r["event"] == "plan_delivery" for r in rows)
    finally:
        source.cleanup()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_native_submit_archives_exact_plan_and_pins_digest(tmp_path, monkeypatch, bundle, newline):
    path = tmp_path / "fixture_task-1-f1_f2.json"
    path.write_bytes(json.dumps(bundle, indent=2).replace("\n", newline).encode())
    plan = InitialPlan.load(path, expected_pair=bundle["pair"])
    source = Path(__file__).parents[1] / "scripts/nlp_cluster/submit.py"
    module_spec = importlib.util.spec_from_file_location("submit_initial_fixture", source)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(source),
            "--mode",
            "real",
            "--env-file",
            "/private/profile",
            "--coordination-mode",
            "group-chat",
            "--initial-plan-dir",
            str(tmp_path),
            "--pairs",
            bundle["pair"],
        ],
    )
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    uploaded, commands = [], []

    def output(argv, **kwargs):
        if argv[:2] == ["git", "rev-parse"]:
            return "b" * 40
        if argv[:2] == ["git", "status"]:
            return b""
        if argv[:2] == ["git", "archive"]:
            return archive.getvalue()
        commands.append(argv[-1])
        return "12345\n"

    def run(argv, **kwargs):
        commands.append(argv[-1])
        if "tar -xf -" in argv[-1]:
            uploaded.append(kwargs["input"])

    monkeypatch.setattr(module.subprocess, "check_output", output)
    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    with tarfile.open(fileobj=io.BytesIO(uploaded[0])) as t:
        meta = json.loads(t.extractfile("_run/metadata.json").read())
        assert meta["initial_plans"] == {bundle["pair"]: plan.bundle_sha256}
        assert (
            t.extractfile("_run/data/initial-plans/fixture_task-1-f1_f2.json").read()
            == (tmp_path / "fixture_task-1-f1_f2.json").read_bytes()
        )
    assert any("COOPER_INITIAL_PLAN_DIR=" in c and "/data/initial-plans" in c for c in commands)


@pytest.mark.parametrize("invalid_base", ["wrong_commit", "dirty_tree"])
def test_injection_rejects_wrong_or_modified_base_before_history(tmp_path, bundle, invalid_base):
    source = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        source.write_file("base.py", "value = 1\n")
        assert source.execute("git add base.py && git commit -qm base").exit_code == 0
        base = source.execute("git rev-parse HEAD").stdout.strip()
        bundle["source_base_commit"] = base if invalid_base == "dirty_tree" else "a" * 40
        if invalid_base == "dirty_tree":
            source.write_file("base.py", "value = 2\n")
        plan = save_plan(tmp_path, bundle)
        c = _Coordinator(
            {"agent1": source, "agent2": source},
            initial_plan=plan,
            assignments=[Assignment(agent_id=f"agent{i}", feature_id=i, task=f"feature{i}") for i in (1, 2)],
            bus=InMemoryBus("base-check"),
            notebook_path=tmp_path / "notebook.md",
            coordination_mode="group-chat",
            repo="fixture_task",
            task_id=1,
            complete=lambda _: pytest.fail("must fail before model calls"),
        )
        with pytest.raises(ValueError, match="base source mismatch|unchanged base"):
            c.inject_initial_plan()
        assert c._chat == [] and c._version == 0 and c._final_plan is None
    finally:
        source.cleanup()


def test_provenance_stays_in_audit_not_coordinator_or_restored_worker_inputs(tmp_path, bundle):
    from types import SimpleNamespace

    source = LocalEnv.fresh(workdir=str(tmp_path))
    trace = Trajectory(tmp_path / "provenance.jsonl.gz")
    try:
        bundle["source_base_commit"] = source._base_commit
        plan = save_plan(tmp_path, bundle)
        prompts, messages = [], []
        c = _Coordinator(
            {"agent1": source, "agent2": source},
            initial_plan=plan,
            assignments=[Assignment(agent_id=f"agent{i}", feature_id=i, task=f"feature{i}") for i in (1, 2)],
            bus=InMemoryBus("provenance"), notebook_path=tmp_path / "notebook.md",
            coordination_mode="group-chat", repo="fixture_task", task_id=1,
            complete=lambda prompt: prompts.append(prompt) or [],
            trace=lambda event, **data: trace.emit("coordinator", event, **data),
        )
        c.inject_initial_plan()
        c.decide()
        observation = json.loads(prompts[0].split("OBSERVATION:\n", 1)[1])
        assert set(observation["planning"]) == {"phase", "final_plan"}
        assert set(observation["planning"]["final_plan"]) == {"version", "content", "sha256"}
        assert all(set(row) == {"message_id", "sender", "addressed_to", "content"}
                   for row in observation["group_chat"]["unread"])
        fake = SimpleNamespace(
            _compaction_count=1, add_messages=lambda *items: messages.extend(items),
            model=SimpleNamespace(format_message=lambda **kwargs: kwargs),
        )
        c.drain("agent1")
        with c._condition:
            c._restore_plan_locked("agent1", fake)
        c.restore_context("agent1", fake)
        fake._compaction_count += 1
        with c._condition:
            c._restore_plan_locked("agent1", fake)
        assert [m["content"] for m in messages if m["role"] == "assistant"] == [bundle["worker_summaries"]["agent1"]]
        c._requests["agent1"] = {"phase_text": c._implementation_text()}
        c.restore_context("agent1", fake, request=True)
        model_text = json.dumps([prompts, messages, (tmp_path / "notebook.md").read_text()])
        assert "synthetic" not in model_text.lower() and "injected" not in model_text.lower()
        assert all(message.content in model_text for message in plan.history)
        assert plan.content in model_text
        assert c.planning_metrics()["planning"]["outcome"] == "injected"
        assert c.planning_metrics()["planning"]["final_plan"]["confirmations"] == {}
        trace.close()
        rows = [json.loads(line) for line in open_events(tmp_path / "provenance.jsonl.gz")]
        assert all(r["data"]["synthetic"] for r in rows if r["event"] == "group_message")
        assert any(r["event"] == "initial_plan_injected" and r["data"]["synthetic"] for r in rows)
        assert not any(r["event"] == "plan_confirmation" for r in rows)
    finally:
        trace.close()
        source.cleanup()


def test_legacy_bundle_without_worker_memory_still_loads(tmp_path, bundle):
    del bundle["worker_summaries"]
    assert save_plan(tmp_path, bundle).worker_summaries == ()
