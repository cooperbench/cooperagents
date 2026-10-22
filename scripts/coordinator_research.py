"""Build and check a small, source-linked coordinator intervention dataset.

The output is local experiment data, not a package resource. Each point names an
archived model request so a later replay uses the exact worker conversation.
"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Split by repository: no codebase or task description crosses a split.
SPLITS = {
    "train": {"dspy_task", "pallets_click_task", "pallets_jinja_task"},
    "validation": {"dottxt_ai_outlines_task", "go_chi_task", "huggingface_datasets_task"},
    "test": {"llama_index_task", "openai_tiktoken_task", "pillow_task", "samuelcolvin_dirty_equals_task"},
}

# One row per archived pair, in sorted path order within each repository.
PHASES = {
    "dspy_task": "early conflict budget budget",
    "pallets_click_task": "early early conflict conflict budget budget",
    "pallets_jinja_task": "early early early conflict conflict conflict budget budget",
    "dottxt_ai_outlines_task": "early early conflict budget budget",
    "go_chi_task": "early conflict",
    "huggingface_datasets_task": "conflict budget",
    "llama_index_task": "budget",
    "openai_tiktoken_task": "early conflict budget",
    "pillow_task": "early conflict",
    "samuelcolvin_dirty_equals_task": "early conflict budget",
}


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def first_request_after(rows: list[dict], seq: int, target: str) -> dict:
    return next(
        row
        for row in rows
        if row["seq"] > seq and row["actor"] == target and row["event"] == "request" and "messages" in row["data"].get("request", {})
    )


def choose_boundary(rows: list[dict], phase: str) -> tuple[dict, dict, str | None]:
    ends = [row["seq"] for row in rows if row["event"] == "agent_end" and row["actor"] in {"agent1", "agent2"}]
    first_end = min(ends)
    if phase == "early":
        request = next(
            row for row in rows if row["actor"] == "agent1" and row["event"] == "request" and "messages" in row["data"].get("request", {})
        )
        return request, request, None
    if phase == "conflict":
        nudge = next(
            row
            for row in rows
            if row["event"] == "nudge"
            and row["seq"] < first_end
            and row["data"].get("kind", "").startswith("COLLISION:")
            and "patch.txt" not in row["data"]["kind"]
        )
        target = nudge["data"]["target"]
        decision = next(
            row
            for row in reversed(rows[: nudge["seq"] - 1])
            if row["event"] == "decision" and row["data"].get("target") == target and row["data"].get("kind") == nudge["data"]["kind"]
        )
        delivery = next(
            row
            for row in rows
            if row["seq"] > nudge["seq"]
            and row["event"] == "delivery"
            and row["data"].get("target") == target
            and nudge["data"]["text"] in row["data"].get("messages", [])
        )
        return decision, first_request_after(rows, delivery["seq"], target), nudge["data"]["kind"].removeprefix("COLLISION:")
    if phase == "budget":
        first_finished = next(row["actor"] for row in rows if row["seq"] == first_end)
        requests = [
            row
            for row in rows
            if row["actor"] == first_finished
            and row["event"] == "request"
            and "messages" in row["data"].get("request", {})
            and row["seq"] < first_end
        ]
        # The five-call replay limit is the experimental budget; the archive's
        # original 1000-step worker limit is recorded separately, not rewritten.
        request = requests[max(0, len(requests) * 4 // 5 - 1)]
        return request, request, None
    raise ValueError(phase)


def expected(phase: str, target: str, overlap: str | None) -> tuple[str, str]:
    if phase == "early":
        return (
            f"Ask {target} to inspect relevant code and report proposed files, edit regions, "
            "and shared interfaces to the peer before contested edits; do not claim agreement.",
            "Acknowledges the message, inspects code, and sends a concrete scope/interface "
            "proposal to the peer; continues independent exploration while confirmation is pending.",
        )
    if phase == "conflict":
        return (
            f"Tell {target} that both workers touched {overlap}; ask it to inspect the peer diff "
            "and agree on ownership or a shared interface before further disputed edits. "
            "Do not invent agreement.",
            "Acknowledges the warning, inspects the peer's diff, and sends a specific "
            "ownership/interface proposal; avoids further disputed edits until confirmation.",
        )
    return (
        f"Tell {target} the experiment allows five further worker model calls; ask for one "
        "feasible focused check or an exact status and handoff, without starting new feature work.",
        "Acknowledges the five-call limit, runs a relevant focused check or sends a precise "
        "handoff with unverified work identified; does not start a new feature.",
    )


def build(source: Path, output: Path) -> list[dict]:
    points = []
    grouped = defaultdict(list)
    images = json.loads((source.parents[2] / "images.json").read_text(encoding="utf-8"))
    for path in sorted(source.glob("**/trajectory.jsonl.gz")):
        pair = path.parent.relative_to(source).as_posix()
        grouped[pair.split("/")[0]].append(path)
    for repo, paths in sorted(grouped.items()):
        phases = PHASES[repo].split()
        if len(paths) != len(phases):
            raise ValueError(f"Archived pair count changed for {repo}: {len(paths)} != {len(phases)}")
        split = next(name for name, repos in SPLITS.items() if repo in repos)
        for path, phase in zip(paths, phases, strict=True):
            rows = read_rows(path)
            boundary, worker_request, overlap = choose_boundary(rows, phase)
            target = worker_request["actor"]
            starts = {row["actor"]: row for row in rows if row["event"] == "agent_start" and row["actor"] in {"agent1", "agent2"}}
            behavior, reaction = expected(phase, target, overlap)
            pair = path.parent.relative_to(source).as_posix()
            image = next(
                name
                for name in images
                if name.endswith(f":task{pair.split('/')[1]}") and repo.replace("_task", "").replace("_", "-") in name
            )
            starts_at = datetime.fromisoformat(starts[target]["time"])
            elapsed = (datetime.fromisoformat(worker_request["time"]) - starts_at).total_seconds()
            dirty = next(
                (
                    row["data"]["output"]
                    for row in reversed(rows[: worker_request["seq"] - 1])
                    if row["event"] == "dirty_files" and row["data"].get("target") == target
                ),
                None,
            )
            points.append(
                {
                    "id": pair.replace("/", "__") + "__" + phase,
                    "split": split,
                    "phase": phase,
                    "repo": repo,
                    "image": image,
                    "pair": pair,
                    "trajectory": str(path.resolve()),
                    "boundary_seq": boundary["seq"],
                    "worker_request_seq": worker_request["seq"],
                    "target": target,
                    "feature_id": starts[target]["data"]["feature_id"],
                    "overlap_files": overlap.split(",") if overlap else [],
                    "archive_dirty_files": dirty.splitlines() if dirty is not None else [],
                    "archive_step_limit": starts[target]["data"].get("step_limit"),
                    "archive_time_remaining_s": round((starts[target]["data"].get("time_limit_s") or 0) - elapsed),
                    "replay_model_calls": 5,
                    "expected_coordinator_behavior": behavior,
                    "expected_worker_reaction": reaction,
                    "workspace_status": "base_image" if phase == "early" else "needs_reconstruction",
                }
            )
    validate(points)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in points), encoding="utf-8")
    return points


def validate(points: list[dict]) -> None:
    counts = Counter(point["split"] for point in points)
    if counts != {"train": 18, "validation": 9, "test": 9}:
        raise ValueError(f"Unexpected split counts: {counts}")
    for split in SPLITS:
        phases = Counter(point["phase"] for point in points if point["split"] == split)
        expected_count = counts[split] // 3
        if phases != {"early": expected_count, "conflict": expected_count, "budget": expected_count}:
            raise ValueError(f"Unbalanced phases in {split}: {phases}")
    ids = [point["id"] for point in points]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case id")
    repos = defaultdict(set)
    for point in points:
        repos[point["repo"]].add(point["split"])
        if not point["expected_coordinator_behavior"] or not point["expected_worker_reaction"]:
            raise ValueError(f"Missing declared target: {point['id']}")
        rows = read_rows(Path(point["trajectory"]))
        request = rows[point["worker_request_seq"] - 1]
        if request["seq"] != point["worker_request_seq"] or request["actor"] != point["target"] or request["event"] != "request":
            raise ValueError(f"Invalid worker boundary: {point['id']}")
    if any(len(splits) != 1 for splits in repos.values()):
        raise ValueError("Repository crosses split")


def draft_candidates(points: list[dict], output: Path) -> None:
    acknowledgment = (
        "When a coordinator notice arrives, promptly send a brief acknowledgment to the coordinator "
        "that names its request. Then carry out the requested action. An acknowledgment alone is not completion."
    )
    for point in points:
        if point["split"] != "train":
            continue
        target = point["target"]
        peer = "agent2" if target == "agent1" else "agent1"
        if point["phase"] == "early":
            message = (
                f"Inspect relevant code, then tell {peer} and coordinator your proposed files, edit regions "
                "and shared interfaces. Confirm contested ownership before editing those regions."
            )
        elif point["phase"] == "conflict":
            files = ", ".join(point["overlap_files"])
            message = (
                f"Both workers changed {files}. Inspect the peer's branch diff there and send {peer} "
                "a concrete ownership or interface proposal before further disputed edits."
            )
        else:
            message = (
                "This replay allows five more model calls. Run one feasible focused check, or send coordinator "
                "an exact status and handoff with unverified work named. Avoid starting new feature work."
            )
        for name, suffix in (("static-v1", ""), ("ack-v1", acknowledgment)):
            path = output / name / f"{point['id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "case_id": point["id"],
                        "actions": [{"name": "send_message", "arguments": {"recipient": target, "content": message}}],
                        "worker_coordination_suffix": suffix,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "validate", "draft"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--candidates-dir", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        if args.source is None:
            parser.error("build requires --source")
        points = build(args.source, args.dataset)
    else:
        points = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines()]
        validate(points)
        if args.command == "draft":
            if args.candidates_dir is None:
                parser.error("draft requires --candidates-dir")
            draft_candidates(points, args.candidates_dir)
    print(
        json.dumps(
            {"count": len(points), "splits": dict(Counter(p["split"] for p in points)), "phases": dict(Counter(p["phase"] for p in points))}
        )
    )


if __name__ == "__main__":
    main()
