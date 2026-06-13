"""Local execution environment: a git working tree on the host.

Each agent gets its own private checkout (a ``git clone`` of a seed repo,
or a fresh ``git init``), so concurrent agents never clobber one another.
``git_diff`` reports the change against the commit the checkout started at —
the exact thing CooperBench scores.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from cooperagents.env.base import Environment, ExecResult


class LocalEnv(Environment):
    def __init__(self, repo_path: str, base_commit: str | None = None) -> None:
        self.repo_path = repo_path
        self._base_commit = base_commit or self._current_commit()
        self._owns_dir = False

    @classmethod
    def clone(cls, source_repo: str, *, workdir: str | None = None) -> LocalEnv:
        """Clone ``source_repo`` into a fresh temp dir and return an env on it."""
        dest = tempfile.mkdtemp(prefix="ca-env-", dir=workdir)
        repo = os.path.join(dest, "repo")
        subprocess.run(["git", "clone", "--quiet", source_repo, repo], check=True)
        env = cls(repo)
        env._owns_dir = True
        return env

    @classmethod
    def fresh(cls, *, workdir: str | None = None) -> LocalEnv:
        """Create an empty, git-initialized working tree (handy for tests)."""
        dest = tempfile.mkdtemp(prefix="ca-env-", dir=workdir)
        repo = os.path.join(dest, "repo")
        os.makedirs(repo)
        for cmd in (
            ["git", "init", "--quiet"],
            ["git", "config", "user.email", "agent@cooperagents.local"],
            ["git", "config", "user.name", "cooperagent"],
            ["git", "commit", "--allow-empty", "--quiet", "-m", "base"],
        ):
            subprocess.run(cmd, cwd=repo, check=True)
        env = cls(repo)
        env._owns_dir = True
        return env

    def _current_commit(self) -> str:
        try:
            out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_path, capture_output=True, text=True, check=True)
            return out.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        try:
            proc = subprocess.run(
                command,
                cwd=self.repo_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecResult(stdout=(proc.stdout or "") + (proc.stderr or ""), exit_code=proc.returncode)
        except subprocess.TimeoutExpired:
            return ExecResult(stdout=f"[timed out after {timeout}s]", exit_code=124)

    def read_file(self, path: str) -> str:
        full = os.path.join(self.repo_path, path)
        if not os.path.isfile(full):
            return ""
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read()

    def write_file(self, path: str, content: str) -> None:
        full = os.path.join(self.repo_path, path)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def git_diff(self) -> str:
        # Stage everything (including new files) so the diff is complete,
        # then diff the index against the base commit.
        subprocess.run(["git", "add", "-A"], cwd=self.repo_path, check=False)
        base = self._base_commit or "HEAD"
        proc = subprocess.run(
            ["git", "diff", "--cached", base] if self._base_commit else ["git", "diff", "--cached"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout

    def cleanup(self) -> None:
        if self._owns_dir:
            shutil.rmtree(os.path.dirname(self.repo_path), ignore_errors=True)
