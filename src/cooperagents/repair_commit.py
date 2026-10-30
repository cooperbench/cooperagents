"""Explicit, opt-in difficulty instruction and read-only commit evidence."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cooperagents.checkpoint import complete_output

if TYPE_CHECKING:
    from cooperagents.env.base import Environment, ExecResult
    from cooperagents.trajectory import Trajectory

COMMIT_INSTRUCTION = (
    "Resolve both features, run the prescribed validation and repair gate, then commit "
    "the final patch in this private workspace before normal submission. "
    "Commit success alone does not establish test success."
)


def probe(env: Environment, command: str, trajectory: Trajectory | None = None, *, allow_failure: bool = False) -> ExecResult:
    result = env.execute(command, timeout=30)
    if trajectory is not None:
        trajectory.emit("controller", "repair_commit_probe", command=command, stdout=result.stdout, exit_code=result.exit_code)
    complete_output(result, "Repair commit evidence", allow_failure=allow_failure)
    return result


def head(env: Environment, trajectory: Trajectory | None = None) -> str:
    value = probe(env, "git rev-parse --verify HEAD", trajectory).stdout.strip()
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ValueError("Invalid repair commit HEAD evidence")
    return value


def evidence(env: Environment, initial_head: str, trajectory: Trajectory | None = None) -> dict[str, str | bool]:
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", initial_head) is None:
        raise ValueError("Invalid initial repair HEAD")
    final_head = head(env, trajectory)
    status = probe(env, "git status --porcelain=v1 --untracked-files=all", trajectory)
    ancestor = probe(env, f"git merge-base --is-ancestor {initial_head} HEAD", trajectory, allow_failure=True)
    if ancestor.exit_code not in (0, 1):
        raise RuntimeError("Unable to determine repair commit ancestry")
    return dict(
        initial_head=initial_head,
        final_head=final_head,
        status=status.stdout,
        ancestor=ancestor.exit_code == 0,
        clean=status.stdout == "",
        new_commit=final_head != initial_head,
        committed=final_head != initial_head and ancestor.exit_code == 0 and status.stdout == "",
    )
