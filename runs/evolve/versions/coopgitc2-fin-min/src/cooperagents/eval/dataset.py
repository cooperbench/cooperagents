"""Loading tasks from the CooperBench dataset.

CooperBench is a sibling checkout used **only** as the source of tasks and
the evaluator — we never import or modify its source here.  This module
reads its on-disk dataset directly: subset manifests under
``dataset/subsets/*.json`` and per-feature specs at
``dataset/<repo>/task<id>/feature<n>/feature.md``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorkItem:
    """One evaluable unit: a feature pair on a repo task."""

    repo: str
    task_id: int
    features: list[int]


def find_cooperbench(explicit: str | None = None) -> Path:
    """Locate the CooperBench checkout.

    Order: explicit arg, ``$COOPERBENCH_DIR``, a ``CooperBench`` dir next to
    the cwd, or one beside this package.  Raises if none has a ``dataset/``.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.getenv("COOPERBENCH_DIR"):
        candidates.append(Path(os.environ["COOPERBENCH_DIR"]))
    candidates.append(Path.cwd() / "CooperBench")
    candidates.append(Path(__file__).resolve().parents[3] / "CooperBench")
    for c in candidates:
        if (c / "dataset").is_dir():
            return c
    raise FileNotFoundError("Could not find CooperBench (with a dataset/ dir). Set COOPERBENCH_DIR or pass cooperbench_dir explicitly.")


def dataset_dir(cooperbench_dir: str | Path | None = None) -> Path:
    return find_cooperbench(str(cooperbench_dir) if cooperbench_dir else None) / "dataset"


def load_subset(subset: str = "flash", *, cooperbench_dir: str | Path | None = None) -> list[WorkItem]:
    """Flatten a subset manifest into one :class:`WorkItem` per feature pair."""
    manifest = dataset_dir(cooperbench_dir) / "subsets" / f"{subset}.json"
    data = json.loads(manifest.read_text())
    items: list[WorkItem] = []
    for entry in data["tasks"]:
        for pair in entry["pairs"]:
            items.append(WorkItem(repo=entry["repo"], task_id=int(entry["task_id"]), features=list(pair)))
    return items


def read_feature(repo: str, task_id: int, feature_id: int, *, cooperbench_dir: str | Path | None = None) -> str:
    """Read a feature spec (the task text handed to an agent)."""
    path = dataset_dir(cooperbench_dir) / repo / f"task{task_id}" / f"feature{feature_id}" / "feature.md"
    if not path.is_file():
        raise FileNotFoundError(f"feature spec not found: {path}")
    return path.read_text()


# Mirrors cooperbench.utils.{REGISTRY,IMAGE_PREFIX,get_image_name} so DockerEnv
# pulls the exact same public image the evaluator scores against.
CB_REGISTRY = "akhatua"
CB_IMAGE_PREFIX = "cooperbench"


def image_name(repo: str, task_id: int) -> str:
    """The public Docker image CooperBench uses for a task (for ``DockerEnv``).

    e.g. ``go_chi_task`` / 26 -> ``akhatua/cooperbench-go-chi:task26``.
    """
    repo_clean = repo.replace("_task", "").replace("_", "-")
    return f"{CB_REGISTRY}/{CB_IMAGE_PREFIX}-{repo_clean}:task{task_id}"


__all__ = ["WorkItem", "find_cooperbench", "dataset_dir", "load_subset", "read_feature", "image_name"]
