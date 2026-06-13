"""S7 guardrail: the env adapter blocks destructive git on the shared tree."""

from __future__ import annotations

from cooperagents.env.base import Environment, ExecResult
from cooperagents.workers.mini_swe_worker import MiniSweEnvAdapter


class _RecordingEnv(Environment):
    repo_path = "/workspace/repo"

    def __init__(self):
        self.ran = []

    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        self.ran.append(command)
        return ExecResult(stdout="ok", exit_code=0)

    def read_file(self, path: str) -> str:
        return ""

    def write_file(self, path: str, content: str) -> None: ...
    def git_diff(self) -> str:
        return ""


DESTRUCTIVE = ["git reset --hard", "git checkout -- .", "git clean -fd", "git stash", "rm -rf .git"]
SAFE = ["git diff", "git add -A", "ls", "go build ./...", "git checkout -b feature"]


def test_guard_blocks_destructive():
    env = _RecordingEnv()
    a = MiniSweEnvAdapter(env, guard_git=True)
    for cmd in DESTRUCTIVE:
        out = a.execute({"command": cmd})
        assert out["returncode"] == 1 and "BLOCKED" in out["output"], cmd
    assert env.ran == []  # nothing destructive reached the env


def test_guard_allows_safe():
    env = _RecordingEnv()
    a = MiniSweEnvAdapter(env, guard_git=True)
    for cmd in SAFE:
        out = a.execute({"command": cmd})
        assert out["returncode"] == 0, cmd
    assert len(env.ran) == len(SAFE)


def test_no_guard_passes_through():
    env = _RecordingEnv()
    a = MiniSweEnvAdapter(env, guard_git=False)
    a.execute({"command": "git reset --hard"})
    assert env.ran  # reached the env when guard is off
