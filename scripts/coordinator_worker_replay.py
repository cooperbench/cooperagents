"""Five-call paired worker replay from one archived coordinator decision point.

For non-initial points, require a reviewed workspace snapshot. A conversation
without its matching code state is not a valid counterfactual replay.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
from functools import partial
from importlib.resources import files
from pathlib import Path

import yaml

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import Environment, ExecResult
from cooperagents.env.docker import DockerEnv
from cooperagents.env.runtime import task_environment
from cooperagents.harness import _Coordinator
from cooperagents.trajectory import Trajectory, record_call
from cooperagents.types import Assignment
from cooperagents.vendor.mini_swe.exceptions import FormatError
from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import parse_toolcall_actions


def required(result: ExecResult, label: str) -> str:
    if result.exit_code:
        raise RuntimeError(f"{label}: {result.stdout}")
    return result.stdout


def archive(point: dict) -> tuple[list[dict], dict, dict[str, dict]]:
    with gzip.open(point["trajectory"], "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    request = rows[point["worker_request_seq"] - 1]["data"]["request"]
    starts = {row["actor"]: row["data"] for row in rows if row["event"] == "agent_start" and row["actor"] in {"agent1", "agent2"}}
    return rows, request, starts


def verified_patch(point: dict, state_dir: Path | None) -> Path | None:
    if point["workspace_status"] == "base_image":
        if point["archive_dirty_files"]:
            raise ValueError("Initial source state is dirty; a base-image replay would be invalid")
        return None
    if state_dir is None:
        raise ValueError(f"{point['id']} needs a reviewed workspace snapshot (--state-dir)")
    patch = state_dir / f"{point['id']}.patch"
    evidence_path = state_dir / f"{point['id']}.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    if (
        evidence.get("trajectory") != point["trajectory"]
        or evidence.get("worker_request_seq") != point["worker_request_seq"]
        or evidence.get("patch_sha256") != digest
        or evidence.get("archive_dirty_files") != point["archive_dirty_files"]
        or evidence.get("reviewed_against_archive") is not True
    ):
        raise ValueError("Snapshot provenance is missing or differs from the dataset")
    return patch


def materialize_actions(point: dict, candidate: dict, notebook_path: Path) -> tuple[list[str], str, str]:
    if not isinstance(candidate.get("actions"), list) or not isinstance(candidate.get("worker_coordination_suffix", ""), str):
        raise ValueError("Candidate needs actions and a worker coordination suffix")
    if len(candidate.get("worker_coordination_suffix", "")) > 2000:
        raise ValueError("Worker coordination suffix exceeds 2000 characters")
    if candidate.get("case_id") != point["id"]:
        raise ValueError("Candidate case_id differs from source point")
    _, _, starts = archive(point)
    assignments = [Assignment(aid, item["task"], item["role"], item["feature_id"]) for aid, item in starts.items()]
    coordinator = _Coordinator(
        {},
        assignments=assignments,
        bus=InMemoryBus("research"),
        notebook_path=notebook_path,
        complete=lambda _: [],
    )
    calls = [{"name": action["name"], "arguments": json.dumps(action["arguments"], ensure_ascii=False)} for action in candidate["actions"]]
    parsed = coordinator._parse_actions(calls)
    coordinator._apply_actions(parsed)
    notices = coordinator.drain(point["target"])
    return notices, notebook_path.read_text(encoding="utf-8"), coordinator.worker_instructions()


def render_notices(notices: list[str], candidate: dict) -> list[str]:
    label = candidate.get("coordinator_notice_label", "")
    if not isinstance(label, str) or len(label) > 128 or any(char in label for char in "[]\r\n"):
        raise ValueError("Coordinator notice label must be plain text of at most 128 characters")
    if label:
        notices = [
            notice.replace("[coordinator", f"[{label}", 1) if notice.startswith(("[coordinator]", "[coordinator;")) else notice
            for notice in notices
        ]
    prefix = candidate.get("coordinator_notice_prefix", "")
    if not isinstance(prefix, str) or len(prefix) > 128:
        raise ValueError("Coordinator notice prefix must be a string of at most 128 characters")
    return [prefix + "\n" + notice for notice in notices] if prefix else notices


def replay_environment(image: str, notebook_dir: Path) -> Environment:
    if os.getenv("COOPER_RUNTIME", "docker") == "docker":
        return DockerEnv(image, network="none", volumes=[f"{notebook_dir}:/coordination:ro"], keepalive="8h")
    return task_environment(image, coordinator_dir=notebook_dir)


def seed_environment(env: Environment, point: dict, patch: Path | None) -> None:
    if patch is not None:
        env.write_file("/tmp/coordinator-replay.patch", patch.read_text(encoding="utf-8"))
        required(
            env.execute("git apply /tmp/coordinator-replay.patch"),
            "apply workspace snapshot",
        )
    status = required(
        env.execute("git -c core.quotepath=false status --porcelain=v1 -z --no-renames"),
        "inspect workspace",
    )
    actual = sorted(
        {item[3:] for item in status.split("\0") if len(item) > 3 and item[3:] != "patch.txt" and not item[3:].startswith(".cb_")}
    )
    if actual != sorted(point["archive_dirty_files"]):
        raise ValueError(f"Workspace files differ from archive: {actual} != {point['archive_dirty_files']}")


def remove_superseded_notice(messages: list[dict], rows: list[dict], point: dict) -> list[dict]:
    if point["phase"] != "conflict":
        return messages
    old = [
        row["data"]["text"]
        for row in rows
        if point["boundary_seq"] < row["seq"] < point["worker_request_seq"]
        and row["actor"] == "coordinator"
        and row["event"] == "nudge"
        and row["data"].get("target") == point["target"]
    ]
    if not old:
        raise ValueError("Conflict point has no original notice to replace")
    for notice in old:
        found = False
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and notice in content:
                message["content"] = content.replace(notice, "").strip()
                found = True
        if not found:
            raise ValueError("Archived worker request lacks the original coordinator notice")
    return [message for message in messages if message.get("content") or message.get("tool_calls") or message.get("role") == "system"]


def replay_one(point: dict, candidate: dict, state_dir: Path | None, label: str, trace_path: Path | None = None) -> dict:
    from openai import OpenAI

    rows, request, _ = archive(point)
    with tempfile.TemporaryDirectory(prefix="coordinator-replay-") as temporary:
        notices, _, instructions = materialize_actions(point, candidate, Path(temporary) / "notebook.md")
        notices = render_notices(notices, candidate)
        patch = verified_patch(point, state_dir)
        env = replay_environment(point["image"], Path(temporary))
        trajectory = None
        try:
            trajectory = Trajectory(trace_path) if trace_path is not None else None
            trace = partial(trajectory.emit, "worker") if trajectory else None
            seed_environment(env, point, patch)
            messages = [
                {
                    key: value
                    for key, value in message.items()
                    if key in {"role", "content", "tool_calls", "tool_call_id", "name"} and value is not None
                }
                for message in request["messages"]
            ]
            messages = remove_superseded_notice(messages, rows, point)
            messages[0]["content"] += instructions
            suffix = candidate.get("worker_coordination_suffix", "")
            if suffix:
                messages[0]["content"] += "\n\nCOORDINATION PROTOCOL: " + suffix
            baseline_hash = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
            if notices:
                messages.append({"role": "user", "content": "\n".join(notices)})
            model = os.environ.get("COOPER_REPLAY_MODEL", "qwen3.5-9b")
            client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ.get("OPENAI_API_KEY", "dummy"), timeout=120)
            config = files("cooperagents.vendor.mini_swe").joinpath("config", "solo.yaml")
            format_error_template = yaml.safe_load(config.read_text())["model"]["format_error_template"]
            steps = []
            for index in range(point["replay_model_calls"]):
                response = record_call(
                    trace,
                    client.chat.completions.create,
                    model=model,
                    messages=messages,
                    tools=request["tools"],
                    tool_choice="auto",
                    temperature=0,
                    top_p=0.95,
                    presence_penalty=1.5,
                    max_tokens=1200,
                    extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}},
                )
                choice = response.choices[0]
                reply = choice.message
                tool_calls = reply.tool_calls or []
                try:
                    parse_toolcall_actions(
                        tool_calls,
                        format_error_template=format_error_template,
                    )
                except FormatError as error:
                    steps.append(
                        {
                            "index": index + 1,
                            "content": reply.content,
                            "actions": [],
                            "usage": response.usage.model_dump() if response.usage else None,
                            "accepted": False,
                            "error": error.messages[0]["content"],
                        }
                    )
                    messages.extend({"role": message["role"], "content": message["content"]} for message in error.messages)
                    continue
                calls = [
                    {"id": tool.id, "type": "function", "function": {"name": tool.function.name, "arguments": tool.function.arguments}}
                    for tool in tool_calls
                ]
                messages.append({"role": "assistant", "content": reply.content or "", **({"tool_calls": calls} if calls else {})})
                actions = []
                for call in calls:
                    function = call["function"]
                    simulation = None
                    try:
                        arguments = json.loads(function["arguments"])
                        if function["name"] == "bash":
                            result = env.execute(arguments["command"], timeout=90)
                            observation = {"returncode": result.exit_code, "output": result.stdout[:20000]}
                        elif function["name"] == "send_message":
                            # With no peer loop, a blocking send reaches the real 60s timeout
                            # with no reply. Skip wall time but preserve that tool observation.
                            if arguments.get("wait"):
                                simulation = {"outcome": "wait_timeout_no_peer", "timeout_seconds": 60}
                            observation = {
                                "output": f"Message sent to {arguments['recipient']}",
                                "returncode": 0,
                                "exception_info": "",
                            }
                        else:
                            observation = {"returncode": 1, "output": "Unsupported tool"}
                    except (KeyError, ValueError) as exc:
                        arguments = function["arguments"]
                        observation = {"returncode": 1, "output": f"{type(exc).__name__}: {exc}"}
                    action = {"tool": function["name"], "arguments": arguments, "observation": observation}
                    if simulation:
                        action["simulation"] = simulation
                    actions.append(action)
                    if trace is not None:
                        trace("tool_action", step=index + 1, tool_call_id=call["id"], action=action)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(observation, ensure_ascii=False)})
                steps.append(
                    {
                        "index": index + 1,
                        "content": reply.content,
                        "actions": actions,
                        "usage": response.usage.model_dump() if response.usage else None,
                        "accepted": True,
                    }
                )
            diff = required(env.execute("git diff HEAD"), "read final diff")
            return {
                "case_id": point["id"],
                "label": label,
                "candidate": candidate,
                "model": model,
                "source_request_seq": point["worker_request_seq"],
                "notices": notices,
                "initial_messages_sha256": baseline_hash,
                "steps": steps,
                "final_diff": diff,
                "workspace_evidence": "base_image" if patch is None else str(patch),
                **({"trace_path": str(trace_path)} if trace_path is not None else {}),
            }
        finally:
            try:
                if trajectory is not None:
                    trajectory.close()
            finally:
                env.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--control-candidate", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    point = next(p for p in map(json.loads, args.dataset.read_text(encoding="utf-8").splitlines()) if p["id"] == args.case)
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    control = (
        json.loads(args.control_candidate.read_text(encoding="utf-8"))
        if args.control_candidate
        else {"case_id": point["id"], "actions": [], "worker_coordination_suffix": ""}
    )
    if control.get("case_id") != point["id"]:
        raise ValueError("Control candidate case_id differs from source point")
    args.output.mkdir(parents=True, exist_ok=True)
    for label, arm in (("control", control), ("candidate", candidate)):
        path = args.output / f"{label}.json"
        if path.exists():
            raise FileExistsError(path)
        trace_path = args.output / f"{label}.trace.jsonl"
        path.write_text(
            json.dumps(replay_one(point, arm, args.state_dir, label, trace_path=trace_path), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"Saved paired replays to {args.output}")


if __name__ == "__main__":
    main()
