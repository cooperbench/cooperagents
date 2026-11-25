"""Offline agent-mode wiring through real mini-SWE workers and Git checkouts."""

import json
import shlex
import socket
import sys
import threading
from dataclasses import asdict, replace

import pytest

from cooperagents.agent_coordination_types import (
    AGENT_COORDINATOR_METRICS_SCHEMA,
    AgentCoordinatorConfig,
    DecisionResult,
    GateConfig,
    OpeningPlan,
    WorkerPlan,
)
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.trajectory import Trajectory, open_events
from cooperagents.types import Assignment, TeamSpec
from test_bench_budget import compare as compare

WORKERS = ("agent1", "agent2")
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
ADVICE = "Use the existing interface."
AGENT_FLAGS = ["--team-only", "--coop-tools", "--no-seed", "--git-share", "--agent-coordinator"]
SYSTEM2_OVERRIDES = [
    ("--agent-coordinator-reasoning-effort", "inherit", "reasoning_effort", None, ""),
    ("--agent-coordinator-max-retries", "0", "max_retries", 0, "-1"),
    ("--agent-coordinator-retry-delay", "0.2", "retry_base_delay_s", 0.2, "nan"),
    ("--agent-opening-max-tool-calls", "8", "opening_max_tool_calls", 8, "0"),
    ("--agent-opening-time-limit", "45", "opening_time_limit_s", 45.0, "0"),
    ("--agent-opening-reserve", "1", "opening_reserve", 1, "-1"),
    ("--agent-wake-max-tool-calls", "5", "wake_max_tool_calls", 5, "0"),
    ("--agent-wake-time-limit", "30", "wake_time_limit_s", 30.0, "inf"),
    ("--agent-wake-reserve", "0", "wake_reserve", 0, "-1"),
    ("--agent-spec-check-time-limit", "70", "spec_check_time_limit_s", 70.0, "0"),
]


def team_spec(**changes):
    spec = TeamSpec(
        "offline", "fixture", 1, [1, 2],
        assignments=[Assignment(aid, f"Implement feature {i} for {aid}.", feature_id=i) for i, aid in enumerate(WORKERS, 1)],
        shared_workspace=True, worker="mini_swe", model="dummy", coop_tools=True, seed_prior=False,
        git_share=True, agent_coordinator=True,
    )
    return replace(spec, **changes)


def tool(name, **args):
    return {"name": name, "arguments": json.dumps(args)}


@pytest.mark.parametrize("policy", ["model", "rule", "random"])
def test_offline_agent_coordinator(tmp_path, monkeypatch, policy):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-gate-key")
    unexpected_calls = []

    def unexpected(*args, **kwargs):
        unexpected_calls.append("unscripted completion or network access")
        raise AssertionError(unexpected_calls[-1])

    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected)
    import litellm
    import openai

    monkeypatch.setattr(openai, "OpenAI", unexpected)
    seed = LocalEnv.fresh(workdir=str(tmp_path))
    seed.write_file("a.txt", "base\n")
    seed.write_file("b.txt", "base\n")
    seed.write_file(".cb_checks/fail.sh", "exit 1\n")
    assert seed.execute("git add . && git commit -qm fixture").exit_code == 0

    shared_git = tmp_path / "shared.git"
    created, cleaned = {}, {}

    class MountedLocalEnv(LocalEnv):
        def execute(self, command, *, timeout=60):
            return super().execute(command.replace("/cbshared/repo.git", shlex.quote(str(shared_git))), timeout=timeout)

        def cleanup(self):
            cleaned[self.actor] = self.execute("git status --porcelain").stdout
            super().cleanup()

    def factory(aid):
        env = MountedLocalEnv.clone(seed.repo_path, workdir=str(tmp_path))
        env.actor = aid
        created[aid] = env
        return env

    wake_completed = threading.Event()

    class ObservedTrajectory(Trajectory):
        def emit(self, actor, event, **data):
            seq = super().emit(actor, event, **data)
            if event == "wake_end":
                wake_completed.set()
            return seq

    plan = OpeningPlan(1, {aid: WorkerPlan([], [], [], "") for aid in WORKERS}, [], [], [])

    def coordinator(messages, tools):
        names = {row["function"]["name"] for row in tools}
        if "submit_plan" in names:
            assert set(created) == {*WORKERS, "coordinator"}
            assert shared_git.is_dir()
            return [tool("read_file", path="a.txt", start=1, end=1), tool("submit_plan", **asdict(plan))]
        if "end_wake" in names:
            observation = json.loads(messages[-1]["content"])
            assert observation["git_evidence"]["base_sha"]
            return [tool("send_message", recipient="agent1", content=ADVICE), tool("end_wake", note="done")]
        if names == {"spec_verdict"}:
            observation = json.loads(messages[-1]["content"])
            feature = observation["feature"]
            assert "improved" in observation["frozen_diff"]
            if feature["feature_id"] == "1":
                return [tool("spec_verdict", verdict="REJECT", findings=[
                    dict(kind="missing_requirement", feature_id="1", quote=feature["task"], path="a.txt")
                ])]
            return [tool("spec_verdict", verdict="ACCEPT", findings=[])]
        return unexpected()

    def gate_complete(request):
        assert request.questions[0].id == "decision"
        return DecisionResult({"decision": {"WAKE": 0.9, "SKIP": 0.1}}, {"decision": "WAKE"}, 1, 12, 0, 0.001, None, None, None)

    failing_steps = 1 if policy == "random" else 5
    commands = {
        "agent1": iter(
            ["sh .cb_checks/fail.sh"] * failing_steps + ["true", "echo improved > a.txt", SUBMIT, "echo fixed >> a.txt", SUBMIT],
        ),
        "agent2": iter(["echo improved > b.txt", SUBMIT]),
    }
    turns = dict.fromkeys(WORKERS, 0)

    def worker(messages, **request):
        context = "\n".join(message.get("content") or "" for message in messages if message["role"] == "user")
        (aid,) = [aid for aid in WORKERS if f"for {aid}." in context]
        turn = turns[aid]
        turns[aid] += 1
        if turn == 0:
            assert "[Message from coordinator]: Proposed opening plan" in context
            assert {row["function"]["name"] for row in request["tools"]} == {"bash", "send_message"}
        if (aid == "agent1" and turn == failing_steps) or (aid == "agent2" and turn == 0):
            assert wake_completed.wait(10), "coordinator wake did not finish"
        if aid == "agent1" and turn == failing_steps + 1:
            assert f"[Message from coordinator]: {ADVICE}" in context
        if aid == "agent1" and turn == failing_steps + 3:
            assert any("SPEC_CHECK_REJECTED" in (m.get("content") or "") for m in messages)
        command = next(commands[aid])
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"{aid}-{turn}", "type": "function", "function": tool("bash", command=command),
            }]}}],
            usage={"prompt_tokens": 10, "completion_tokens": 1},
        )

    monkeypatch.setattr(litellm, "completion", worker)
    gate = GateConfig(policy=policy, wake_budget=1, **({"random_wakes": 1, "random_step_horizon": 1} if policy == "random" else {}))
    trajectory = ObservedTrajectory(tmp_path / "trajectory.jsonl.gz")
    gate_attempts = []

    def base_gate(env):
        gate_attempts.append(env.actor)
        return None

    try:
        result = UnifiedHarness(
            trajectory=trajectory, step_limit=20,
            agent_coordinator_config=AgentCoordinatorConfig(gate=gate, tick_s=0.01),
            agent_coordinator_complete=coordinator,
            agent_gate_complete=gate_complete if policy == "model" else None,
        ).run(team_spec(completion_gate=base_gate), env_factory=factory)
    finally:
        trajectory.close()
        seed.cleanup()

    assert unexpected_calls == []
    assert cleaned.keys() == created.keys() == {*WORKERS, "coordinator", "merge"}
    assert not cleaned["coordinator"]
    assert {aid: row.status for aid, row in result.seeds.items()} == dict.fromkeys(WORKERS, "submitted")
    assert result.integrated.status == "submitted"
    assert "+fixed" in result.integrated.patch and "+++ b/b.txt" in result.integrated.patch
    assert sorted(gate_attempts) == ["agent1", "agent1", "agent2"]
    with open_events(trajectory.path) as journal:
        rows = list(map(json.loads, journal))

    opening = next(row for row in rows if row["event"] == "opening_plan")
    first_worker = next(row for row in rows if row["event"] == "request" and row["actor"] in WORKERS)
    assert opening["seq"] < first_worker["seq"]
    decisions = [row["data"] for row in rows if row["event"] == "gate_decision"]
    (wake,) = [decision for decision in decisions if decision["decision"] == "WAKE"]
    assert wake["source"] == policy
    assert wake["reason_code"] == ("RANDOM_SCHEDULE" if policy == "random" else "NO_PROGRESS")
    if policy != "random":
        assert wake["state"]["signals"][0]["kind"] == "NO_PROGRESS_FAILING"
    metrics = result.metrics["agent_coordinator"]
    assert metrics.keys() == {"schema", "config", "opening", "signals", "gate", "wakes", "spec_check", "probe", "usage",
                              "team_worker_steps", "timeline"}
    assert metrics["schema"] == AGENT_COORDINATOR_METRICS_SCHEMA
    assert metrics["config"]["gate"]["policy"] == policy
    assert metrics["opening"]["outcome"] == "delivered"
    assert metrics["wakes"]["total"] == metrics["gate"]["wake"] == 1
    assert metrics["gate"]["skip"] == len(decisions) - 1
    assert metrics["spec_check"] == dict(checked=2, accepted=1, rejected=1, errors=0)
    assert metrics["probe"]["errors"] == 0
    assert metrics["timeline"]["worker_end_s"].keys() == set(WORKERS)
    assert metrics["usage"]["coordinator"]["calls"] == 4
    gate_requests = [row for row in rows if row["event"] == "request" and row["actor"] == "coordinator_gate"]
    assert len(gate_requests) == metrics["usage"]["coordinator_gate"]["calls"] == int(policy == "model")
    if gate_requests:
        assert gate_requests[0]["data"]["request"]["tool_calls"] == []
        assert metrics["usage"]["coordinator_gate"]["prompt_tokens"] == 12
    if policy == "random":
        assert metrics["gate"]["random_planned"] == 1 and metrics["gate"]["random_unused"] == 0
    assert "offline-gate-key" not in json.dumps(rows)


@pytest.mark.parametrize("changes", [
    {"shared_workspace": False}, {"coop_tools": False}, {"worker": "builtin"}, {"seed_prior": True}, {"git_share": False},
    {"team_roles": True}, {"adaptive": True}, {"decompose": True}, {"best_of_n": 2}, {"contract_first": True},
    {"allow_spawn_tool": True}, {"task_board": True}, {"claim_mode": True}, {"wait_protocol": True},
    {"assignments": [Assignment("single", "feature")]},
    {"assignments": [Assignment("same", "feature"), Assignment("same", "feature")]},
    {"assignments": [Assignment("", "feature"), Assignment("valid", "feature")]},
    {"assignments": [Assignment("coordinator", "feature"), Assignment("valid", "feature")]},
])
def test_invalid_team_before_env(changes):
    with pytest.raises(ValueError, match="Agent coordinator"):
        UnifiedHarness().run(team_spec(**changes), env_factory=lambda _: pytest.fail("created an environment"))


@pytest.mark.parametrize(("gate", "error"), [
    (GateConfig(model=""), "invalid model"),
    (GateConfig(base_url="http://example.com"), "invalid gate base URL"),
    (GateConfig(), "gate API key environment variable is empty"),
    (GateConfig(policy="random"), "random gate requires a matched agent_main result"),
    (GateConfig(policy="random", random_wakes=2, random_step_horizon=1), "matched step horizon"),
])
def test_invalid_gate_before_env(monkeypatch, gate, error):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    harness = UnifiedHarness(agent_coordinator_config=AgentCoordinatorConfig(gate=gate), agent_gate_complete=lambda _: None)
    with pytest.raises(ValueError, match=error):
        harness.run(team_spec(), env_factory=lambda _: pytest.fail("created an environment"))


@pytest.mark.parametrize("kwargs", [
    {"coordinator_complete": lambda _: []},
    {"coordinator_completion": object(), "coordination_mode": "group-chat-planning"},
    {"worker_completion": object(), "coordination_mode": "group-chat-planning"},
    {"planning_only": True, "coordination_mode": "group-chat-planning"},
])
def test_agent_mode_rejects_legacy_callbacks(kwargs):
    with pytest.raises(ValueError, match="requires its own completion callbacks"):
        UnifiedHarness(**kwargs).run(team_spec(), env_factory=lambda _: pytest.fail("created an environment"))


@pytest.mark.parametrize("kwargs", [
    {"agent_coordinator_config": AgentCoordinatorConfig()},
    {"agent_coordinator_complete": lambda *_: []},
    {"agent_gate_complete": lambda _: None},
])
def test_agent_settings_require_agent_mode(kwargs):
    with pytest.raises(ValueError, match="settings require agent_coordinator"):
        UnifiedHarness(**kwargs).run(team_spec(agent_coordinator=False), env_factory=lambda _: pytest.fail("created an environment"))


@pytest.mark.parametrize("name", ["agent_coordinator_complete", "agent_gate_complete"])
def test_injected_completion_must_be_callable(name):
    with pytest.raises(TypeError, match=f"{name} must be callable"):
        UnifiedHarness(**{name: False})


@pytest.mark.parametrize(("flags", "key", "error"), [
    (["--agent-gate", "model"], None, "gate API key environment variable is empty"),
    (["--agent-gate-base-url", "http://example.com"], "offline", "invalid gate base URL"),
    (["--agent-gate", "rule", "--agent-gate-model", "unused"], None, "decision model fields require model policy"),
    (["--agent-gate", "random", "--agent-gate-model", "unused"], None, "decision model fields require model policy"),
    (["--agent-gate-threshold", "1.5"], "offline", "invalid wake threshold"),
    (["--agent-gate", "random"], None, "random gate requires a matched agent_main result"),
    (["--agent-gate", "random", "--agent-random-match", "missing-match"], None, "random gate requires a matched agent_main result"),
])
def test_cli_gate_validation(compare, monkeypatch, tmp_path, capsys, flags, key, error):
    monkeypatch.setattr(compare, "_load_env", lambda: None)
    monkeypatch.setattr(compare, "run_team", lambda *_args, **_kwargs: pytest.fail("started a team"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", "--pairs", "fixture:1:2,1", "--log-dir", str(tmp_path), *AGENT_FLAGS, *flags])
    with pytest.raises(SystemExit) as exc:
        compare.main()
    assert exc.value.code == 2
    assert error in capsys.readouterr().err


@pytest.mark.parametrize(("flags", "error"), [
    (["--agent-gate", "rule"], "agent coordinator flags require --agent-coordinator"),
    (["--no-agent-spec-check"], "agent coordinator flags require --agent-coordinator"),
    (["--agent-coordinator", "--coop-tools", "--no-seed"], "requires a team with --coop-tools, --no-seed, and --git-share"),
    ([*AGENT_FLAGS, "--task-board"], "--agent-coordinator excludes"),
    (["--agent-coordinator-reasoning-effort", "medium"], "agent coordinator flags require --agent-coordinator"),
    *[([flag, value], "agent coordinator flags require --agent-coordinator") for flag, value, *_ in SYSTEM2_OVERRIDES],
    *[([*AGENT_FLAGS, "--agent-gate", "rule", flag, bad], name) for flag, _, name, _, bad in SYSTEM2_OVERRIDES],
])
def test_cli_family_validation(compare, monkeypatch, capsys, flags, error):
    monkeypatch.setattr(compare, "_load_env", lambda: None)
    monkeypatch.setattr(compare, "load_subset", lambda _: pytest.fail("looked up benchmark pairs"))
    monkeypatch.setattr(sys, "argv", ["bench_compare.py", *flags])
    with pytest.raises(SystemExit) as exc:
        compare.main()
    assert exc.value.code == 2
    assert error in capsys.readouterr().err


@pytest.mark.parametrize("use_overrides", [False, True])
def test_cli_system2_settings(compare, monkeypatch, tmp_path, use_overrides):
    configs = []

    def run_team(item, **options):
        configs.append(options["agent_coordinator_config"])
        return {"duration": 0.0}

    monkeypatch.setattr(compare, "_load_env", lambda: None)
    monkeypatch.setattr(compare, "run_team", run_team)
    flags = [token for flag, value, *_ in SYSTEM2_OVERRIDES for token in (flag, value)] if use_overrides else []
    monkeypatch.setattr(sys, "argv", [
        "bench_compare.py", "--pairs", "fixture:1:2,1", "--log-dir", str(tmp_path), *AGENT_FLAGS,
        "--agent-gate", "rule", "--skip-eval", *flags,
    ])
    compare.main()
    defaults = AgentCoordinatorConfig(gate=GateConfig(policy="rule"))
    overrides = {name: value for _, _, name, value, _ in SYSTEM2_OVERRIDES} if use_overrides else {}
    assert configs == [replace(defaults, **overrides)]


def test_cli_resolves_random_matches_per_pair(compare, monkeypatch, tmp_path):
    configs = {}

    def run_team(item, **options):
        assert options["agent_coordinator"]
        configs[item.task_id] = options["agent_coordinator_config"].gate
        return {"duration": 0.0}

    for task, wakes, horizon in [(1, 2, 10), (2, 3, 11)]:
        result = tmp_path / f"matched/team/fixture/{task}/f1_f2/result.json"
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({"metrics": {"agent_coordinator": {"gate": {"wake": wakes}, "team_worker_steps": horizon}}}))
    monkeypatch.setattr(compare, "_load_env", lambda: None)
    monkeypatch.setattr(compare, "run_team", run_team)
    monkeypatch.setattr(sys, "argv", [
        "bench_compare.py", "--pairs", "fixture:1:2,1", "fixture:2:1,2", "--log-dir", str(tmp_path), *AGENT_FLAGS,
        "--agent-gate", "random", "--agent-random-match", "matched", "--agent-random-seed", "21", "--skip-eval",
    ])
    compare.main()
    assert configs == {
        1: GateConfig(policy="random", random_wakes=2, random_step_horizon=10, random_seed=21),
        2: GateConfig(policy="random", random_wakes=3, random_step_horizon=11, random_seed=21),
    }


@pytest.mark.parametrize("use_overrides", [False, True])
def test_runner_records_agent_settings_and_mounts_shared_git(compare, monkeypatch, tmp_path, use_overrides):
    from cooperagents.types import RunResult

    defaults = AgentCoordinatorConfig(gate=GateConfig(policy="rule", wake_budget=3), opening="none", spec_check=False)
    overrides = {name: value for _, _, name, value, _ in SYSTEM2_OVERRIDES} if use_overrides else {}
    config = replace(defaults, **overrides)
    mounts = []

    def run(harness, spec, *, env_factory, **kwargs):
        assert spec.agent_coordinator
        assert harness.agent_coordinator_config == config
        for aid in ["agent1", "agent2", "coordinator"]:
            env_factory(aid)
        return RunResult(spec.run_id, spec.repo, spec.task_id, spec.features)

    monkeypatch.setenv("COOPER_RUNTIME", "docker")
    monkeypatch.setattr(compare, "task_environment", lambda image, **options: mounts.append(options))
    monkeypatch.setattr(compare, "read_feature", lambda *args: "feature")
    monkeypatch.setattr(compare.UnifiedHarness, "run", run)
    compare.run_team(
        compare.WorkItem("fixture", 1, [2, 1]), run_name="agent_main", logs_dir=tmp_path, step_limit=10, max_agents=2,
        seed_prior=False, git_share=True, coop_tools=True, agent_coordinator=True, agent_coordinator_config=config,
        record_trajectory=True,
    )
    assert len(mounts) == 3 and mounts[0] == mounts[1] == mounts[2]
    assert mounts[0]["coordinator_dir"] is None
    assert mounts[0]["volumes"][0].endswith(":/cbshared")

    with open_events(next(tmp_path.rglob("trajectory.jsonl.gz"))) as journal:
        start = json.loads(next(journal))["data"]
    assert start["agent_coordinator"] is True
    assert start["agent_gate"] == "rule"
    assert start["agent_gate_model"] is None and start["agent_gate_base_url"] is None
    assert start["agent_wake_budget"] == 3 and start["agent_opening"] == "none" and start["agent_spec_check"] is False
    for _, _, name, _, _ in SYSTEM2_OVERRIDES:
        assert start[f"agent_{name}"] == getattr(config, name)
    assert start["agent_reasoning_effort"] == (None if use_overrides else "medium")
