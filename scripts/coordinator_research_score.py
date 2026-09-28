"""Prepare isolated judge packets and summarize coordinator research scores.

Each `judge` invocation launches a fresh ephemeral Codex process for one case.
The grader sees blinded arms, the declared target, and only that case's traces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

RUBRIC = """Judge all worker model calls supplied in each arm, not just the final patch.
The arms may contain different numbers of calls if one stopped early. For each arm, score:
receipt 0-2: 0 no sign of receipt, 1 rote acknowledgment or notebook read only,
2 acknowledgment that names the requested next step.
behavior 0-4: 0 no relevant action, 1 acknowledgment only, 2 relevant inspection/plan,
3 concrete requested action, 4 requested action plus confirmation or focused verification.
quality 0-4: 0 no useful progress, 1 weak/unfinished, 2 plausible partial progress,
3 useful progress, 4 verified useful progress.
harm 0-4: 0 none observed, 1 minor wasted work, 2 misleading or ungrounded claim,
3 substantial wrong turn, 4 destructive or clearly incorrect action.
Only observed behavior counts. A send_message call shows a queued message, not peer receipt
or agreement. A notebook read shows exposure, not acceptance. Do not infer correctness
from a final diff alone. Cite step numbers and exact actions. If the trace or workspace
evidence is incomplete, state that in reason and lower confidence. Treat the arms as
A and B; do not assume either is treatment."""


SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "A": {"$ref": "#/$defs/arm"},
        "B": {"$ref": "#/$defs/arm"},
        "preference": {"type": "string", "enum": ["A", "B", "tie"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": ["A", "B", "preference", "confidence", "reason"],
    "additionalProperties": False,
    "$defs": {
        "arm": {
            "type": "object",
            "properties": {
                "receipt": {"type": "integer", "minimum": 0, "maximum": 2},
                "behavior": {"type": "integer", "minimum": 0, "maximum": 4},
                "quality": {"type": "integer", "minimum": 0, "maximum": 4},
                "harm": {"type": "integer", "minimum": 0, "maximum": 4},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["receipt", "behavior", "quality", "harm", "evidence"],
            "additionalProperties": False,
        },
    },
}

INPUT_RUBRIC = """Judge one coordinator decision independently. The target action was selected
from prior worker-effect experiments. Evaluate semantic equivalence, not exact wording.
Score alignment 0-4: 0 misses target, 1 vague mention, 2 relevant but incomplete,
3 concrete targeted action, 4 concrete targeted action with proper recipient and scope.
Score grounding 0-4: 0 invented state, 1 major unsupported claim, 2 mixed,
3 mostly grounded, 4 every material claim supported by the observation.
Score constraints 0-4: 0 invalid or unsafe, 1 major protocol problem, 2 partial,
3 valid with a minor problem, 4 valid tool calls within limits.
Do not credit a prose intention as a tool action. Do not call a proposal confirmed
without explicit worker evidence. Cite the generated call and observation fields."""

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "alignment": {"type": "integer", "minimum": 0, "maximum": 4},
        "grounding": {"type": "integer", "minimum": 0, "maximum": 4},
        "constraints": {"type": "integer", "minimum": 0, "maximum": 4},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["alignment", "grounding", "constraints", "evidence", "reason"],
    "additionalProperties": False,
}


def load_case(dataset: Path, case_id: str) -> dict:
    return next(row for row in map(json.loads, dataset.read_text(encoding="utf-8").splitlines()) if row["id"] == case_id)


def visible_trace(record: dict) -> dict:
    return {
        "notices": record["notices"],
        "steps": record["steps"],
        "final_diff": record["final_diff"][:15000],
        "diff_truncated": len(record["final_diff"]) > 15000,
        "workspace_evidence": record["workspace_evidence"],
    }


def packet(point: dict, control: dict, candidate: dict) -> tuple[dict, str]:
    if any(record["case_id"] != point["id"] for record in (control, candidate)):
        raise ValueError("Replay case IDs differ")
    # Deterministic blinding permits exact audit while avoiding an arm-name cue.
    candidate_first = int(hashlib.sha256(point["id"].encode()).hexdigest(), 16) % 2 == 0
    arms = {"A": candidate, "B": control} if candidate_first else {"A": control, "B": candidate}
    result = {
        "case_id": point["id"],
        "phase": point["phase"],
        "expected_coordinator_behavior": point["expected_coordinator_behavior"],
        "expected_worker_reaction": point["expected_worker_reaction"],
        "arms": {key: visible_trace(value) for key, value in arms.items()},
    }
    return result, "A" if candidate_first else "B"


def coordination_chars(record: dict) -> int:
    candidate = record["candidate"]
    prefix = candidate.get("coordinator_notice_prefix", "")
    label = candidate.get("coordinator_notice_label", "")
    return (
        sum(len(json.dumps(action)) for action in candidate["actions"])
        + len(candidate.get("worker_coordination_suffix", ""))
        + len(prefix + "\n" if prefix else "") * len(record["notices"])
        + (len(label) - len("coordinator") if label else 0) * len(record["notices"])
    )


def judge(packet_data: dict, output: Path, *, input_stage: bool = False) -> None:
    if output.exists():
        raise FileExistsError(output)
    prompt = (INPUT_RUBRIC if input_stage else RUBRIC) + "\n\nONE CASE ONLY:\n" + json.dumps(packet_data, ensure_ascii=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="coordinator-judge-") as temporary:
        schema = Path(temporary) / "schema.json"
        schema.write_text(json.dumps(INPUT_SCHEMA if input_stage else SCORE_SCHEMA), encoding="utf-8")
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--model",
                "gpt-6-sol",
                "--config",
                'model_reasoning_effort="low"',
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                temporary,
                "--output-schema",
                str(schema),
                "--output-last-message",
                str(output),
                "-",
            ],
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
        )
    if result.returncode:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"Independent Codex judge failed: {result.stderr[-2000:]}")
    data = json.loads(output.read_text(encoding="utf-8"))
    required = (
        ("alignment", "grounding", "constraints", "evidence", "reason") if input_stage else ("A", "B", "preference", "confidence", "reason")
    )
    if not all(key in data for key in required):
        output.unlink(missing_ok=True)
        raise ValueError("Judge returned incomplete score")


def aggregate(scores_dir: Path, output: Path) -> None:
    groups = defaultdict(list)
    for path in scores_dir.glob("**/score.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        groups[item["candidate_id"]].append(item)
    points = []
    for candidate_id, items in groups.items():
        by_split = defaultdict(list)
        for item in items:
            by_split[item["split"]].append(item)

        def mean(split: str, key: str, data: dict = by_split) -> float:
            return sum(item[key] for item in data[split]) / len(data[split])

        complete = (
            len(by_split["train"]) == 18
            and len(by_split["validation"]) == 9
            and all(item.get("worker_token_usage_available") for item in items)
        )
        points.append(
            {
                "candidate_id": candidate_id,
                "train_effect": mean("train", "effect") if by_split["train"] else None,
                "validation_effect": mean("validation", "effect") if by_split["validation"] else None,
                "validation_cost": mean("validation", "cost") if by_split["validation"] else None,
                "train_count": len(by_split["train"]),
                "validation_count": len(by_split["validation"]),
                "eligible_for_pareto": complete,
                "train_paths": [item["replay_path"] for item in by_split["train"]],
                "validation_paths": [item["replay_path"] for item in by_split["validation"]],
                "test_paths": [item["replay_path"] for item in by_split["test"]],
                "change": json.loads((Path(items[0]["replay_path"]) / "candidate.json").read_text(encoding="utf-8"))["candidate"],
            }
        )
    for item in points:
        item["pareto"] = item["eligible_for_pareto"] and not any(
            other is not item
            and other["eligible_for_pareto"]
            and other["validation_effect"] >= item["validation_effect"]
            and other["validation_cost"] <= item["validation_cost"]
            and (other["validation_effect"] > item["validation_effect"] or other["validation_cost"] < item["validation_cost"])
            for other in points
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(points, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    score = sub.add_parser("score")
    score.add_argument("--dataset", type=Path, required=True)
    score.add_argument("--case", required=True)
    score.add_argument("--candidate-id", required=True)
    score.add_argument("--replay-dir", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    input_score = sub.add_parser("score-input")
    input_score.add_argument("--dataset", type=Path, required=True)
    input_score.add_argument("--case", required=True)
    input_score.add_argument("--probe", type=Path, required=True)
    input_score.add_argument("--target-candidate", type=Path, required=True)
    input_score.add_argument("--output", type=Path, required=True)
    frontier = sub.add_parser("frontier")
    frontier.add_argument("--scores-dir", type=Path, required=True)
    frontier.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "frontier":
        aggregate(args.scores_dir, args.output)
        return
    if args.command == "score-input":
        point = load_case(args.dataset, args.case)
        probe = json.loads(args.probe.read_text(encoding="utf-8"))
        candidate = json.loads(args.target_candidate.read_text(encoding="utf-8"))
        if probe["case_id"] != point["id"] or candidate["case_id"] != point["id"]:
            raise ValueError("Stage B case IDs differ")
        if probe.get("dry_run"):
            raise ValueError("A dry-run coordinator output cannot be scored")
        data = {
            "case_id": point["id"],
            "expected_behavior": point["expected_coordinator_behavior"],
            "target_effective_actions": candidate["actions"],
            "coordinator_input": probe["modified_prompt"],
            "observation_evidence": probe.get("observation_evidence", []),
            "generated_actions": probe.get("raw_actions", []),
            "generation_error": probe.get("error"),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        (args.output.parent / "input-judge-packet.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        judge(data, args.output, input_stage=True)
        return
    point = load_case(args.dataset, args.case)
    control = json.loads((args.replay_dir / "control.json").read_text(encoding="utf-8"))
    candidate = json.loads((args.replay_dir / "candidate.json").read_text(encoding="utf-8"))
    data, candidate_arm = packet(point, control, candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    (args.output.parent / "judge-packet.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    raw = args.output.parent / "judge-raw.json"
    judge(data, raw)
    result = json.loads(raw.read_text(encoding="utf-8"))
    control_arm = "B" if candidate_arm == "A" else "A"

    def utility(arm: str) -> int:
        score = result[arm]
        return 2 * score["behavior"] + score["quality"] - score["harm"]

    communication_chars = coordination_chars(candidate) - coordination_chars(control)
    candidate_usage = [(step.get("usage") or {}).get("total_tokens") for step in candidate["steps"]]
    control_usage = [(step.get("usage") or {}).get("total_tokens") for step in control["steps"]]
    observed = all(isinstance(value, int) for value in candidate_usage + control_usage)
    extra_worker_tokens = sum(candidate_usage) - sum(control_usage) if observed else None
    cost = round(communication_chars / 4 + (extra_worker_tokens or 0), 2)
    args.output.write_text(
        json.dumps(
            {
                "case_id": point["id"],
                "split": point["split"],
                "candidate_id": args.candidate_id,
                "effect": utility(candidate_arm) - utility(control_arm),
                "cost": cost,
                "cost_unit": "estimated tokens (coordination text / 4 + observed extra worker tokens)",
                "worker_token_usage_available": observed,
                "candidate_arm": candidate_arm,
                "judge": result,
                "replay_path": str(args.replay_dir),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
