"""Verify every expected pair and replayed final agent history; write an integrity manifest."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from cooperagents.trajectory import open_events, replay


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
        with open_events(path) as handle:
            for line in handle:
                row = json.loads(line)
                counts[f"{row['actor']}:{row['event']}"] += 1
                if row["event"] == "agent_end":
                    ends[row["actor"]] = row["data"]
        if metadata.get("coordinator"):
            if counts["coordinator:coordinator_start"] != 1:
                raise ValueError(f"{pair}: missing coordinator journal")
            if counts["coordinator:nudge"] != len(result.get("metrics", {}).get("coordinator_events", [])):
                raise ValueError(f"{pair}: coordinator nudge count mismatch")
        workers = {"agent1", "agent2"}
        if metadata.get("checkpoint_repair"):
            config = json.loads((directory / "checkpoints/run.json").read_text())
            workers = {assignment["agent_id"] for assignment in config["assignments"]}
        if not workers <= ends.keys() or not workers <= result["agents"].keys():
            raise ValueError(f"{pair}: missing workers")
        for actor, info in result["agents"].items():
            final = json.loads((directory / f"{actor}_traj.json").read_text())
            if state["contexts"].get(actor) != final["messages"]:
                raise ValueError(f"{pair}/{actor}: replay differs from exported final history")
            if ends[actor]["steps"] != info["steps"] or ends[actor]["status"] != info["status"]:
                raise ValueError(f"{pair}/{actor}: final status or step mismatch")
            if counts[f"{actor}:request"] == 0:
                raise ValueError(f"{pair}/{actor}: missing requests")
        checkpoints = {}
        if metadata.get("checkpoint_repair"):
            from cooperagents.checkpoint import verify_checkpoint

            root = directory / "checkpoints"
            expected = {"pre-repair", "post-repair", *(f"worker-{actor}" for actor in workers)}
            expected.update(f"before-{actor}" for actor in result["agents"] if actor.startswith("integrator"))
            for name in sorted(expected):
                saved = verify_checkpoint(root / name)
                checkpoints[name] = saved["metadata"]["boundary"]
                if name.startswith("worker-"):
                    actor = name.removeprefix("worker-")
                    snapshot = saved["metadata"]["result"]
                    final = json.loads((directory / f"{actor}_traj.json").read_text())
                    if (snapshot["messages"] != final["messages"] or snapshot["status"] != ends[actor]["status"]
                            or snapshot["steps"] != ends[actor]["steps"]):
                        raise ValueError(f"{pair}/{actor}: checkpoint worker state mismatch")
            if (root / "post-repair/submission.patch").read_text() != (directory / "integrated.patch").read_text():
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
