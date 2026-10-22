"""Validated author-provided planning boundary for the group-chat ablation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from cooperagents.types import Assignment


@dataclass(frozen=True)
class PlanningMessage:
    sender: str
    addressed_to: str
    content: str


@dataclass(frozen=True)
class InitialPlan:
    pair: str
    source_base_commit: str
    feature_sha256: tuple[tuple[int, str], ...]
    content: str
    history: tuple[PlanningMessage, ...]
    bundle_sha256: str
    worker_summaries: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(cls, path: Path, *, expected_pair: str) -> InitialPlan:
        raw = path.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("synthetic") is not True:
            raise ValueError("Initial plan requires schema_version=1 and synthetic=true")
        if value.get("pair") != expected_pair:
            raise ValueError("Initial plan pair does not match the selected task")
        base = value.get("source_base_commit")
        if not isinstance(base, str) or re.fullmatch(r"[0-9a-f]{40}", base) is None:
            raise ValueError("Initial plan requires a pinned source_base_commit")
        content = value.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 8000:
            raise ValueError("Initial plan content must contain 1-8000 characters")
        features = [int(f) for f in expected_pair.split(":")[2].split(",")]
        hashes = value.get("feature_sha256")
        if not isinstance(hashes, dict) or set(hashes) != {str(f) for f in features}:
            raise ValueError("Initial plan must pin both selected feature texts")
        if any(not isinstance(h, str) or re.fullmatch(r"[0-9a-f]{64}", h) is None for h in hashes.values()):
            raise ValueError("Invalid initial plan feature checksum")
        records = value.get("history")
        if not isinstance(records, list) or not records:
            raise ValueError("Initial plan requires a synthetic public history")
        history = []
        for record in records:
            if not isinstance(record, dict) or set(record) != {"sender", "addressed_to", "content"}:
                raise ValueError("Invalid initial plan public message")
            sender, recipient, text = record["sender"], record["addressed_to"], record["content"]
            if sender not in ("agent1", "agent2", "coordinator") or recipient not in ("agent1", "agent2", "coordinator", "group"):
                raise ValueError("Initial plan history contains an unknown participant")
            if not isinstance(text, str) or not text.strip() or len(text) > 1200:
                raise ValueError("Initial plan messages must be nonempty and fit the public-message limit")
            history.append(PlanningMessage(sender, recipient, text))
        if {m.sender for m in history} != {"agent1", "agent2", "coordinator"}:
            raise ValueError("Initial plan history must include all three participants")
        if sum(len(f"[group #{i}; {m.sender} -> {m.addressed_to}] {m.content}") + 1 for i, m in enumerate(history, 1)) > 8000:
            raise ValueError("Initial plan history must fit one canonical public-chat page")
        summaries = value.get("worker_summaries", {})
        if not isinstance(summaries, dict) or (summaries and set(summaries) != {"agent1", "agent2"}):
            raise ValueError("Initial plan worker summaries must cover both workers")
        if any(not isinstance(s, str) or not s.strip() or len(s) > 8000 for s in summaries.values()):
            raise ValueError("Initial plan worker summaries must contain 1-8000 characters")
        return cls(
            expected_pair, base, tuple((f, hashes[str(f)]) for f in features), content, tuple(history),
            hashlib.sha256(raw).hexdigest(), tuple(sorted(summaries.items())),
        )

    def validate_assignments(self, repo: str, task_id: int, assignments: list[Assignment]) -> None:
        features = [a.feature_id for a in assignments]
        expected = f"{repo}:{task_id}:{','.join(map(str, features))}"
        if self.pair != expected or [a.agent_id for a in assignments] != ["agent1", "agent2"]:
            raise ValueError("Initial plan roster/features do not match runtime assignments")
        for assignment, (feature, digest) in zip(assignments, self.feature_sha256, strict=True):
            if assignment.feature_id != feature or hashlib.sha256(assignment.task.encode()).hexdigest() != digest:
                raise ValueError("Initial plan was authored for a different feature specification")


def initial_plan_path(directory: Path, pair: str) -> Path:
    repo, task, features = pair.split(":")
    if re.fullmatch(r"[a-zA-Z0-9_]+", repo) is None or not task.isdigit():
        raise ValueError("Invalid initial plan task identity")
    ids = list(map(int, features.split(",")))
    if len(ids) != 2 or ids != sorted(set(ids)) or min(ids) < 1:
        raise ValueError("Initial plan requires two sorted distinct positive feature IDs")
    return directory / f"{repo}-{task}-f{ids[0]}_f{ids[1]}.json"
