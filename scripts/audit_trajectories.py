"""Verify every expected pair and replayed final agent history; write an integrity manifest."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from cooperagents.initial_plan import InitialPlan
from cooperagents.trajectory import open_events, replay


def audit_worker_summaries(rows: list[dict], plan: InitialPlan) -> None:
    """Check the immutable memory, its recipient and first native model input."""
    deliveries = [r for r in rows if r["event"] == "worker_summary_delivery"]
    expected = dict(plan.worker_summaries)
    if len(deliveries) != len(expected) or {r["data"]["target"] for r in deliveries} != set(expected):
        raise ValueError("Worker summary delivery roster differs from the approved bundle")
    for row in deliveries:
        aid, content = row["data"]["target"], row["data"]["content"]
        first = next(r for r in rows if r["actor"] == aid and r["event"] == "request")
        if content != expected[aid] or row["seq"] >= first["seq"] or not row["data"].get("synthetic"):
            raise ValueError("Worker summary differs from the approved bundle or was delivered too late")
        messages = first["data"]["request"]["messages"]
        if sum(m.get("role") == "assistant" and m.get("content") == content for m in messages) != 1:
            raise ValueError("Worker summary is missing from its first native model input")


def audit_coordination(rows: list[dict], result: dict) -> dict:
    """Verify host protocol, separately from human review of code-edit behavior."""
    mode = result.get("metrics", {}).get("coordination_mode", "direct")
    injected = result.get("metrics", {}).get("planning", {}).get("outcome") == "injected"
    has_plan = mode == "group-chat-planning" or injected
    injections = []
    messages, deliveries, requests, finalizing, ended, failed = {}, {}, {}, [], [], []
    pending_public = {}
    requested_public = Counter()
    confirmations = {}
    group_deliveries = Counter()
    worker_requests = {}
    coordinator_public = {}
    coordinator_sdk_inputs = 0
    coordinator_observation_inputs = 0
    actual_implementation_requests = Counter()
    for row in rows:
        event, data = row["event"], row["data"]
        if event == "observation" and row["actor"] == "coordinator":
            coordinator_public = {record["message_id"]: record for record in data.get("group_chat", {}).get("unread", [])}
            coordinator_observation_inputs += 1
        elif event == "request" and row["actor"] == "coordinator" and "messages" in data.get("request", {}):
            prompt = next(message["content"] for message in data["request"]["messages"] if message["role"] == "user")
            observation = json.loads(prompt.split("OBSERVATION:\n", 1)[1])
            records = observation.get("group_chat", {}).get("unread", [])
            coordinator_public = {record["message_id"]: record for record in records}
            coordinator_sdk_inputs += 1
            public = records + observation.get("group_chat", {}).get("recent", [])
            # Historical forced requests used a separate frozen_evidence wrapper.
            public += observation.get("frozen_evidence", {}).get("group_chat", [])
            for record in public:
                expected = messages.get(record["message_id"])
                if expected is None or any(record[key] != expected[key] for key in ("sender", "addressed_to", "content")):
                    raise ValueError("Coordinator model input contains changed/noncanonical public records")
        elif event == "initial_plan_injected":
            injections.append(row)
        elif event == "group_message":
            mid = data["message_id"]
            if mid != len(messages) + 1 or data["sender"] not in ("agent1", "agent2", "coordinator"):
                raise ValueError("Noncanonical group message identity/order")
            messages[mid] = data
        elif event == "group_delivery":
            ids = data["message_ids"]
            if ids != sorted(set(ids)) or any(mid not in messages for mid in ids):
                raise ValueError("Group delivery skips canonical identity/order")
            if data["target"] == "coordinator":
                for mid in ids:
                    record = coordinator_public.get(mid)
                    if record is None or any(record[key] != messages[mid][key] for key in ("sender", "addressed_to", "content")):
                        raise ValueError("Coordinator delivery lacks corresponding input records")
            if data["target"] != "coordinator":
                pending_public.setdefault(data["target"], set()).update(ids)
            if not data["restored"]:
                target = data["target"]
                previous = group_deliveries[target]
                if ids != list(range(previous + 1, previous + len(ids) + 1)):
                    raise ValueError("Unread group delivery has a gap or duplicate")
                group_deliveries[target] = ids[-1] if ids else previous
        elif event == "plan_confirmation":
            if data["accepted"]:
                confirmations[data["target"]] = data["version"]
            elif data["reason"] in ("new_discussion", "notebook_revised"):
                confirmations.pop(data["target"], None)
        elif event == "planning_finalizing":
            finalizing.append(row)
        elif event == "planning_end":
            ended.append(row)
            if data["outcome"] == "agreed":
                if confirmations != {"agent1": data["version"], "agent2": data["version"]}:
                    raise ValueError("Agreed plan lacks two valid confirmations")
            elif data["outcome"] == "forced":
                if len(finalizing) != 1 or data["reason"] not in ("step_limit", "time_limit"):
                    raise ValueError("Forced plan lacks a single budget trigger")
                budget = finalizing[0]["data"]["frozen"]["planning"]
                if data["reason"] == "step_limit" and max(budget["planning_steps"].values()) < budget["step_limit"]:
                    raise ValueError("Forced plan step budget was not exhausted")
                if data["reason"] == "time_limit" and budget["seconds_remaining"] > 0:
                    raise ValueError("Forced plan time budget was not exhausted")
            elif data["outcome"] == "injected":
                if not injected or len(injections) != 1 or confirmations or any(worker_requests.values()):
                    raise ValueError("Injected plan lacks an honest pre-worker boundary")
                expected_ids = injections[0]["data"]["history_message_ids"]
                if expected_ids != list(range(1, len(messages) + 1)) or any(not m.get("synthetic") for m in messages.values()):
                    raise ValueError("Injected history is not fully labeled canonical synthetic evidence")
            else:
                raise ValueError("Unknown planning end outcome")
        elif event == "plan_delivery":
            if not ended or data["target"] in deliveries or data["content"] != ended[-1]["data"]["content"]:
                raise ValueError("Final plan delivery identity/content mismatch")
            if data["sha256"] != hashlib.sha256(data["content"].encode()).hexdigest():
                raise ValueError("Final plan hash mismatch")
            deliveries[data["target"]] = row
        elif event == "worker_request":
            worker_requests[data["target"]] = row
            if has_plan and data["phase"] == "implementation":
                if set(deliveries) != {"agent1", "agent2"} or max(d["seq"] for d in deliveries.values()) >= row["seq"]:
                    raise ValueError("Implementation request preceded both final plan deliveries")
                requests[data["target"]] = row
        elif (
            event == "request"
            and row["actor"] in worker_requests
            and "messages" in data.get("request", {})
            and data["request"].get("tools") is not None
        ):
            snapshot = worker_requests[row["actor"]]["data"]
            content = "\n".join(str(m.get("content") or "") for m in data["request"]["messages"])
            for mid in pending_public.pop(row["actor"], set()):
                message = messages[mid]
                canonical = f"[group #{mid}; {message['sender']} -> {message['addressed_to']}] {message['content']}"
                if canonical not in content:
                    raise ValueError("Public message absent or changed in actual model request")
                requested_public[row["actor"]] += 1
            if has_plan and snapshot["phase"] == "implementation":
                expected = ended[-1]["data"]["content"]
                texts = [m.get("content") or "" for m in data["request"]["messages"]]
                if not any(t.startswith("[IMPLEMENTATION MODE]") and expected in t for t in texts):
                    raise ValueError("Actual implementation model request lacks the final notebook")
                actual_implementation_requests[row["actor"]] += 1
        elif event == "planning_failed":
            failed.append(row)
    if has_plan:
        outcome = result["metrics"]["planning"]["outcome"]
        if outcome == "error":
            if not failed or any(data["data"]["phase"] == "implementation" for data in requests.values()):
                raise ValueError("Planning error lacks failure record")
        elif (
            len(ended) != 1
            or ended[0]["data"]["outcome"] != outcome
            or set(deliveries) != {"agent1", "agent2"}
            or set(actual_implementation_requests) != {"agent1", "agent2"}
        ):
            raise ValueError("Incomplete planning protocol evidence")
    coordinator_calls = [
        r for r in rows if r["actor"] == "coordinator" and r["event"] == "request" and "messages" in r["data"].get("request", {})
    ]
    tokens = Counter()
    usage_seen = 0
    call_ids = {r["seq"] for r in coordinator_calls}
    for row in rows:
        if row["event"] == "response" and row["data"].get("call_id") in call_ids:
            usage = row["data"]["response"].get("usage") or {}
            if "prompt_tokens" in usage and "completion_tokens" in usage:
                usage_seen += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                tokens[key] += usage.get(key, 0) or 0
    return dict(
        mode=mode,
        group_messages=len(messages),
        group_delivery_positions=dict(group_deliveries),
        planning_outcome=result.get("metrics", {}).get("planning", {}).get("outcome"),
        initial_plan_bundle_sha256=injections[0]["data"]["bundle_sha256"] if len(injections) == 1 else None,
        final_deliveries=sorted(deliveries),
        public_records_in_requests=dict(requested_public),
        public_records_without_later_request={k: sorted(v) for k, v in pending_public.items()},
        coordinator_calls=result.get("metrics", {}).get("coordinator_calls", len(coordinator_calls)),
        coordinator_transport_attempts=len(coordinator_calls),
        coordinator_input_evidence="sdk_requests"
        if coordinator_sdk_inputs
        else "observation_only"
        if coordinator_observation_inputs
        else "none",
        coordinator_tokens=dict(tokens) if usage_seen == len(coordinator_calls) and usage_seen > 0 else None,
        code_edit_behavior="unknown; requires separate trajectory/patch review",
    )


def audit(run: Path) -> dict:
    metadata = json.loads((run / "metadata.json").read_text())
    pairs = metadata["pairs"]
    if len(set(pairs)) != len(pairs):
        raise ValueError("Duplicate pairs in run metadata")
    entries = []
    for pair in pairs:
        repo, task, features = pair.split(":")
        directory = run / "logs/real/team" / repo / task / "_".join(f"f{f}" for f in sorted(map(int, features.split(","))))
        result = json.loads((directory / "result.json").read_text())
        path = directory / "trajectory.jsonl.gz"
        state = replay(path)
        if state["pending_calls"]:
            raise ValueError(f"{pair}: unfinished calls")
        if state["actors"]["harness"]["last_event"]["event"] != "pair_end":
            raise ValueError(f"{pair}: missing completion marker")
        counts = Counter()
        ends = {}
        rows = []
        with open_events(path) as handle:
            for line in handle:
                row = json.loads(line)
                rows.append(row)
                counts[f"{row['actor']}:{row['event']}"] += 1
                if row["event"] == "agent_end":
                    ends[row["actor"]] = row["data"]
        if metadata.get("coordinator"):
            if counts["coordinator:coordinator_start"] != 1:
                raise ValueError(f"{pair}: missing coordinator journal")
            if metadata.get("coordination_mode", "direct") == "direct" and counts["coordinator:nudge"] != len(
                result.get("metrics", {}).get("coordinator_events", [])
            ):
                raise ValueError(f"{pair}: coordinator nudge count mismatch")
        workers = {"agent1", "agent2"}
        stop_mode = metadata.get("stop_after_repair_checkpoint", False)
        only_first = metadata.get("only_first_repair_checkpoint", False)
        collect_conflicts = metadata.get("collect_source_conflicts", False)
        if collect_conflicts and (not stop_mode or metadata.get("official_evaluation", False)):
            raise ValueError("Source conflict selection is restricted to training collection")
        if only_first and not stop_mode:
            raise ValueError("Only-first repair checkpoint requires stop mode")
        if stop_mode and not metadata.get("checkpoint_repair"):
            raise ValueError("Stop mode requires checkpoint collection")
        if metadata.get("checkpoint_repair"):
            config = json.loads((directory / "checkpoints/run.json").read_text())
            if config.get("stop_after_repair_checkpoint", False) != stop_mode:
                raise ValueError("Run and pair repair stop modes differ")
            if config.get("only_first_repair_checkpoint", False) != only_first:
                raise ValueError("Run and pair checkpoint selections differ")
            if config.get("collect_source_conflicts", False) != collect_conflicts:
                raise ValueError("Run and pair source conflict selections differ")
            workers = {assignment["agent_id"] for assignment in config["assignments"]}
        if not workers <= ends.keys() or not workers <= result["agents"].keys():
            raise ValueError(f"{pair}: missing workers")
        outcome = result.get("metrics", {}).get("repair_checkpoint", {}).get("outcome")
        if stop_mode and outcome not in {"captured", "skipped_healthy"}:
            raise ValueError("Stop mode requires an explicit repair checkpoint outcome")
        if not stop_mode and outcome is not None:
            raise ValueError("Unexpected repair checkpoint outcome outside stop mode")
        captured = outcome == "captured"
        selection = result.get("metrics", {}).get("repair_checkpoint", {}).get("selection")
        selection_rows = [r for r in rows if r["actor"] == "training_selection"]
        if not collect_conflicts and (selection is not None or selection_rows):
            raise ValueError("Unexpected training selection outside opt-in collection")
        if only_first or collect_conflicts:
            health = [r for r in rows if r["actor"] == "merge_gate" and r["event"] == "initial_repair_health"]
            initial_healthy = selection.get("initial_healthy") if collect_conflicts and isinstance(selection, dict) else not captured
            if len(health) != 1 or health[0]["data"].get("healthy") is not initial_healthy:
                raise ValueError("Initial repair health evidence differs from collection outcome")
        if collect_conflicts:
            from cooperagents.env.base import ExecResult
            from cooperagents.training_selection import SOURCE_CONFLICT_COMMAND, select_training_checkpoint

            requests = [r for r in selection_rows if r["event"] == "request"]
            responses = [r for r in selection_rows if r["event"] == "response"]
            decisions = [r for r in selection_rows if r["event"] == "checkpoint_selection"]
            if (len(selection_rows) != 3 or len(requests) != 1 or len(responses) != 1 or len(decisions) != 1
                    or requests[0]["data"]["request"] != {"command": SOURCE_CONFLICT_COMMAND, "timeout": 60}
                    or responses[0]["data"]["call_id"] != requests[0]["seq"]
                    or not health[0]["seq"] < requests[0]["seq"] < responses[0]["seq"] < decisions[0]["seq"]):
                raise ValueError("Missing or invalid source conflict scan evidence")
            expected_selection = select_training_checkpoint(initial_healthy, ExecResult(**responses[0]["data"]["response"]))
            if (selection != expected_selection or decisions[0]["data"] != expected_selection
                    or captured != (expected_selection["reason"] != "healthy")):
                raise ValueError("Training selection differs from source conflict evidence")
        if captured and not (metadata.get("stop_after_repair_checkpoint") and metadata.get("checkpoint_repair")):
            raise ValueError("Unexpected repair capture termination")
        if captured and (result["agents"].get("integrator1", {}).get("status") != "checkpointed"
                         or result["agents"]["integrator1"]["steps"] != 0
                         or any(r["actor"].startswith("integrator") and r["event"] == "request" for r in rows)):
            raise ValueError("Captured handoff must precede all repair inference")
        for actor, info in result["agents"].items():
            final = json.loads((directory / f"{actor}_traj.json").read_text())
            if state["contexts"].get(actor) != final["messages"]:
                raise ValueError(f"{pair}/{actor}: replay differs from exported final history")
            if ends[actor]["steps"] != info["steps"] or ends[actor]["status"] != info["status"]:
                raise ValueError(f"{pair}/{actor}: final status or step mismatch")
            if (counts[f"{actor}:request"] == 0 and not (captured and actor == "integrator1")
                    and not result.get("metrics", {}).get("planning_failed")):
                raise ValueError(f"{pair}/{actor}: missing requests")
        coordination = audit_coordination(rows, result)
        approved_digest = metadata.get("initial_plans", {}).get(pair)
        if approved_digest is not None or coordination["planning_outcome"] == "injected":
            from cooperagents.initial_plan import InitialPlan, initial_plan_path

            plan = InitialPlan.load(initial_plan_path(run / "data/initial-plans", pair), expected_pair=pair)
            if plan.bundle_sha256 != approved_digest or coordination["initial_plan_bundle_sha256"] != approved_digest:
                raise ValueError("Injected plan differs from the immutable run input")
            ending = next(r["data"] for r in rows if r["event"] == "planning_end")
            history = [r["data"] for r in rows if r["event"] == "group_message"][:len(plan.history)]
            if ending["content"] != plan.content or any(
                (r["sender"], r["addressed_to"], r["content"]) != (m.sender, m.addressed_to, m.content)
                for r, m in zip(history, plan.history, strict=True)
            ):
                raise ValueError("Injected notebook/history differs from the approved bundle")
            audit_worker_summaries(rows, plan)
        checkpoints = {}
        if metadata.get("checkpoint_repair"):
            from cooperagents.checkpoint import verify_checkpoint

            root = directory / "checkpoints"
            expected = {"pre-repair", *(f"worker-{actor}" for actor in workers)}
            if not captured:
                expected.add("post-repair")
            expected.update(f"before-{actor}" for actor in result["agents"] if actor.startswith("integrator"))
            if only_first:
                expected = {"before-integrator1"} if captured else set()
                if {p.name for p in root.iterdir() if p.is_dir()} != expected:
                    raise ValueError("Only-first checkpoint inventory contains missing or extra snapshots")
            for name in sorted(expected):
                saved = verify_checkpoint(root / name)
                checkpoints[name] = saved["metadata"]["boundary"]
                if collect_conflicts and name in {"pre-repair", "before-integrator1"}:
                    if saved["metadata"].get("healthy") is not initial_healthy:
                        raise ValueError("Checkpoint changed the original health result")
                    if saved["metadata"].get("gate_checks") != health[0]["data"].get("gate_checks"):
                        raise ValueError("Checkpoint changed the original gate evidence")
                if collect_conflicts and name == "before-integrator1":
                    if saved["metadata"].get("training_selection") != selection:
                        raise ValueError("Checkpoint training selection differs from audited evidence")
                    captures = [r for r in rows if r["actor"] == "integrator1" and r["event"] == "checkpoint_end"]
                    if len(captures) != 1 or decisions[0]["seq"] >= captures[0]["seq"]:
                        raise ValueError("Training selection must precede checkpoint capture")
                if name.startswith("worker-"):
                    actor = name.removeprefix("worker-")
                    snapshot = saved["metadata"]["result"]
                    final = json.loads((directory / f"{actor}_traj.json").read_text())
                    if (
                        snapshot["messages"] != final["messages"]
                        or snapshot["status"] != ends[actor]["status"]
                        or snapshot["steps"] != ends[actor]["steps"]
                    ):
                        raise ValueError(f"{pair}/{actor}: checkpoint worker state mismatch")
            if stop_mode and outcome == "skipped_healthy":
                healthy = (health[0]["data"].get("healthy") if only_first or collect_conflicts
                           else verify_checkpoint(root / "pre-repair")["metadata"].get("healthy"))
                if (healthy is not True
                        or any(a.startswith("integrator") for a in result["agents"])
                        or any(r["actor"].startswith("integrator") for r in rows)
                        or any(root.glob("before-integrator*"))):
                    raise ValueError("Healthy skip has unhealthy state or repair activity")
            final = "before-integrator1" if captured else "post-repair"
            if captured:
                from cooperagents.repair import load_repair_input

                load_repair_input(root / final)
                if (root / "post-repair").exists() or (root / "before-integrator2").exists():
                    raise ValueError("Captured handoff continued past the first checkpoint")
            if not (only_first and not captured):
                if (root / final / "submission.patch").read_text() != (directory / "integrated.patch").read_text():
                    raise ValueError(f"{pair}: checkpoint differs from official submission")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        entries.append(
            dict(
                pair=pair,
                path=str(path.relative_to(run)),
                bytes=path.stat().st_size,
                sha256=digest,
                events=state["seq"],
                counts=dict(counts),
                agents=ends,
                checkpoints=checkpoints,
                coordination=coordination,
                repair_checkpoint=result.get("metrics", {}).get("repair_checkpoint"),
            )
        )
    return dict(
        complete=True,
        pairs=len(entries),
        workers=sum(len([a for a in e["agents"] if a.startswith("agent")]) for e in entries),
        entries=entries,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    result = audit(args.run)
    (args.run / "trajectory-audit.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Verified {result['pairs']} pair journals and {result['workers']} worker histories")


if __name__ == "__main__":
    main()
