"""Docker execution environment built from a CooperBench task image.

Mirrors the model CooperBench's own Python-loop adapters use: a long-lived
container (``sleep``) that the host-side agent loop ``docker exec``s into.
The repo lives at ``/workspace/repo``; ``git_diff`` reports the change vs.
the image's base commit, which is what CooperBench evaluates.

``docker`` is shelled out to (no SDK dependency).  This backend is only
exercised in live runs; the test suite uses :class:`LocalEnv`.
"""

from __future__ import annotations

import subprocess
import uuid

from cooperagents.env.base import Environment, ExecResult

CONTAINER_REPO = "/workspace/repo"


class DockerEnv(Environment):
    def __init__(self, image: str, *, name: str | None = None, keepalive: str = "4h") -> None:
        self.image = image
        self.repo_path = CONTAINER_REPO
        self.name = name or f"ca-{uuid.uuid4().hex[:10]}"
        subprocess.run(
            ["docker", "run", "-d", "--name", self.name, "--entrypoint", "", image, "sleep", keepalive],
            check=True,
            capture_output=True,
        )
        # CooperBench images ship the repo at /workspace/repo already; for any
        # other image, make sure the dir exists so `docker exec -w` succeeds.
        subprocess.run(
            ["docker", "exec", self.name, "bash", "-lc", f"mkdir -p {self.repo_path}"],
            check=False,
            capture_output=True,
        )
        self._base_commit = self._current_commit()

    def _current_commit(self) -> str:
        res = self.execute("git rev-parse HEAD")
        return res.stdout.strip() if res.exit_code == 0 else ""

    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        try:
            proc = subprocess.run(
                ["docker", "exec", "-w", self.repo_path, self.name, "bash", "-lc", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecResult(stdout=(proc.stdout or "") + (proc.stderr or ""), exit_code=proc.returncode)
        except subprocess.TimeoutExpired:
            return ExecResult(stdout=f"[timed out after {timeout}s]", exit_code=124)

    def read_file(self, path: str) -> str:
        res = self.execute(f"cat {path}")
        return res.stdout if res.exit_code == 0 else ""

    def write_file(self, path: str, content: str) -> None:
        # Pipe via base64 to survive arbitrary content without quoting issues.
        import base64

        encoded = base64.b64encode(content.encode()).decode()
        self.execute(f'mkdir -p "$(dirname {path})" && echo {encoded} | base64 -d > {path}')

    def git_diff(self) -> str:
        self.execute("git add -A")
        base = self._base_commit or "HEAD"
        res = self.execute(f"git diff --cached {base}")
        return res.stdout

    def cleanup(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], check=False, capture_output=True)
