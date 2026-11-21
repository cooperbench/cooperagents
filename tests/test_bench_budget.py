import importlib.util
import json
import os
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def compare():
    spec = importlib.util.spec_from_file_location("bench_compare", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    return bench


def test_deterministic_coordinator_cli_excludes_tool_call_coordinator(compare, monkeypatch, capsys):
    monkeypatch.setattr(compare, "_load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", "--coordinator", "--deterministic-coordinator"])
    with pytest.raises(SystemExit) as exc:
        compare.main()
    assert exc.value.code == 2
    assert "argument --deterministic-coordinator: not allowed with argument --coordinator" in capsys.readouterr().err


def test_planning_cli_bindings_use_verified_wire_transport(compare, monkeypatch, tmp_path):
    import httpx

    from cooperagents.completion import CompletionBinding
    from cooperagents.trajectory import open_events
    from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import BASH_TOOL

    for key in list(os.environ):
        if key.startswith("COOPER_") or key.startswith("AZURE_OPENAI_"):
            monkeypatch.delenv(key)
    for key, value in {
        "OPENAI_BASE_URL": "https://tinker.invalid/v1",
        "OPENAI_API_KEY": "fixture-key",
        "COOPER_MAX_COMPLETION_TOKENS": "2000",
        "COOPER_WORKER_MAX_COMPLETION_TOKENS": "4096",
        "COOPER_TEMPERATURE_FORCE": "1",
        "COOPER_TOP_P": "0.9",
        "COOPER_REASONING_EFFORT": "high",
        "COOPER_CHAT_TEMPLATE_ENABLE_THINKING": "false",
        "COOPER_WORKER_CHAT_TEMPLATE_ENABLE_THINKING": "true",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(compare, "MODEL", "tinker-model")
    monkeypatch.setattr(compare, "read_feature", lambda *a: "feature")
    monkeypatch.setattr(compare, "write_run_outputs", lambda *a, **k: None)
    received = []

    def respond(request):
        assert str(request.url) == "https://tinker.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-key"
        received.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "tinker-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
        })

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx.Client, "_transport_for_url", lambda self, url: transport)

    def run(self, team, **kwargs):
        assert self.coordination_mode == "group-chat-planning"
        for actor, binding, tools in (
            ("coordinator", self.coordinator_completion, [BASH_TOOL]),
            ("agent1", self.worker_completion, [BASH_TOOL]),
            ("agent1", self.worker_completion, None),
        ):
            assert isinstance(binding, CompletionBinding)
            response = binding.complete(
                messages=[{"role": "user", "content": "work"}], tools=tools, actor_id=actor, call_id=1,
                trace=partial(self.trajectory.emit, actor),
            )
            assert response.choices[0].message.content == "done"
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={}, seeds={})

    monkeypatch.setattr(compare.UnifiedHarness, "run", run)
    compare.run_team(
        compare.WorkItem("go_chi_task", 26, [1, 2]), run_name="planning", logs_dir=tmp_path, step_limit=10, max_agents=2,
        coordinator=True, coop_tools=True, seed_prior=False, coordination_mode="group-chat-planning", record_trajectory=True,
    )

    assert [body["max_completion_tokens"] for body in received] == [2000, 4096, 4096]
    assert [body["chat_template_kwargs"]["enable_thinking"] for body in received] == [False, True, True]
    assert all(body["model"] == "tinker-model" and body["temperature"] == 1 and body["top_p"] == 0.9 for body in received)
    assert all(body["reasoning_effort"] == "high" and "max_tokens" not in body for body in received)
    assert [body.get("tool_choice") for body in received] == ["auto", "auto", None]
    assert [body.get("tools") for body in received] == [[BASH_TOOL], [BASH_TOOL], None]
    with open_events(next(tmp_path.rglob("trajectory.jsonl.gz"))) as journal:
        rows = list(map(json.loads, journal))
    wire = [row for row in rows if row["event"] == "wire_request"]
    assert [row["actor"] for row in wire] == ["coordinator", "agent1", "agent1"]
    assert [row["data"]["request"] for row in wire] == received
    assert "fixture-key" not in json.dumps(rows)


@pytest.mark.parametrize("send_tool_choice", [None, "false"])
def test_planning_tool_choice_env_controls_coordinator_and_worker(compare, monkeypatch, send_tool_choice):
    import httpx

    from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import BASH_TOOL

    for key in list(os.environ):
        if key.startswith("COOPER_") or key.startswith("AZURE_OPENAI_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://tinker.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    if send_tool_choice is not None:
        monkeypatch.setenv("COOPER_SEND_TOOL_CHOICE", send_tool_choice)
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "tinker-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
        })

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx.Client, "_transport_for_url", lambda self, url: transport)
    for worker in (False, True):
        compare._planning_completion("tinker-model", worker=worker).complete(
            messages=[{"role": "user", "content": "work"}], tools=[BASH_TOOL],
            actor_id="worker" if worker else "coordinator", call_id=1,
        )

    assert len(received) == 2
    assert all(body["tools"] == [BASH_TOOL] for body in received)
    if send_tool_choice is None:
        assert all(body["tool_choice"] == "auto" for body in received)
    else:
        assert all("tool_choice" not in body for body in received)


def test_budget_reaches_solo_and_team(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("bench_compare", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)
    captured = []

    def run(self, team, **kwargs):
        captured.append((self.step_limit, team.agent_time_limit))
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={})

    monkeypatch.setattr(bench.UnifiedHarness, "run", run)
    monkeypatch.setattr(bench, "read_feature", lambda *a: "feature")
    monkeypatch.setattr(bench, "write_run_outputs", lambda *a, **k: None)
    item = bench.WorkItem("go_chi_task", 26, [1, 2])
    kwargs = dict(run_name="budget", logs_dir=tmp_path, step_limit=1000, agent_time_limit=3600)
    bench.run_solo(item, **kwargs)
    bench.run_team(item, max_agents=2, **kwargs)
    assert captured == [(1000, 3600), (1000, 3600)]


def test_runners_isolate_notebooks_and_mount_workers_only(monkeypatch, tmp_path):
    from cooperagents.harness import _Coordinator

    paths, mounts = [], []

    def run(self, team, *, env_factory, **kwargs):
        path = self.coordinator_notebook_path
        paths.append(path)
        coordinator = _Coordinator({}, assignments=team.assignments, bus=self.bus, notebook_path=path, complete=lambda _: [])
        for worker in [a.agent_id for a in team.assignments] + ["merge"]:
            env_factory(worker)
        assert bool(path) == team.coordinator_notebook
        if path is not None:
            assert path.is_file()
            assert len(coordinator._queues) == len(team.assignments)
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={}, integrated=SimpleNamespace(patch=""))

    for script in ("bench_compare", "bench_programbench"):
        module_spec = importlib.util.spec_from_file_location(script, Path(__file__).parents[1] / f"scripts/{script}.py")
        bench = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(bench)
        monkeypatch.setattr(bench.UnifiedHarness, "run", run)
        if script == "bench_compare":
            monkeypatch.setattr(bench, "read_feature", lambda *a: "feature")
            monkeypatch.setattr(bench, "write_run_outputs", lambda *a, **k: None)
            monkeypatch.setattr(bench, "task_environment", lambda *a, **kw: mounts.append(kw["coordinator_dir"]))

            def invoke(enabled, bench=bench):
                return bench.run_team(
                    bench.WorkItem("go_chi_task", 26, [1, 2]),
                    run_name="mount",
                    logs_dir=tmp_path,
                    step_limit=10,
                    max_agents=2,
                    coordinator=True,
                    coordinator_notebook=enabled,
                    coop_tools=True,
                    seed_prior=False,
                )

            size = 2
        else:
            monkeypatch.setattr(bench, "DockerEnv", lambda *a, **kw: mounts.append(kw))
            monkeypatch.setattr(
                bench,
                "ADAPTER",
                SimpleNamespace(
                    name="test",
                    build_artifact="artifact",
                    reference_binary="reference",
                    image=lambda _: "image",
                    task_for=lambda *a: "feature",
                    setup_env=lambda *a: None,
                    env_kwargs=lambda: {"user": "1000", "network": "none"},
                ),
            )

            def invoke(enabled, bench=bench):
                return bench.run_team_once(
                    "coopgitc2",
                    "test",
                    step_limit=10,
                    agent_time_limit=30,
                    team_size=3,
                    coordinator_notebook=enabled,
                    artifact_dir=tmp_path,
                )

            before = set(tmp_path.rglob("*"))
            dry, _ = bench.run_team_once(
                "coopgitc2",
                "test",
                step_limit=10,
                agent_time_limit=30,
                team_size=3,
                coordinator_notebook=False,
                artifact_dir=tmp_path,
                dry_run=True,
            )
            assert dry["agents"] == ["agent1", "agent2", "agent3"] and not dry["spec"]["coordinator_notebook"]
            assert set(tmp_path.rglob("*")) == before
            size = 3
        for enabled in (True, True, False):
            mounts.clear()
            invoke(enabled)
            path = paths[-1]
            if script == "bench_compare":
                assert mounts == [path.parent if path else None] * size + [None]
            else:
                for index, options in enumerate(mounts):
                    assert options["user"] == "1000" and options["network"] == "none"
                    notes = [v for v in options["volumes"] if v.endswith(":/coordination:ro")]
                    assert notes == ([f"{path.parent}:/coordination:ro"] if path and index < size else [])
        assert paths[-3] != paths[-2] and paths[-1] is None


@pytest.mark.parametrize("only_first", [False, True])
@pytest.mark.parametrize("collect_conflicts", [False, True])
def test_benchmark_checkpoint_options_reach_valid_harness(monkeypatch, tmp_path, only_first, collect_conflicts):
    from cooperagents.repair import validate_capture_spec

    module_spec = importlib.util.spec_from_file_location("bench_capture", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bench)

    def run(self, team, **kwargs):
        validate_capture_spec(team, self.bus)
        assert self.stop_after_repair_checkpoint
        assert self.only_first_repair_checkpoint is only_first
        assert self.collect_source_conflicts is collect_conflicts
        assert team.repair_integrator
        return SimpleNamespace(duration_seconds=0, total_steps=0, helpers={}, seeds={})

    monkeypatch.setattr(bench.UnifiedHarness, "run", run)
    monkeypatch.setattr(bench, "read_feature", lambda *a: "feature")
    monkeypatch.setattr(bench, "write_run_outputs", lambda *a, **k: None)
    bench.run_team(
        bench.WorkItem("go_chi_task", 26, [1, 2]), run_name="capture", logs_dir=tmp_path,
        step_limit=10, max_agents=2, seed_prior=False, coop_tools=True, repair_integrator=True,
        completion_gate=True, checkpoint_repair=True, stop_after_repair_checkpoint=True,
        only_first_repair_checkpoint=only_first,
        collect_source_conflicts=collect_conflicts,
    )


@pytest.mark.parametrize("only_first", [False, True])
@pytest.mark.parametrize("collect_conflicts", [False, True])
def test_stop_cli_skips_official_evaluation(monkeypatch, tmp_path, only_first, collect_conflicts):
    import sys

    module_spec = importlib.util.spec_from_file_location("bench_stop", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bench)
    calls = []
    monkeypatch.setattr(bench, "_load_env", lambda: None)
    monkeypatch.setattr(bench, "run_eval", lambda *a, **k: pytest.fail("capture mode must not score"))
    monkeypatch.setattr(bench, "run_team", lambda *a, **k: calls.append(k) or {"duration": 0, "steps": 0})
    monkeypatch.setattr(sys, "argv", [
        "bench_compare.py", "--pairs", "go_chi_task:26:1,2", "--team-only", "--checkpoint-repair",
        "--repair-integrator", "--stop-after-repair-checkpoint", "--log-dir", str(tmp_path),
        *(["--only-first-repair-checkpoint"] if only_first else []),
        *(["--collect-source-conflicts"] if collect_conflicts else []),
    ])
    bench.main()
    assert len(calls) == 1 and calls[0]["stop_after_repair_checkpoint"]
    assert calls[0]["only_first_repair_checkpoint"] is only_first
    assert calls[0]["collect_source_conflicts"] is collect_conflicts


def test_standard_completion_gate_partial_preserves_defaults():
    import inspect

    from cooperagents.verification import validate

    defaults = inspect.signature(validate).parameters
    assert defaults["merged"].default is False and defaults["build_artifact"].default is None


@pytest.mark.parametrize("missing", ["--checkpoint-repair", "--repair-integrator", "--team-only"])
def test_stop_cli_requires_capture_repair_and_team(monkeypatch, missing):
    import sys

    module_spec = importlib.util.spec_from_file_location("bench_invalid_stop", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bench)
    monkeypatch.setattr(bench, "_load_env", lambda: None)
    monkeypatch.setattr(bench, "run_team", lambda *a, **k: pytest.fail("invalid options reached task execution"))
    flags = ["--checkpoint-repair", "--repair-integrator", "--team-only"]
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", "--stop-after-repair-checkpoint", *[f for f in flags if f != missing]])
    with pytest.raises(SystemExit):
        bench.main()


@pytest.mark.parametrize("script", ["bench_compare.py", "nlp_cluster/submit.py"])
@pytest.mark.parametrize("flag", ["--only-first-repair-checkpoint", "--collect-source-conflicts"])
def test_collection_cli_requires_stop_mode(monkeypatch, capsys, script, flag):
    import sys

    spec = importlib.util.spec_from_file_location("only_first_invalid", Path(__file__).parents[1] / "scripts" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "_load_env"):
        monkeypatch.setattr(module, "_load_env", lambda: None)
    monkeypatch.setattr(sys, "argv", [script, flag])
    with pytest.raises(SystemExit):
        module.main()
    assert "requires --stop-after-repair-checkpoint" in capsys.readouterr().err


def test_merged_state_cli_and_harness_wiring(monkeypatch, tmp_path):
    import sys

    module_spec = importlib.util.spec_from_file_location("bench_merged", Path(__file__).parents[1] / "scripts/bench_compare.py")
    bench = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(bench)
    monkeypatch.setattr(bench, "_load_env", lambda: None)
    monkeypatch.setattr(bench, "run_eval", lambda *a, **k: pytest.fail("capture must not evaluate"))
    calls = []
    monkeypatch.setattr(bench, "run_team", lambda *a, **k: calls.append(k) or {"duration": 0, "steps": 0})
    flags = ["--pairs", "go_chi_task:26:1,2", "--team-only", "--no-seed", "--record-trajectory", "--capture-merged-state"]
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", *flags, "--log-dir", str(tmp_path)])
    bench.main()
    assert calls[0]["capture_merged_state"]
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", *[flag for flag in flags if flag != "--record-trajectory"]])
    with pytest.raises(SystemExit):
        bench.main()
    for forbidden in ("--repair-integrator", "--focused-repair", "--verify-fix", "--completeness-review", "--checkpoint-repair"):
        monkeypatch.setattr(sys, "argv", ["bench_compare.py", *flags, forbidden])
        with pytest.raises(SystemExit):
            bench.main()
