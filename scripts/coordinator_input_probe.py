"""Probe coordinator prompt/observation variants at archived decision points.

Use after effective actions have been found by worker replay. The variant may
add coordination instructions and source-grounded observation notes only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import ExecResult
from cooperagents.harness import _SEND_MESSAGE_TOOL, _UPDATE_NOTEBOOK_TOOL, _Coordinator
from cooperagents.planner import _default_planner_complete
from cooperagents.types import Assignment


def archived_state(point: dict) -> tuple[list[dict], dict[str, list[dict]], dict[str, dict], dict[str, list[str]]]:
    with gzip.open(point["trajectory"], "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    contexts = {"agent1": [], "agent2": []}
    dirty = {"agent1": [], "agent2": []}
    starts = {}
    for row in rows:
        if row["seq"] >= point["boundary_seq"]:
            break
        aid, event, data = row["actor"], row["event"], row["data"]
        if aid in contexts and event == "agent_start":
            starts[aid] = data
        elif aid in contexts and event == "context":
            contexts[aid] = list(data["messages"])
        elif aid in contexts and event == "messages":
            contexts[aid].extend(data["messages"])
        elif aid == "coordinator" and event == "dirty_files" and data.get("target") in dirty:
            dirty[data["target"]] = data["output"].splitlines()
    if len(starts) != 2:
        raise ValueError("Both workers must be present at the decision point")
    return rows, contexts, starts, dirty


def run(point: dict, variant: dict, output: Path, *, dry_run: bool = False) -> dict:
    if (
        variant.get("case_id") != point["id"]
        or not isinstance(variant.get("instruction_addendum", ""), str)
        or not isinstance(variant.get("observation_addendum", ""), str)
        or len(variant.get("instruction_addendum", "")) > 2000
        or len(variant.get("observation_addendum", "")) > 2000
    ):
        raise ValueError("Invalid input variant")
    rows, contexts, starts, dirty = archived_state(point)
    evidence_seqs = variant.get("observation_evidence_seqs", [])
    if not isinstance(evidence_seqs, list) or any(
        not isinstance(seq, int) or seq < 1 or seq >= point["boundary_seq"] for seq in evidence_seqs
    ):
        raise ValueError("Observation evidence must name earlier archive sequence numbers")
    if variant.get("observation_addendum") and not evidence_seqs:
        raise ValueError("An observation addendum needs archived evidence")
    evidence = [
        {
            "seq": seq,
            "actor": rows[seq - 1]["actor"],
            "event": rows[seq - 1]["event"],
            "data_excerpt": json.dumps(rows[seq - 1]["data"], ensure_ascii=False)[:3000],
        }
        for seq in evidence_seqs
    ]
    if point["phase"] == "early":
        contexts = {aid: [] for aid in contexts}
        dirty = {aid: [] for aid in dirty}
    elif dirty[point["target"]] != point["archive_dirty_files"]:
        raise ValueError("Archived modified-file observation differs from dataset")
    roster = [Assignment(aid, item["task"], item["role"], item["feature_id"]) for aid, item in starts.items()]
    envs = {
        aid: SimpleNamespace(execute=lambda *_args, files=files, **_kwargs: ExecResult("".join(f" M {path}\0" for path in files), 0))
        for aid, files in dirty.items()
    }
    captured = {}
    model = os.environ.get("COOPER_REPLAY_MODEL", "qwen3.5-9b")
    complete = (
        (lambda _prompt: [])
        if dry_run
        else _default_planner_complete(
            model,
            None,
            None,
            tools=[_SEND_MESSAGE_TOOL, _UPDATE_NOTEBOOK_TOOL],
            timeout=60,
            max_retries=0,
        )
    )
    if complete is None:
        raise RuntimeError("Coordinator model is not configured")

    def call(prompt: str) -> list[dict[str, str]]:
        prefix, separator, observation = prompt.partition("\nOBSERVATION:\n")
        if not separator:
            raise ValueError("Coordinator observation marker missing")
        add_instruction = variant.get("instruction_addendum", "")
        add_observation = variant.get("observation_addendum", "")
        payload = json.loads(observation)
        if add_observation:
            payload["research_observation_note"] = add_observation
        modified = prefix + ("\n" + add_instruction if add_instruction else "") + separator + json.dumps(payload, ensure_ascii=False)
        captured["baseline_prompt"] = prompt
        captured["modified_prompt"] = modified
        result = complete(modified)
        captured["raw_actions"] = result
        return result

    output.parent.mkdir(parents=True, exist_ok=True)
    notebook = output.with_suffix(".notebook.md")
    coordinator = _Coordinator(
        envs,
        assignments=roster,
        bus=InMemoryBus("research"),
        notebook_path=notebook,
        repo=point["repo"],
        task_id=int(point["pair"].split("/")[1]),
        complete=call,
    )
    boundary_time = datetime.fromisoformat(rows[point["boundary_seq"] - 1]["time"])
    for aid, start in starts.items():
        used = sum(
            row["actor"] == aid and row["event"] == "request" and "messages" in row["data"].get("request", {})
            for row in rows[: point["boundary_seq"] - 1]
        )
        original_limit = start.get("step_limit") or 1000
        limit = used + point["replay_model_calls"] if point["phase"] == "budget" and aid == point["target"] else original_limit
        original_seconds = start.get("time_limit_s")
        remaining = (
            max(
                0,
                original_seconds
                - (
                    boundary_time
                    - datetime.fromisoformat(next(row["time"] for row in rows if row["actor"] == aid and row["event"] == "agent_start"))
                ).total_seconds(),
            )
            if original_seconds
            else None
        )
        config = SimpleNamespace(step_limit=limit, wall_deadline=time.time() + remaining if remaining is not None else None)
        coordinator.register(aid, SimpleNamespace(messages=contexts[aid], n_calls=used, config=config, _compaction_count=0))
    coordinator.decide(initial=point["phase"] == "early")
    result = {
        "case_id": point["id"],
        "variant": variant,
        "model": model,
        "dry_run": dry_run,
        "source_boundary_seq": point["boundary_seq"],
        "observation_evidence": evidence,
        "experimental_five_call_budget": point["phase"] == "budget",
        **captured,
        "delivered": {aid: coordinator.drain(aid) for aid in contexts},
        "notebook": notebook.read_text(encoding="utf-8"),
        "error": str(coordinator.error) if coordinator.error else None,
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--variant", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    point = next(row for row in map(json.loads, args.dataset.read_text(encoding="utf-8").splitlines()) if row["id"] == args.case)
    result = run(point, json.loads(args.variant.read_text(encoding="utf-8")), args.output, dry_run=args.dry_run)
    print(json.dumps({"case_id": point["id"], "error": result["error"], "actions": result.get("raw_actions")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
