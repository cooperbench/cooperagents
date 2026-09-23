"""Persistent, job-owned writable Apptainer sandboxes for CPU cluster tasks."""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from cooperagents.env.base import Environment, ExecResult
from cooperagents.env.limited_process import run_limited


class ApptainerEnv(Environment):
    def __init__(self, image: str, *, scratch: str, shared: str | None = None, repo_path: str = "/workspace/repo") -> None:
        sif = Path(image).resolve(strict=True)
        if not sif.is_file():
            raise ValueError("Apptainer image must be a prepared SIF file")
        Path(scratch).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ca-", dir=scratch))
        self.repo_path = repo_path
        self._lock = threading.RLock()
        self._closed = False
        self._host_env = {
            k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "APPTAINER_CACHEDIR", "APPTAINER_TMPDIR")
        }
        self._host_env["HOME"] = str(self.root / "host-home")
        (self.root / "host-home").mkdir()
        try:
            subprocess.run(
                ["apptainer", "build", "--fix-perms", "--sandbox", str(self.root / "fs"), str(sif)],
                env=self._host_env,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            for directory in ("tmp", "var/tmp", "home/agent", "workspace", "patches", "cbshared"):
                (self.root / "fs" / directory).mkdir(parents=True, exist_ok=True)
            self._argv = [
                "apptainer",
                "exec",
                "--writable",
                "--containall",
                "--cleanenv",
                "--no-mount",
                "hostfs,bind-paths",
                "--bind",
                "/etc/resolv.conf:/etc/resolv.conf:ro",
                "--home",
                f"{self.root / 'fs/home/agent'}:/home/agent",
                "--bind",
                f"{self.root / 'fs/tmp'}:/tmp",
                "--bind",
                f"{self.root / 'fs/var/tmp'}:/var/tmp",
            ]
            if shared:
                Path(shared).mkdir(parents=True, exist_ok=True)
                self._argv += ["--bind", f"{Path(shared).resolve()}:/cbshared"]
            self._argv += ["--pwd", "/", str(self.root / "fs")]
            result = self.execute("git rev-parse HEAD")
            if result.exit_code:
                raise RuntimeError(f"Task repository unavailable: {result.stdout}")
            self._base_commit = result.stdout.strip()
        except BaseException:
            self.cleanup()
            raise

    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        # Serialize commands within one writable filesystem, including Git sync.
        with self._lock:
            if self._closed:
                raise RuntimeError("Apptainer environment is closed")
            script = f"cd {shlex.quote(self.repo_path)} || exit 125\n{command}"
            result, _ = run_limited(
                [*self._argv, "bash", "-s"],
                input_text=script,
                timeout=timeout,
                env=self._host_env,
                new_session=True,
            )
            return result

    def read_file(self, path: str) -> str:
        result = self.execute(f"cat -- {shlex.quote(path)}")
        return result.stdout if result.exit_code == 0 else ""

    def write_file(self, path: str, content: str) -> None:
        encoded = base64.b64encode(content.encode()).decode()
        parent = shlex.quote(str(Path(path).parent))
        result = self.execute(f"mkdir -p -- {parent} && printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}")
        if result.exit_code:
            raise RuntimeError(result.stdout)

    def git_diff(self) -> str:
        self.execute("git add -A")
        result = self.execute(f"git diff --cached {shlex.quote(self._base_commit)}")
        if not result.stdout.strip() and self.execute("git stash list | head -1").stdout.strip():
            self.execute("git stash pop -q 2>/dev/null || git checkout stash@{0} -- . 2>/dev/null; git add -A")
            result = self.execute(f"git diff --cached {shlex.quote(self._base_commit)}")
        return result.stdout

    def recover_shared_diff(self, agent_id: str) -> str:
        # Run outside the repo in case the worker removed its working directory.
        with self._lock:
            cwd, self.repo_path = self.repo_path, "/"
            try:
                result = self.execute(f"git --git-dir=/cbshared/repo.git diff {shlex.quote(self._base_commit)} {shlex.quote(agent_id)}")
                return result.stdout if result.exit_code == 0 else ""
            finally:
                self.repo_path = cwd

    def cleanup(self) -> None:
        with self._lock:
            self._closed = True
            shutil.rmtree(self.root, ignore_errors=True)
