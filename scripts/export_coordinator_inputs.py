"""Export synthetic coordinator requests through the real planner/client construction path.

No network or model invocation: only OpenAI's transport is replaced. All scenario
reports, tool choices and worker observations are explicitly synthetic fixtures.
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

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import ExecResult
from cooperagents.harness import _Coordinator
from cooperagents.types import Assignment

VARIANTS = ("auto-prompt", "human-prompt")
MODES = ("direct", "group-chat", "group-chat-planning")
FIXED_TIME = 1801600000.0
PLAN = (
    "# Synthetic candidate\nagent1 owns src/alpha.py: alpha(); agent2 owns src/beta.py: beta().\n"
    "Shared APIs: none. Dependencies: none. Verify each function with the existing local tests.\n"
    "Evidence: synthetic reports below; no real repository was inspected."
)
REPORTS = {
    "agent1": "SYNTHETIC inspection: src/alpha.py defines alpha(); propose agent1 owns alpha(). No shared API.",
    "agent2": "SYNTHETIC inspection: src/beta.py defines beta(); propose agent2 owns beta(). No dependency.",
}


def export_inputs(destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    captures, contexts = [], []
    response_calls = []
    scenario = {}

    def transport(**kwargs):
        captures.append(dict(synthetic=True, **scenario, request=copy.deepcopy(kwargs)))
        calls = [SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(args))) for name, args in response_calls]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls))])

    def make_worker(aid, coordinator):
        messages = [
            {
                "role": "system",
                "content": "SYNTHETIC worker system fixture; production coordinator supplement follows."
                + coordinator.worker_instructions(),
            },
            {"role": "user", "content": f"SYNTHETIC task for {aid}"},
            {
                "role": "assistant",
                "content": "Synthetic inspection action",
                "extra": {"actions": [{"tool_name": "bash", "command": f"cat src/{'alpha' if aid == 'agent1' else 'beta'}.py"}]},
            },
            {
                "role": "user",
                "content": "Synthetic inspected source",
                "extra": {"raw_output": "def alpha(): pass" if aid == "agent1" else "def beta(): pass", "returncode": 0},
            },
        ]
        return SimpleNamespace(
            messages=messages,
            n_calls=3,
            cost=0,
            _compaction_count=0,
            config=SimpleNamespace(step_limit=1000, cost_limit=5, wall_deadline=FIXED_TIME + 3600),
            model=SimpleNamespace(format_message=lambda **kwargs: kwargs),
            add_messages=lambda m: messages.append(m),
        )

    env = {key: value for key, value in os.environ.items() if not key.startswith("COOPER_")}
    env.update(
        OPENAI_API_KEY="synthetic-offline",
        OPENAI_BASE_URL="https://synthetic.invalid/v1",
        COOPER_TEMPERATURE="1",
        COOPER_TOP_P=".95",
        COOPER_TOP_K="20",
        COOPER_PRESENCE_PENALTY="1.5",
        COOPER_CHAT_TEMPLATE_ENABLE_THINKING="false",
    )
    with (
        tempfile.TemporaryDirectory(prefix="coordinator-inputs-") as temporary,
        patch.dict(os.environ, env, clear=True),
        patch("openai.OpenAI", lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=transport)))),
        patch("cooperagents.harness.time.time", lambda: FIXED_TIME),
    ):
        for variant in VARIANTS:
            for mode in MODES:
                for path_kind in ("normal", "forced") if mode == "group-chat-planning" else ("normal",):
                    scenario.clear()
                    scenario.update(variant=variant, mode=mode, path=path_kind)
                    roster = [
                        Assignment(
                            agent_id=aid, role="member", task=f"SYNTHETIC: implement {'alpha()' if i == 1 else 'beta()'}", feature_id=i
                        )
                        for i, aid in enumerate(REPORTS, 1)
                    ]
                    bus = InMemoryBus(f"{variant}-{mode}-{path_kind}")
                    envs = {aid: SimpleNamespace(execute=lambda *a, **k: ExecResult("", 0)) for aid in REPORTS}
                    c = _Coordinator(
                        envs,
                        "SYNTHETIC-Qwen3.5-9B",
                        assignments=roster,
                        bus=bus,
                        notebook_path=Path(temporary) / variant / mode / path_kind / "notebook.md",
                        coordination_variant=variant,
                        coordination_mode=mode,
                        repo="synthetic_repository",
                        task_id=7,
                        step_limit=1000,
                        time_limit_s=3600,
                    )
                    scenario["stage"] = "initial"
                    response_calls[:] = []
                    c.decide(initial=True)
                    c.start_planning()
                    reports = (
                        REPORTS
                        if path_kind == "normal"
                        else {
                            "agent1": (
                                "SYNTHETIC inspection: shared.py has parser callers. Propose agent1 writes parse_record(text: str) -> dict."
                            ),
                            "agent2": (
                                "SYNTHETIC inspection: beta needs parsed records. Disagree: "
                                "agent2 should write parse_record(text: bytes) -> Record."
                            ),
                        }
                    )
                    for aid, report in reports.items():
                        worker = make_worker(aid, c)
                        c.register(aid, worker)
                        if mode == "direct":
                            bus.send(sender=aid, to="coordinator", content=report)
                        else:
                            c.publish(aid, "group", report)
                    scenario["stage"] = "regular-group-observation" if mode != "direct" else "regular-direct-observation"
                    response_calls[:] = [
                        (
                            "update_notebook",
                            {
                                "content": PLAN
                                if path_kind == "normal"
                                else "# Synthetic disputed candidate\nOwnership of shared.py and parse_record signature remain unresolved: "
                                "agent1 proposes str -> dict; agent2 proposes bytes -> Record. No agreement or verification."
                            },
                        )
                    ]
                    c.decide()
                    if mode == "group-chat-planning":
                        for aid in REPORTS:
                            c.before_query(aid, c._agents[aid])
                            contexts.append(
                                dict(
                                    synthetic=True,
                                    **(scenario | {"stage": "candidate-delivered"}),
                                    worker=aid,
                                    messages=copy.deepcopy(c._agents[aid].messages),
                                )
                            )
                        if path_kind == "normal":
                            for aid in REPORTS:
                                c.confirm_plan(aid, {"version": c._version}, sole=True)
                            scenario["stage"] = "planning-confirmation"
                            response_calls[:] = [("finish_planning", {"version": c._version})]
                        else:
                            c.confirm_plan("agent1", {"version": c._version}, sole=True)
                            c._planning_steps["agent2"] = 200
                            scenario["stage"] = "forced-finalization"
                            response_calls[:] = [
                                (
                                    "update_notebook",
                                    {
                                        "content": "# Synthetic forced final\nagent1 writes shared.py: parse_record(text: str) -> dict; "
                                        "agent2 implements beta using that interface after handoff. Verify both caller paths.\n"
                                        "This decides the reported ownership/signature dispute; tests and Record compatibility "
                                        "remain unverified. Only agent1 confirmed the candidate; agent2 agreement is unknown."
                                    },
                                )
                            ]
                        c.decide()
                        threads = [threading.Thread(target=c.before_query, args=(aid, c._agents[aid])) for aid in REPORTS]
                        try:
                            for thread in threads:
                                thread.start()
                            for thread in threads:
                                thread.join(2)
                                if thread.is_alive():
                                    raise RuntimeError("Synthetic final-plan delivery did not complete")
                        finally:
                            if any(thread.is_alive() for thread in threads):
                                c.stop()
                            for thread in threads:
                                thread.join(2)
                        for aid in REPORTS:
                            contexts.append(
                                dict(
                                    synthetic=True,
                                    **(scenario | {"stage": "implementation-delivered"}),
                                    worker=aid,
                                    messages=copy.deepcopy(c._agents[aid].messages),
                                    metrics=c.planning_metrics(),
                                )
                            )
                        scenario["stage"] = "implementation-monitor"
                        response_calls[:] = []
                        c.decide()
                        c.stop()
    package = dict(
        synthetic=True,
        description="Exact client kwargs from synthetic offline scenarios; no real model responses.",
        coordinator_requests=captures,
        worker_context_fixtures=contexts,
    )
    (destination / "coordinator-inputs.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n")
    for capture in captures:
        name = "-".join(capture[key] for key in ("variant", "mode", "path", "stage"))
        (destination / f"{name}.json").write_text(json.dumps(capture, ensure_ascii=False, indent=2) + "\n")
    return package


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = export_inputs(args.output)
    print(f"Exported {len(data['coordinator_requests'])} synthetic coordinator requests to {args.output}")


if __name__ == "__main__":
    main()
