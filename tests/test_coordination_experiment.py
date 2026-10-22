"""Synthetic six-arm campaign coverage; no model or scheduler calls."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def campaign(tmp_path):
    module = script("summarize_coordination_experiment")
    pairs = ["synthetic:1:1,2", "synthetic:2:3,4"]
    pairs_file = tmp_path / "pairs.txt"
    pairs_file.write_text("\n".join(pairs) + "\n")
    frozen = dict(
        git_commit="code",
        cooperbench_commit="evaluator",
        step_limit=1000,
        agent_time_limit=3600,
        planning_step_limit=200,
        planning_time_limit=600,
        concurrency=10,
        eval_concurrency=1,
        runtime_config=dict(
            model="synthetic",
            model_revision="weights",
            service_revision="service",
            worker_request_parameters={"temperature": 1},
            coordinator_request_parameters={"temperature": 1},
        ),
        images_sha256={"image": "sha"},
        evaluator_patches="frozen patch hashes\n",
    )
    arms = [dict(name=arm, coordination_variant=variant, coordination_mode=mode) for arm, (variant, mode) in module.ARMS.items()]
    runs = []
    for arm, (variant, mode) in module.ARMS.items():
        for round_ in (1, 2, 3):
            run = tmp_path / f"{arm}-{round_}"
            for directory in ("rendered-config", "data"):
                (run / directory).mkdir(parents=True)
            metadata = {k: v for k, v in frozen.items() if k not in ("runtime_config", "images_sha256", "evaluator_patches")}
            metadata.update(
                round=round_,
                coordination_variant=variant,
                coordination_mode=mode,
                pairs=pairs,
                official_evaluation=True,
                record_trajectories=True,
                repair_integrator=False,
                coordinator=True,
                coordinator_notebook=True,
            )
            (run / "metadata.json").write_text(json.dumps(metadata))
            (run / "job-id.txt").write_text("synthetic-job")
            (run / "rendered-config/runtime-config.json").write_text(json.dumps(frozen["runtime_config"]))
            (run / "data/images.sha256.json").write_text(json.dumps(frozen["images_sha256"]))
            (run / "data/evaluator-patch.sha256").write_text(frozen["evaluator_patches"])
            (run / "trajectory-audit.json").write_text(json.dumps(dict(complete=True, pairs=2)))
            audit_entries = []
            for index, pair in enumerate(pairs):
                repo, task, features = pair.split(":")
                fids = sorted(map(int, features.split(",")))
                directory = run / "logs/real/team" / repo / task / "_".join(f"f{f}" for f in fids)
                directory.mkdir(parents=True)
                result = dict(
                    repo=repo,
                    task_id=int(task),
                    features=fids,
                    model="synthetic",
                    total_cost=1.5,
                    total_steps=12,
                    duration_seconds=7,
                    metrics=dict(coordination_mode=mode),
                )
                if mode == "group-chat-planning":
                    result["metrics"]["planning"] = dict(outcome=("error" if round_ == 3 else "agreed" if index == 0 else "forced"))
                passed = index == 0 or mode == "group-chat-planning"
                (directory / "result.json").write_text(json.dumps(result))
                (directory / "eval.json").write_text(
                    json.dumps(dict(both_passed=passed, feature1=dict(passed=passed), feature2=dict(passed=passed)))
                )
                raw = b"synthetic; receipt supplied"
                (directory / "trajectory.jsonl.gz").write_bytes(raw)
                audit_entries.append(
                    dict(
                        pair=pair,
                        sha256=hashlib.sha256(raw).hexdigest(),
                        coordination=dict(
                            coordinator_calls=2, coordinator_tokens={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
                        ),
                    )
                )
            (run / "trajectory-audit.json").write_text(json.dumps(dict(complete=True, pairs=2, entries=audit_entries)))
            runs.append(dict(arm=arm, round=round_, run_dir=str(run), job_id="synthetic-job"))
    path = tmp_path / "campaign.json"
    path.write_text(
        json.dumps(
            dict(
                pairs=pairs,
                pairs_file=str(pairs_file),
                pairs_sha256=hashlib.sha256(pairs_file.read_bytes()).hexdigest(),
                arms=arms,
                runs=runs,
                frozen=frozen,
            )
        )
    )
    return module, path


def test_full_summary_keeps_all_planning_outcomes(campaign):
    module, path = campaign
    summary = module.summarize_campaign(path)
    assert summary["complete"]
    arm = next(a for a in summary["arms"] if a["arm"] == "auto-prompt-group-chat-planning")
    assert arm["coverage"] == arm["expected"] == arm["pair_passes"] == 6
    assert arm["planning_outcomes"] == {"agreed": 2, "forced": 2, "error": 2}
    assert arm["coordinator_calls"] == 12 and arm["coordinator_tokens"]["prompt_tokens"] == 600
    assert arm["worker_cost_recorded"] == 9
    comparison = next(c for c in summary["paired_comparisons"] if c["left"] == "auto-prompt-group-chat")
    assert comparison["delta_percentage_points"] == 50 and comparison["counts"]["right_only"] == 3
    assert "Complete official results" in module.markdown(summary)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_pair",
        "reversed_duplicate_pair",
        "duplicate_run",
        "wrong_arm",
        "wrong_round",
        "mixed_code",
        "mixed_model",
        "non_boolean",
        "wrong_identity",
        "wrong_hash",
    ],
)
def test_corrupt_campaign_is_rejected(campaign, fault):
    module, path = campaign
    data = json.loads(path.read_text())
    run = Path(data["runs"][0]["run_dir"])
    if fault == "duplicate_pair":
        data["pairs"].append(data["pairs"][0])
    elif fault == "reversed_duplicate_pair":
        data["pairs"].append("synthetic:1:2,1")
    elif fault == "duplicate_run":
        data["runs"][1] = copy.deepcopy(data["runs"][0])
    elif fault in ("wrong_arm", "wrong_round", "mixed_code"):
        metadata = json.loads((run / "metadata.json").read_text())
        key, value = {"wrong_arm": ("coordination_mode", "group-chat"), "wrong_round": ("round", 3), "mixed_code": ("git_commit", "other")}[
            fault
        ]
        metadata[key] = value
        (run / "metadata.json").write_text(json.dumps(metadata))
    elif fault == "mixed_model":
        (run / "rendered-config/runtime-config.json").write_text(json.dumps({"model": "other"}))
    elif fault in ("non_boolean", "wrong_identity"):
        file = run / "logs/real/team/synthetic/1/f1_f2/eval.json"
        verdict = json.loads(file.read_text())
        verdict["both_passed" if fault == "non_boolean" else "task_id"] = "false" if fault == "non_boolean" else 999
        file.write_text(json.dumps(verdict))
    else:
        data["pairs_sha256"] = "bad"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        module.summarize_campaign(path)


@pytest.mark.parametrize("fault", ["missing_eval", "infrastructure_error", "missing_metadata", "missing_trajectory"])
def test_incomplete_coverage_withholds_rates(campaign, fault):
    module, path = campaign
    data = json.loads(path.read_text())
    run = Path(data["runs"][0]["run_dir"])
    directory = run / "logs/real/team/synthetic/1/f1_f2"
    if fault == "infrastructure_error":
        (directory / "eval.json").write_text(json.dumps({"infrastructure_error": "synthetic transport outage"}))
    else:
        file = {
            "missing_eval": directory / "eval.json",
            "missing_metadata": run / "metadata.json",
            "missing_trajectory": directory / "trajectory.jsonl.gz",
        }[fault]
        file.unlink()
    summary = module.summarize_campaign(path)
    assert not summary["complete"] and summary["issues"]
    assert all(arm["pair_rate"] is None for arm in summary["arms"])
    assert "INCOMPLETE" in module.markdown(summary)


def test_coordinator_export_uses_actual_client_messages_and_schemas(tmp_path):
    module = script("export_coordinator_inputs")
    package = module.export_inputs(tmp_path)
    assert len(package["coordinator_requests"]) == 24 and package["synthetic"]
    for capture in package["coordinator_requests"]:
        request = capture["request"]
        expected_roles = ["system", "user"] if capture["mode"] == "group-chat-planning" else ["user"]
        assert [message["role"] for message in request["messages"]] == expected_roles
        assert request["tool_choice"] == "auto"
        observation = json.loads(request["messages"][-1]["content"].split("OBSERVATION:\n")[1])
        assert ("replies" in observation) == (capture["mode"] == "direct")
        assert "frozen_evidence" not in observation
        if capture["mode"] != "direct":
            assert set(observation["group_chat"]) == {"unread", "recent", "backlog"}
        tools = [tool["function"]["name"] for tool in request["tools"]]
        if capture["stage"] == "forced-finalization":
            assert tools == ["update_notebook"]
            assert observation["planning"]["phase"] == "finalizing"
        elif capture["mode"] == "group-chat-planning" and capture["stage"] != "implementation-monitor":
            assert "finish_planning" in tools
        else:
            assert "finish_planning" not in tools
    assert len(list(tmp_path.glob("*.json"))) == 25


def test_worker_export_uses_real_templates_queries_and_planning_barrier(tmp_path, monkeypatch):
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent
    from cooperagents.workers.mini_swe_worker import _solo_config

    module = script("export_worker_inputs")
    query = DefaultAgent.query
    queried = []

    def tracked_query(agent):
        queried.append(agent.agent_id)
        return query(agent)

    monkeypatch.setattr(DefaultAgent, "query", tracked_query)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "do-not-copy-this-fixture-secret")
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://do-not-copy-this-fixture.invalid")
    monkeypatch.setenv("COOPER_TEMPERATURE_FORCE", "0.25")
    package = module.export_inputs(tmp_path)
    assert len(package["worker_requests"]) == len(queried) == 64
    assert len(list(tmp_path.glob("*.json"))) == 65
    assert "before provider/HTTP conversion" in package["capture_boundary"]
    assert "do-not-copy-this-fixture" not in json.dumps(package)
    baseline_keys = {
        (capture["variant"], capture["worker"], capture["stage"])
        for capture in package["worker_requests"]
        if capture["mode"] == "direct"
    }
    for capture in package["worker_requests"]:
        assert (capture["variant"], capture["worker"], capture["baseline_stage"]) in baseline_keys
        request = capture["request"]
        assert request["messages"][0]["role"] == "system"
        assert request["messages"][0]["content"].startswith(_solo_config()["agent"]["system_template"].strip())
        assert "## Recommended Workflow" in request["messages"][1]["content"]
        assert "## Submission" in request["messages"][1]["content"]
        assert "SYNTHETIC task " + capture["worker"] in request["messages"][1]["content"]
        assert request["api_key"] == "synthetic-offline-key" and request["api_base"] == "https://synthetic.invalid/v1"
        assert request["temperature"] == 1
        assert not {"variant", "mode", "path", "stage", "worker", "baseline_stage"}.intersection(request)
        tools = {tool["function"]["name"]: tool for tool in request["tools"]}
        assert {"bash", "send_message"} <= tools.keys()
        planning = capture["mode"] == "group-chat-planning"
        implementing = capture["stage"] == "implementation"
        assert ("confirm_plan" in tools) == (planning and not implementing)
        assert ("wait" in tools["send_message"]["function"]["parameters"]["properties"]) == (capture["mode"] == "direct")
        if capture["stage"] == "initial":
            assert ("[PLANNING MODE]" in json.dumps(request["messages"])) == planning
            assert not any(message.get("role") == "assistant" for message in request["messages"])
        elif capture["stage"] == "chat-received":
            peer = "agent2" if capture["worker"] == "agent1" else "agent1"
            users = "\n".join(message["content"] for message in request["messages"] if message["role"] == "user")
            assert module.REPORTS[peer] in users
            assert ("[group #" in users) == (capture["mode"] != "direct")
        elif capture["stage"] == "notebook-delivered":
            assert capture["notebook_version"] == 1
            assert any(
                module.PLAN in (
                    json.loads(message["content"]).get("output", "")
                    if message["role"] == "tool" else message.get("content", "")
                )
                for message in request["messages"]
            )
            if planning:
                assert any("<planning_notebook>" in message.get("content", "") for message in request["messages"])
        if implementing:
            assert capture["host_phase"] == "implementation" and capture["final_plan_delivered_to"] == ["agent1", "agent2"]
            assert any(
                message["role"] == "user" and "[IMPLEMENTATION MODE]" in message.get("content", "")
                for message in request["messages"]
            )
    for scenario in package["scenarios"]:
        assert scenario["worker_steps"] == {"agent1": 4, "agent2": 4}
        if scenario["mode"] == "group-chat-planning":
            assert scenario["outcome"] == scenario["path"]
            events = scenario["host_events"]
            delivered = [event for event in events if event["event"] == "plan_delivery"]
            implementation = [
                event for event in events
                if event["event"] == "worker_request" and event["data"]["phase"] == "implementation"
            ]
            assert len(delivered) == len(implementation) == 2
            assert max(event["seq"] for event in delivered) < min(event["seq"] for event in implementation)
            assert delivered[0]["data"]["content"] == delivered[1]["data"]["content"]
