"""ProgramBench adapter: reconstruct a program from its execute-only binary.

Instance = a ProgramBench instance id (e.g. "abishekvashok__cmatrix.5c082c6").
Score = % of the hidden behavioral test suite passed (programbench eval).

Verification lives in ``cooperagents.verification``; this adapter only
declares the task's two facts (the build must produce ``./executable``; a
runnable reference lives at ``./executable``) and adapts I/O.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cooperagents.adapters.base import BenchmarkAdapter
from cooperagents.env.docker import DockerEnv

WORKSPACE = "/workspace"

TASK = """## Task context

We want to write the source code for a given executable. The executable is
located at `./executable` in the workspace root. You also have access to the
existing documentation (README.md, the man page, and anything else bundled in
the workspace).

## Your task

Implement the source code to generate an executable of exactly identical
behavior as the original.

Rules:
- You MUST create a `./compile.sh` script; running `bash compile.sh` with no
  network access must build your source and produce `./executable` in the
  workspace root. Grading rebuilds from source with your compile.sh — a
  submission without a working compile.sh scores zero.
- Do NOT wrap, shim, or delegate to the provided `./executable`.
- Do NOT decompile the provided `./executable` or use strace/ltrace on it.
- You MAY run the executable with any inputs, flags, and arguments to observe
  its behavior, and read all documentation files.
- Behavior to match includes: --help and --version output, flag handling and
  error messages, exit codes, and the program's visible behavior.

Work in the workspace root. Verify your build (`bash compile.sh`) and compare
your program's behavior against the original before finishing."""


class ProgramBenchAdapter(BenchmarkAdapter):
    name = "programbench"
    build_artifact = "executable"
    reference_binary = "./executable"

    def image(self, instance: str) -> str:
        """Instance id -> cleanroom image ("__" is encoded as "_1776_")."""
        return f"programbench/{instance.replace('__', '_1776_')}:task_cleanroom_v6"

    def env_kwargs(self) -> dict:
        # execute-only reference binary => run as uid "agent"; cleanroom
        # fidelity => no network inside the container.
        return {"repo_path": WORKSPACE, "network": "none", "user": "agent",
                "keepalive": "24h"}

    def setup_env(self, env, agent_id: str) -> None:
        subprocess.run(["docker", "exec", "-u", "root", env.name, "bash", "-c",
                        "chown -R agent:agent /workspace/shared /cbshared 2>/dev/null; true"],
                       capture_output=True)
        # the execute-only binary makes `git add -A` fatal and empties diffs
        env.execute("printf 'executable\nshared/\n' >> .git/info/exclude")
        # team merge needs to know which shared branch is "self"
        env.execute(f"echo {agent_id} > /tmp/.agent_id")
        # capture reference output rate BEFORE any build overwrites the
        # execute-only reference binary; verification.repair compares to it
        env.execute("(TERM=xterm timeout 2 script -qec './executable' /dev/null "
                    "</dev/null 2>/dev/null | wc -c) > /tmp/.ref_rate 2>/dev/null "
                    "|| echo 0 > /tmp/.ref_rate")

    def task_for(self, instance: str, agent_index: int, team_size: int) -> str:
        return TASK  # one shared objective; every agent gets the same task

    def brief(self, instance: str) -> str:
        """Probed toolchain list + the no-network constraint stated
        operationally (agents obey the rule's letter yet reach for package
        registries; naming the consequence up front prevents it)."""
        env = DockerEnv(self.image(instance), **self.env_kwargs())
        try:
            vers = env.execute(
                "for t in gcc g++ make cargo rustc go python3; do "
                "command -v $t >/dev/null 2>&1 && echo \"$t: $($t --version 2>/dev/null | head -1)\"; done",
                timeout=60,
            ).stdout.strip()
        finally:
            env.cleanup()
        return (
            "## Environment\n\nToolchains available in this workspace:\n"
            f"{vers}\n\n"
            "There is NO network access during your run and during grading: any "
            "package download (crates.io, proxy.golang.org, pip install) will "
            "fail. Write your implementation against the STANDARD LIBRARY of the "
            "language you pick, or vendor dependency sources in-tree. Before "
            "finishing, verify that `bash compile.sh` produces ./executable.\n\n"
        )

    def submit(self, instance: str, patch: str, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        env = DockerEnv(self.image(instance), **self.env_kwargs())
        try:
            env.write_file("/tmp/final.patch", patch)
            r = env.execute(
                "git apply --whitespace=nowarn /tmp/final.patch 2>/dev/null"
                " || git apply --3way /tmp/final.patch 2>/dev/null"
                " || git apply --reject /tmp/final.patch 2>/dev/null; "
                "find . -path ./.git -prune -o \\( -name '*.rej' -o -name '*.orig' \\) -print0 | xargs -0 -r rm -f; "
                "tar -czf /tmp/submission.tar.gz --exclude=./.git --exclude=./shared "
                "--exclude=./executable --exclude=./compile_out ."
            )
            if r.exit_code != 0:
                print(f"[warn] submission tar step exit={r.exit_code}: {r.stdout[-300:]}")
            subprocess.run(["docker", "cp", f"{env.name}:/tmp/submission.tar.gz",
                            str(out_dir / "submission.tar.gz")], check=True)
        finally:
            env.cleanup()

    def evaluate(self, run_dir: Path):
        """Run `programbench eval` (requires the ProgramBench checkout)."""
        return subprocess.run(
            ["uv", "run", "programbench", "eval", str(run_dir)],
            cwd=Path.home() / "ProgramBench", capture_output=True, text=True,
        )
