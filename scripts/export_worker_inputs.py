"""Export synthetic inputs at the production adapter -> litellm.completion boundary.

The requests are raw completion kwargs, before LiteLLM's provider/HTTP conversion.
Worker templates, histories, tools and planning delivery use production code;
only external completion responses, environment results and scenario timing are fake.
"""

import argparse
import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import litellm

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import ExecResult
from cooperagents.harness import _Coordinator, _TeammatePoller
from cooperagents.types import Assignment
from cooperagents.workers.mini_swe_worker import BusComm, run_mini_swe_agent

VARIANTS = ("auto-prompt", "human-prompt")
MODES = ("direct", "group-chat", "group-chat-planning")
WORKERS = ("agent1", "agent2")
FIXED_TIME = 1801600000.0
PLAN = (
    "# Synthetic shared notebook\nagent1 owns src/alpha.py: alpha(); "
    "agent2 owns src/beta.py: beta().\nShared APIs: none. Dependencies: none. "
    "Run the existing local tests for each function.\n"
    "Evidence is synthetic and verification is unperformed."
)
REPORTS = {
    "agent1": "SYNTHETIC report: inspected src/alpha.py; propose agent1 owns alpha(); no shared API.",
    "agent2": "SYNTHETIC report: inspected src/beta.py; propose agent2 owns beta(); no dependency.",
}


def export_inputs(destination: Path) -> dict:
    """Run two real worker loops per scenario and capture unmodified API kwargs."""
    captures, scenarios = [], []
    env = {key: value for key, value in os.environ.items() if not key.startswith("COOPER_")}
    env.update(
        OPENAI_API_KEY="synthetic-offline-key",
        AZURE_OPENAI_API_KEY="synthetic-offline-key",
        OPENAI_BASE_URL="https://synthetic.invalid/v1",
        AZURE_OPENAI_BASE_URL="https://synthetic.invalid/v1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT="1",
        COOPER_TEMPERATURE="1",
        COOPER_TOP_P=".95",
        COOPER_TOP_K="20",
        COOPER_PRESENCE_PENALTY="1.5",
        COOPER_CHAT_TEMPLATE_ENABLE_THINKING="false",
    )
    with (
        tempfile.TemporaryDirectory(prefix="worker-inputs-") as temporary,
        patch.dict(os.environ, env, clear=True),
        patch("cooperagents.harness.time.time", lambda: FIXED_TIME),
    ):
        for variant in VARIANTS:
            for mode in MODES:
                for path in ("agreed", "forced") if mode == "group-chat-planning" else ("normal",):
                    requests, scenario = _scenario(Path(temporary), variant, mode, path)
                    captures.extend(requests)
                    scenarios.append(scenario)
    package = {
        "synthetic": True,
        "capture_boundary": "Production model adapter kwargs passed to litellm.completion; before provider/HTTP conversion.",
        "description": (
            "All tasks, source outputs, model responses, coordinator choices and scheduling are synthetic. "
            "Requests use run_mini_swe_agent -> DefaultAgent.step/query -> LitellmModel. "
            "No real model behavior, network calls, source edits or official evaluation. "
            "Raw request contents are deep-copied at invocation, never normalized or rewritten."
        ),
        "baseline_matching": {
            "keys": ["variant", "worker", "baseline_stage"],
            "mode": "direct",
            "path": "normal",
            "note": "Direct has no planning phase: notebook-delivered and regular-execution are real baseline requests at notebook v1.",
        },
        "worker_requests": captures,
        "scenarios": scenarios,
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "worker-inputs.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n")
    for capture in captures:
        name = "-".join(capture[key] for key in ("variant", "mode", "path", "stage", "worker"))
        (destination / f"{name}.json").write_text(json.dumps(capture, ensure_ascii=False, indent=2) + "\n")
    return package


def _scenario(temporary: Path, variant: str, mode: str, path: str) -> tuple[list[dict], dict]:
    planning = mode == "group-chat-planning"
    tasks = {
        aid: f"SYNTHETIC task {aid}: implement {'alpha()' if aid == 'agent1' else 'beta()'} using existing local tests." for aid in WORKERS
    }
    captures, events, results = [], [], {}
    lock = threading.Lock()
    initial = threading.Barrier(2)
    reached = {stage: {aid: threading.Event() for aid in WORKERS} for stage in ("reports", "candidate", "finish")}
    released = {stage: threading.Event() for stage in reached}
    confirmations = {aid: threading.Event() for aid in WORKERS}
    call_counts = {aid: 0 for aid in WORKERS}
    decisions = []

    def trace(actor, event, **data):
        if event in {"group_message", "group_delivery", "plan_confirmation", "plan_delivery", "planning_end", "worker_request"}:
            with lock:
                events.append(dict(seq=len(events) + 1, actor=actor, event=event, data=copy.deepcopy(data)))
        if event == "plan_confirmation" and data["accepted"]:
            confirmations[data["target"]].set()

    notebook = temporary / variant / mode / path / "notebook.md"

    def execute(aid, command, **kwargs):
        if "synthetic_sync_" in command:
            stage = command.rsplit("synthetic_sync_", 1)[1]
            reached[stage][aid].set()
            if not released[stage].wait(10):
                raise TimeoutError(f"Synthetic {stage} barrier timed out for {aid}")
            return ExecResult(f"SYNTHETIC {stage} barrier released", 0)
        if command.endswith("cat /coordination/notebook.md"):
            return ExecResult(notebook.read_text(), 0)
        if "cat src/" in command:
            if aid == "agent2" and not reached["reports"]["agent1"].wait(10):
                raise TimeoutError("Synthetic report ordering barrier timed out")
            symbol = "alpha" if aid == "agent1" else "beta"
            return ExecResult(f"# SYNTHETIC inspected source\ndef {symbol}(): pass\n", 0)
        if command.endswith("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"):
            return ExecResult("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", 0)
        return ExecResult("", 0)  # Synthetic clean git status/fetch/diff results.

    envs = {
        aid: SimpleNamespace(repo_path="/workspace/repo", execute=lambda command, aid=aid, **kwargs: execute(aid, command, **kwargs))
        for aid in WORKERS
    }
    bus = InMemoryBus("synthetic-worker-inputs")
    coordinator = _Coordinator(
        envs,
        "SYNTHETIC-Qwen3.5-9B",
        assignments=[Assignment(agent_id=aid, role="member", task=tasks[aid], feature_id=i) for i, aid in enumerate(WORKERS, 1)],
        bus=bus,
        notebook_path=notebook,
        coordination_variant=variant,
        coordination_mode=mode,
        step_limit=1000,
        time_limit_s=3600,
        trace=lambda event, **data: trace("coordinator", event, **data),
        complete=lambda prompt: copy.deepcopy(decisions),
    )

    def message_actions(content):
        return [
            {"name": "send_message", "arguments": json.dumps({"recipient": aid, "content": content})}
            for aid in (WORKERS if mode == "direct" else ("group",))
        ]

    decisions[:] = message_actions("SYNTHETIC coordinator: inspect source and publicly report scope before proposing ownership.")
    coordinator.decide(initial=True)
    coordinator.start_planning()

    def complete(**kwargs):
        task_text = kwargs["messages"][1]["content"]
        aid = next(aid for aid in WORKERS if tasks[aid] in task_text)
        with lock:
            index = call_counts[aid]
            call_counts[aid] += 1
            stage = ("initial", "chat-received", "notebook-delivered", "implementation" if planning else "regular-execution")[index]
            capture = dict(
                synthetic=True,
                variant=variant,
                mode=mode,
                path=path,
                stage=stage,
                worker=aid,
                baseline_stage="regular-execution" if stage == "implementation" else stage,
                request=copy.deepcopy(kwargs),
                host_phase=coordinator.phase,
                notebook_version=coordinator._version,
                final_plan_delivered_to=sorted(coordinator._delivered),
            )
            captures.append(capture)
        if index == 0:
            initial.wait(10)
            recipients = ("agent2" if aid == "agent1" else "agent1", "coordinator") if mode == "direct" else ("group",)
            actions = [("bash", {"command": f"cat src/{'alpha' if aid == 'agent1' else 'beta'}.py"})]
            actions += [("send_message", {"recipient": recipient, "content": REPORTS[aid]}) for recipient in recipients]
            actions.append(("bash", {"command": "synthetic_sync_reports"}))
        elif index == 1:
            actions = [("bash", {"command": "synthetic_sync_candidate"}), ("bash", {"command": "cat /coordination/notebook.md"})]
        elif index == 2:
            actions = (
                [("confirm_plan", {"version": coordinator._version})]
                if planning and path == "agreed"
                else [("bash", {"command": "synthetic_sync_finish"})]
            )
        else:
            actions = [("bash", {"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})]
        capture["synthetic_response_actions"] = copy.deepcopy(actions)
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": "SYNTHETIC response fixture, not observed model behavior.",
                        "tool_calls": [
                            {
                                "id": f"synthetic-{aid}-{index}-{i}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                            for i, (name, args) in enumerate(actions)
                        ],
                    }
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        )

    def worker(aid):
        poller = _TeammatePoller(aid, envs)
        poller.watch_coordinator(coordinator)
        poller.watch_gitshare()
        results[aid] = run_mini_swe_agent(
            envs[aid],
            task=tasks[aid],
            agent_id=aid,
            role="member",
            feature_id=1 if aid == "agent1" else 2,
            model_name="SYNTHETIC-Qwen3.5-9B",
            step_limit=1000,
            cost_limit=5,
            time_limit_s=3600,
            comm=BusComm(bus, aid, group=coordinator if mode != "direct" else None),
            monitor=coordinator,
            poller=poller,
            tool_protocol=True,
            git_share=True,
            trace=lambda event, **data: trace(aid, event, **data),
        )

    threads = [threading.Thread(target=worker, args=(aid,), name=f"synthetic-{aid}") for aid in WORKERS]

    def wait_all(signals, label):
        for aid in WORKERS:
            if not signals[aid].wait(10):
                raise RuntimeError(f"Synthetic {label} did not complete for {aid}")

    with patch("litellm.completion", complete):
        try:
            for thread in threads:
                thread.start()
            wait_all(reached["reports"], "reports")
            decisions[:] = message_actions("SYNTHETIC coordinator: both reports received; resolve scope in the shared notebook.")
            coordinator.decide()
            released["reports"].set()
            wait_all(reached["candidate"], "candidate preparation")
            decisions[:] = [{"name": "update_notebook", "arguments": json.dumps({"content": PLAN})}]
            coordinator.decide()
            released["candidate"].set()
            if planning and path == "agreed":
                wait_all(confirmations, "candidate confirmations")
                decisions[:] = [{"name": "finish_planning", "arguments": json.dumps({"version": coordinator._version})}]
                coordinator.decide()
            else:
                wait_all(reached["finish"], "final request preparation")
                if planning:
                    with coordinator._condition:
                        coordinator._planning_steps["agent2"] = 200
                    decisions[:] = [
                        {
                            "name": "update_notebook",
                            "arguments": json.dumps(
                                {"content": PLAN + "\nForced host decision: discussion budget exhausted; no worker agreement is claimed."}
                            ),
                        }
                    ]
                    coordinator.decide()
                released["finish"].set()
            for thread in threads:
                thread.join(10)
                if thread.is_alive():
                    raise RuntimeError("Synthetic worker did not finish")
            if set(results) != set(WORKERS) or any(result.status != "submitted" for result in results.values()):
                raise RuntimeError(
                    f"Synthetic worker loop failed: {[(aid, result.status, result.error) for aid, result in results.items()]}"
                )
        finally:
            coordinator.stop()
            initial.abort()
            for signal in released.values():
                signal.set()
            for signals in reached.values():
                for signal in signals.values():
                    signal.set()
            for thread in threads:
                thread.join(10)
    captures.sort(
        key=lambda item: (
            ("initial", "chat-received", "notebook-delivered", "implementation" if planning else "regular-execution").index(item["stage"]),
            item["worker"],
        )
    )
    return captures, dict(
        synthetic=True,
        variant=variant,
        mode=mode,
        path=path,
        outcome=coordinator._planning_outcome if planning else None,
        worker_steps={aid: result.steps for aid, result in results.items()},
        host_events=events,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = export_inputs(args.output)
    print(f"Exported {len(package['worker_requests'])} synthetic worker completion inputs to {args.output}")


if __name__ == "__main__":
    main()
