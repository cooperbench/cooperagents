"""Stat-cache refresh with an explicit repair-state invariance guard."""

from cooperagents.checkpoint import complete_output, preview_patch
from cooperagents.env.base import Environment


def _snapshot(env: Environment) -> tuple[str, str, str]:
    return (
        complete_output(env.execute("GIT_OPTIONAL_LOCKS=0 git status --porcelain=v1 --untracked-files=all"), "Index status"),
        complete_output(env.execute("git ls-files --stage -z"), "Index entries"),
        preview_patch(env),
    )


def refresh_index(env: Environment) -> dict:
    before = _snapshot(env)
    result = env.execute("git update-index -q --refresh")
    if result.exit_code not in (0, 1):
        raise ValueError("Git index refresh failed")
    complete_output(result, "Git index refresh", allow_failure=True)
    if before != _snapshot(env):
        raise ValueError("Index refresh changed status, staged entries or patch")
    return dict(exit_code=result.exit_code, status_unchanged=True, entries_unchanged=True, patch_unchanged=True)
