"""Strict six-arm summaries from explicit campaign/run paths; never infer missing verdicts."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from cooperagents.eval.dataset import WorkItem
from cooperagents.eval.scorecard import PairScore, Scorecard

VARIANTS = ("auto-prompt", "human-prompt")
MODES = ("direct", "group-chat", "group-chat-planning")
ARMS = {f"{variant}-{mode}": (variant, mode) for variant in VARIANTS for mode in MODES}


def pair_identity(pair: str) -> tuple[str, int, tuple[int, int]]:
    repo, task, features = pair.split(":")
    fids = tuple(sorted(map(int, features.split(","))))
    if not repo or len(fids) != 2 or len(set(fids)) != 2:
        raise ValueError("Pair must contain two distinct features")
    return repo, int(task), fids


def read(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected object")
    return data


def summarize_campaign(path: Path) -> dict:
    campaign = read(path)
    pairs = campaign["pairs"]
    if not pairs or len(pairs) != len({pair_identity(pair) for pair in pairs}):
        raise ValueError("Campaign pairs must be nonempty and unique")
    pairs_file = Path(campaign["pairs_file"])
    if not pairs_file.is_absolute():
        pairs_file = path.parent / pairs_file
    if hashlib.sha256(pairs_file.read_bytes()).hexdigest() != campaign["pairs_sha256"]:
        raise ValueError("Pairs hash mismatch")
    manifest_pairs = [line.strip() for line in pairs_file.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if manifest_pairs != pairs:
        raise ValueError("Pairs manifest differs from campaign")
    arms = {arm["name"]: (arm["coordination_variant"], arm["coordination_mode"]) for arm in campaign["arms"]}
    if len(campaign["arms"]) != 6 or arms != ARMS:
        raise ValueError("Campaign must contain exactly the six supported arms")
    runs = campaign["runs"]
    expected = {(arm, round_) for arm in ARMS for round_ in (1, 2, 3)}
    keys = [(run["arm"], run["round"]) for run in runs]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("Campaign must contain one run per arm/round")
    frozen = campaign["frozen"]
    required = {
        "git_commit",
        "cooperbench_commit",
        "step_limit",
        "agent_time_limit",
        "planning_step_limit",
        "planning_time_limit",
        "concurrency",
        "eval_concurrency",
        "runtime_config",
        "images_sha256",
        "evaluator_patches",
    }
    if set(frozen) != required or any(value is None for value in frozen.values()):
        raise ValueError("Freeze exact code, model/service, requests, budgets, images and evaluator patches before reporting")
    runtime = frozen["runtime_config"]
    if any(runtime.get(key) in (None, "") for key in ("model", "model_revision", "service_revision")):
        raise ValueError("Model/service revisions are required")
    cards, outcomes, issues, verdicts, per_round = {}, {}, [], {}, []
    costs = {
        arm: dict(
            worker_cost_recorded=0.0,
            worker_cost_known=True,
            coordinator_calls=0,
            coordinator_tokens=Counter(),
            coordinator_usage_known=True,
            coordinator_calls_known=True,
        )
        for arm in ARMS
    }
    seen_dirs = set()
    for entry in runs:
        arm, round_ = entry["arm"], entry["round"]
        directory = Path(entry["run_dir"]) if entry.get("run_dir") else None
        if directory is None:
            issues.append(dict(arm=arm, round=round_, reason="missing run_dir"))
            continue
        if not directory.is_absolute():
            directory = path.parent / directory
        directory = directory.resolve()
        if directory in seen_dirs:
            raise ValueError("Run directory reused across arms/rounds")
        seen_dirs.add(directory)
        try:
            metadata = read(directory / "metadata.json")
            if metadata.get("round") != round_ or (metadata.get("coordination_variant"), metadata.get("coordination_mode")) != arms[arm]:
                raise ValueError("Run arm/round mismatch")
            if metadata.get("pairs") != pairs:
                raise ValueError("Run pairs mismatch")
            if entry.get("job_id") != (directory / "job-id.txt").read_text().strip():
                raise ValueError("Job identity mismatch")
            for key in required - {"runtime_config", "images_sha256", "evaluator_patches"}:
                if metadata.get(key) != frozen[key]:
                    raise ValueError(f"Mixed run configuration: {key}")
            if not metadata.get("official_evaluation") or not metadata.get("record_trajectories"):
                raise ValueError("Official scoring and trajectories are required")
            if metadata.get("repair_integrator") or not metadata.get("coordinator") or not metadata.get("coordinator_notebook"):
                raise ValueError("Campaign requires coordinator/notebook and no repair")
            if read(directory / "rendered-config/runtime-config.json") != runtime:
                raise ValueError("Mixed model/service/request configuration")
            if read(directory / "data/images.sha256.json") != frozen["images_sha256"]:
                raise ValueError("Mixed qualified task images")
            if (directory / "data/evaluator-patch.sha256").read_text() != frozen["evaluator_patches"]:
                raise ValueError("Mixed evaluator patches")
            receipt = read(directory / "trajectory-audit.json")
            if receipt.get("complete") is not True or receipt.get("pairs") != len(pairs):
                raise ValueError("Incomplete trajectory audit")
            audit_entries = receipt.get("entries", [])
            if len(audit_entries) != len(pairs) or {pair_identity(e["pair"]) for e in audit_entries} != {pair_identity(p) for p in pairs}:
                raise ValueError("Audit receipt pair identities mismatch")
            for audit_entry in audit_entries:
                info = audit_entry.get("coordination", {})
                calls = info.get("coordinator_calls")
                if calls is None:
                    costs[arm]["coordinator_calls_known"] = False
                else:
                    costs[arm]["coordinator_calls"] += calls
                tokens = info.get("coordinator_tokens")
                if tokens is None:
                    costs[arm]["coordinator_usage_known"] = False
                else:
                    costs[arm]["coordinator_tokens"].update(tokens)
            if len(receipt.get("entries", [])) != len(pairs):
                costs[arm]["coordinator_usage_known"] = False
        except FileNotFoundError as exc:
            issues.append(dict(arm=arm, round=round_, reason=f"missing {exc.filename}"))
            continue
        card = cards.setdefault(arm, Scorecard(label=arm, setting="team"))
        outcome_counts = outcomes.setdefault(arm, Counter())
        round_passes = 0
        for pair in pairs:
            repo, task, features = pair.split(":")
            fids = sorted(map(int, features.split(",")))
            if len(fids) != 2 or len(set(fids)) != 2:
                raise ValueError("Pair must contain two distinct features")
            pair_dir = directory / "logs/real/team" / repo / task / "_".join(f"f{fid}" for fid in fids)
            try:
                result, evaluation = read(pair_dir / "result.json"), read(pair_dir / "eval.json")
                if not (pair_dir / "trajectory.jsonl.gz").is_file():
                    raise FileNotFoundError(str(pair_dir / "trajectory.jsonl.gz"))
            except FileNotFoundError as exc:
                issues.append(dict(arm=arm, round=round_, pair=pair, reason=f"missing {exc.filename or str(exc)}"))
                continue
            receipt_entry = next(e for e in audit_entries if pair_identity(e["pair"]) == pair_identity(pair))
            actual_sha = hashlib.sha256((pair_dir / "trajectory.jsonl.gz").read_bytes()).hexdigest()
            if actual_sha != receipt_entry["sha256"]:
                raise ValueError("Trajectory differs from audit receipt")
            if result.get("model") != runtime["model"]:
                raise ValueError("Result model mismatch")
            if (result.get("repo"), result.get("task_id"), result.get("features")) != (repo, int(task), fids):
                raise ValueError("Result pair identity mismatch")
            if evaluation.get("error") or evaluation.get("infrastructure_error"):
                issues.append(dict(arm=arm, round=round_, pair=pair, reason="evaluator infrastructure error"))
                continue
            for key, value in [
                ("both_passed", evaluation.get("both_passed")),
                ("feature1", evaluation.get("feature1", {}).get("passed")),
                ("feature2", evaluation.get("feature2", {}).get("passed")),
            ]:
                if type(value) is not bool:
                    raise ValueError(f"Official {key} verdict must be boolean")
            if evaluation["both_passed"] != (evaluation["feature1"]["passed"] and evaluation["feature2"]["passed"]):
                raise ValueError("Inconsistent official pair and feature verdicts")
            # Official evaluator versions may omit identity fields; validate every supplied one.
            for key, value in [("repo", repo), ("task_id", int(task)), ("features", fids)]:
                if key in evaluation and evaluation[key] != value:
                    raise ValueError("Evaluator pair identity mismatch")
            worker_cost = result.get("total_cost")
            if worker_cost is None or worker_cost <= 0:
                costs[arm]["worker_cost_known"] = False
            costs[arm]["worker_cost_recorded"] += worker_cost or 0
            metrics = result.get("metrics", {})
            if metrics.get("coordination_mode") != arms[arm][1]:
                raise ValueError("Result coordination mode mismatch")
            planning = metrics.get("planning", {})
            if arms[arm][1] == "group-chat-planning":
                outcome = planning.get("outcome")
                if outcome not in ("agreed", "forced", "error"):
                    raise ValueError("Missing planning outcome")
                outcome_counts[outcome] += 1
            passed = evaluation["both_passed"]
            card.pairs.append(
                PairScore(
                    item=WorkItem(repo=repo, task_id=int(task), features=fids),
                    passed=passed,
                    features_passed=int(evaluation["feature1"]["passed"]) + int(evaluation["feature2"]["passed"]),
                    duration=float(result["duration_seconds"]),
                    steps=int(result["total_steps"]),
                )
            )
            verdicts[arm, round_, pair] = passed
            round_passes += passed
        per_round.append(
            dict(
                arm=arm,
                round=round_,
                pair_passes=round_passes,
                coverage=sum((arm, round_, pair) in verdicts for pair in pairs),
                expected=len(pairs),
            )
        )
    complete = not issues and len(verdicts) == 18 * len(pairs)
    comparisons = []
    comparisons_to_make = [
        (f"{variant}-{a}", f"{variant}-{b}")
        for variant in VARIANTS
        for a, b in [("direct", "group-chat"), ("group-chat", "group-chat-planning")]
    ]
    comparisons_to_make += [(f"auto-prompt-{mode}", f"human-prompt-{mode}") for mode in MODES]
    for left, right in comparisons_to_make:
        paired = Counter()
        for round_ in (1, 2, 3):
            for pair in pairs:
                if (left, round_, pair) in verdicts and (right, round_, pair) in verdicts:
                    a, b = verdicts[left, round_, pair], verdicts[right, round_, pair]
                    paired["both_pass" if a and b else "both_fail" if not a and not b else "left_only" if a else "right_only"] += 1
        n = sum(paired.values())
        comparisons.append(
            dict(
                left=left,
                right=right,
                coverage=n,
                expected=3 * len(pairs),
                counts=dict(paired),
                delta_percentage_points=100 * (paired["right_only"] - paired["left_only"]) / n if complete else None,
            )
        )
    return dict(
        complete=complete,
        expected_pair_runs_per_arm=3 * len(pairs),
        issues=issues,
        arms=[
            dict(
                arm=arm,
                coverage=cards.get(arm, Scorecard(arm, "team")).n,
                expected=3 * len(pairs),
                pair_passes=cards.get(arm, Scorecard(arm, "team")).passed,
                pair_rate=cards[arm].pass_rate if complete else None,
                feature_passes=sum(p.features_passed for p in cards.get(arm, Scorecard(arm, "team")).pairs),
                feature_rate=cards[arm].feature_rate if complete else None,
                avg_steps=cards[arm].avg_steps if complete else None,
                avg_duration_seconds=cards[arm].avg_duration if complete else None,
                planning_outcomes=dict(outcomes.get(arm, {})),
                worker_cost_recorded=costs[arm]["worker_cost_recorded"],
                worker_cost_status="recorded_estimate" if costs[arm]["worker_cost_known"] else "unknown; zero is not free",
                coordinator_calls=costs[arm]["coordinator_calls"] if costs[arm]["coordinator_calls_known"] else None,
                coordinator_tokens=dict(costs[arm]["coordinator_tokens"]) if costs[arm]["coordinator_usage_known"] else None,
            )
            for arm in ARMS
        ],
        rounds=per_round,
        paired_comparisons=comparisons,
    )


def markdown(summary: dict) -> str:
    lines = [
        "Complete official results" if summary["complete"] else "INCOMPLETE: rates withheld; coverage and missing evidence below.",
        "",
        "| Arm | Coverage | Pair passes | Feature passes | Pair rate | Planning outcomes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for arm in summary["arms"]:
        rate = f"{arm['pair_rate']:.1%}" if arm["pair_rate"] is not None else "withheld"
        lines.append(
            f"| {arm['arm']} | {arm['coverage']}/{arm['expected']} | {arm['pair_passes']} | "
            f"{arm['feature_passes']} | {rate} | {arm['planning_outcomes']} |"
        )
    if summary["issues"]:
        lines += ["", "Missing/infrastructure evidence:", *[f"- {issue}" for issue in summary["issues"]]]
    lines += [
        "",
        "Costs (worker costs are recorded estimates; unknown/zero values do not imply free service):",
        *[
            f"- {arm['arm']}: steps={arm['avg_steps']}; seconds={arm['avg_duration_seconds']}; "
            f"worker cost={arm['worker_cost_recorded']} ({arm['worker_cost_status']}); "
            f"coordinator calls={arm['coordinator_calls']}; tokens={arm['coordinator_tokens']}"
            for arm in summary["arms"]
        ],
    ]
    lines += [
        "",
        "Paired comparisons (right minus left; incomplete deltas withheld):",
        *[f"- {item}" for item in summary["paired_comparisons"]],
        "",
        "Per-round results:",
        *[f"- {item}" for item in summary["rounds"]],
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = summarize_campaign(args.campaign)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.output / "summary.md").write_text(markdown(summary))
    print("complete" if summary["complete"] else "incomplete; official rates withheld")


if __name__ == "__main__":
    main()
