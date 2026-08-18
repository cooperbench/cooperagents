"""Evaluation via CooperBench (used only as a task source + evaluator)."""

from __future__ import annotations

from cooperagents.eval.cooperbench import build_eval_command, run_eval, write_run_outputs
from cooperagents.eval.dataset import WorkItem, image_name, load_subset, read_feature

__all__ = [
    "WorkItem",
    "load_subset",
    "read_feature",
    "image_name",
    "write_run_outputs",
    "build_eval_command",
    "run_eval",
]
