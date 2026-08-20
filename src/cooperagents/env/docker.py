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
    def __init__(
        self,
        image: str,
        *,
        name: str | None = None,
        keepalive: str = "4h",
        volumes: list[str] | None = None,
        repo_path: str = CONTAINER_REPO,
        network: str | None = None,
        user: str | None = None,
    ) -> None:
        self.image = image
        self.repo_path = repo_path
        self.name = name or f"ca-{uuid.uuid4().hex[:10]}"
        base = ["docker", "run", "-d", "--name", self.name, "--entrypoint", ""]
        if user:
            # ProgramBench cleanroom: the reference binary is execute-only for
            # uid 1000 ("agent"); running as root would let the agent read it.
            base += ["-u", user]
        if network:
            # e.g. "none" — ProgramBench cleanroom fidelity: agent commands
            # inside the container must not reach the internet (model calls
            # happen host-side and are unaffected).
            base += ["--network", network]
        self.volumes = list(volumes or [])
        for v in volumes or []:
            base += ["-v", v]
        tail = [image, "sleep", keepalive]
        first = subprocess.run([*base, *tail], capture_output=True, text=True)
        if first.returncode != 0:
            # Many CooperBench images are arm64-only with no amd64 manifest; on an
            # amd64 host docker auto-selects the host platform and fails. Retry under
            # arm64 emulation (binfmt) so the image still runs. Native (multi-arch)
            # images take the fast first path and never pay the emulation cost.
            if "platform" in first.stderr.lower() or "manifest" in first.stderr.lower():
                subprocess.run(
                    [*base, "--platform", "linux/arm64", *tail],
                    check=True,
                    capture_output=True,
                )
            else:
                raise subprocess.CalledProcessError(first.returncode, first.args, first.stdout, first.stderr)
        # CooperBench images ship the repo at /workspace/repo already; for any
        # other image, make sure the dir exists so `docker exec -w` succeeds.
        subprocess.run(
            ["docker", "exec", self.name, "bash", "-c", f"mkdir -p {self.repo_path}"],
            check=False,
            capture_output=True,
        )
        self._base_commit = self._current_commit()

    def _current_commit(self) -> str:
        res = self.execute("git rev-parse HEAD")
        return res.stdout.strip() if res.exit_code == 0 else ""

    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        try:
            # Non-login shell: a login shell (-l) sources /etc/profile which
            # RESETS PATH, hiding image-provided toolchains from the agent
            # (e.g. /usr/local/go/bin in the go-chi images — the agent then
            # cannot build/test its own code and ships broken patches).
            if len(command) < 100_000:
                argv, stdin = ["docker", "exec", "-w", self.repo_path, self.name, "bash", "-c", command], None
            else:
                # A single argv element is capped at ~128KB (MAX_ARG_STRLEN);
                # giant commands (e.g. an agent writing a whole file via one
                # heredoc) must be streamed over stdin instead.
                argv, stdin = ["docker", "exec", "-i", "-w", self.repo_path, self.name, "bash", "-s"], command
            proc = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                errors="replace",  # agents probing binaries emit non-UTF8 bytes
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
        if not res.stdout.strip():
            # Agents sometimes stash their work to "verify a clean patch"
            # right before finishing (observed: tree back at base, 1000+
            # lines sitting in the stash). Restore and re-collect.
            has_stash = self.execute("git stash list | head -1").stdout.strip()
            if has_stash:
                self.execute("git stash pop -q 2>/dev/null || git checkout stash@{0} -- . 2>/dev/null")
                self.execute("git add -A")
                res = self.execute(f"git diff --cached {base}")
        return res.stdout

    def cleanup(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], check=False, capture_output=True)
