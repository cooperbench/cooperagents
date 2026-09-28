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
import subprocess
import tempfile
import uuid
from pathlib import Path

from cooperagents.bus.memory import InMemoryBus
from cooperagents.harness import _Coordinator
from cooperagents.types import Assignment


def command(*args: str, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout, check=False)


def docker(*args: str, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return command("docker", *args, input_text=input_text, timeout=timeout)


def required(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode:
        raise RuntimeError(f"{label}: {result.stderr or result.stdout}")
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


def seed_container(name: str, point: dict, patch: Path | None, notebook: str) -> None:
    required(
        docker("run", "-d", "--rm", "--network", "none", "--entrypoint", "sleep", "--name", name, point["image"], "infinity"),
        "start container",
    )
    if patch is not None:
        required(
            docker("exec", "-i", name, "bash", "-lc", "cd /workspace/repo && git apply -", input_text=patch.read_text(encoding="utf-8")),
            "apply workspace snapshot",
        )
    status = required(
        docker("exec", name, "bash", "-lc", "cd /workspace/repo && git -c core.quotepath=false status --porcelain=v1 -z --no-renames"),
        "inspect workspace",
    )
    actual = sorted(
        {item[3:] for item in status.split("\0") if len(item) > 3 and item[3:] != "patch.txt" and not item[3:].startswith(".cb_")}
    )
    if actual != sorted(point["archive_dirty_files"]):
        raise ValueError(f"Workspace files differ from archive: {actual} != {point['archive_dirty_files']}")
    required(
        docker("exec", "-i", name, "bash", "-lc", "mkdir -p /coordination && cat > /coordination/notebook.md", input_text=notebook),
        "mount notebook",
    )


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


def replay_one(point: dict, candidate: dict, state_dir: Path | None, label: str) -> dict:
    from openai import OpenAI

    rows, request, _ = archive(point)
    with tempfile.TemporaryDirectory(prefix="coordinator-replay-") as temporary:
        notices, notebook, instructions = materialize_actions(point, candidate, Path(temporary) / "notebook.md")
    patch = verified_patch(point, state_dir)
    name = "coordinator-replay-" + uuid.uuid4().hex[:10]
    try:
        seed_container(name, point, patch, notebook)
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
        steps = []
        for index in range(point["replay_model_calls"]):
            response = client.chat.completions.create(
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
            calls = [
                {"id": tool.id, "type": "function", "function": {"name": tool.function.name, "arguments": tool.function.arguments}}
                for tool in reply.tool_calls or []
            ]
            messages.append({"role": "assistant", "content": reply.content or "", **({"tool_calls": calls} if calls else {})})
            actions = []
            for call in calls:
                function = call["function"]
                try:
                    arguments = json.loads(function["arguments"])
                    if function["name"] == "bash":
                        result = docker("exec", name, "bash", "-lc", arguments["command"], timeout=90)
                        observation = {"returncode": result.returncode, "output": (result.stdout + result.stderr)[:20000]}
                    elif function["name"] == "send_message":
                        observation = {"returncode": 0, "output": f"Message queued for {arguments['recipient']}"}
                    else:
                        observation = {"returncode": 1, "output": "Unsupported tool"}
                except (KeyError, ValueError, subprocess.TimeoutExpired) as exc:
                    arguments = function["arguments"]
                    observation = {"returncode": 1, "output": f"{type(exc).__name__}: {exc}"}
                actions.append({"tool": function["name"], "arguments": arguments, "observation": observation})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(observation, ensure_ascii=False)})
            steps.append(
                {
                    "index": index + 1,
                    "content": reply.content,
                    "actions": actions,
                    "usage": response.usage.model_dump() if response.usage else None,
                }
            )
            if not calls:
                break
        diff = required(docker("exec", name, "bash", "-lc", "cd /workspace/repo && git diff HEAD"), "read final diff")
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
        }
    finally:
        docker("rm", "-f", name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    point = next(p for p in map(json.loads, args.dataset.read_text(encoding="utf-8").splitlines()) if p["id"] == args.case)
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    control = {"case_id": point["id"], "actions": [], "worker_coordination_suffix": ""}
    args.output.mkdir(parents=True, exist_ok=True)
    for label, arm in (("control", control), ("candidate", candidate)):
        path = args.output / f"{label}.json"
        if path.exists():
            raise FileExistsError(path)
        path.write_text(json.dumps(replay_one(point, arm, args.state_dir, label), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved paired replays to {args.output}")


if __name__ == "__main__":
    main()
