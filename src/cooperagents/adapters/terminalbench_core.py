"""Terminal-Bench "core" adapter (classic 0.1.1 task format).

The classic Terminal-Bench dataset (``terminal-bench-core-0.1.1``) uses a flat
task layout that differs from the TB-3.0/Harbor format handled by
``terminalbench.py``:

    <task>/
      task.yaml            # instruction, timeouts, parser_name: pytest
      Dockerfile           # agent environment (built directly, context = task dir)
      docker-compose.yaml  # service topology (single ``client`` = single-container)
      run-tests.sh         # sources tests/setup-uv-pytest.sh, runs tests/run-uv-pytest.sh
      tests/               # pytest test files + the uv setup/run scripts
      solution.sh          # reference solution (unused here)

Execution model: the agent works in the container workdir (``/app``); the task's
own pytest suite asserts against that live filesystem, so a task "resolves" iff
its tests pass. This maps onto cooperagents' git substrate exactly like SWE-bench:
the submission is the git diff of ``/app``; :meth:`evaluate` applies it to a fresh
task container and runs the task's ``run-tests.sh``.

Scope: single-container tasks (one ``client`` service). Multi-container tasks
(e.g. the ``*-qemu`` ones) are excluded by :meth:`instances`, mirroring the
TB-3.0 adapter's pilot scope. Caveat: state written outside the git-tracked
workdir does not travel through a patch (shared with the TB-3.0 adapter).

Dataset root defaults to ``<repo>/data/terminal-bench-core-0.1.1`` (override with
``TBC_ROOT``).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

from cooperagents.adapters.base import BenchmarkAdapter
from cooperagents.env.docker import DockerEnv

_DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "data" / "terminal-bench-core-0.1.1"
TBC_ROOT = Path(os.getenv("TBC_ROOT", str(_DEFAULT_ROOT))).expanduser()
WORKSPACE = "/app"


def _task_dir(instance: str) -> Path:
    d = TBC_ROOT / instance
    if not d.is_dir():
        raise FileNotFoundError(f"terminal-bench-core task not found: {d}")
    return d


def _manifest(instance: str) -> dict:
    return yaml.safe_load((_task_dir(instance) / "task.yaml").read_text()) or {}


def _services(instance: str) -> dict:
    compose = _task_dir(instance) / "docker-compose.yaml"
    if not compose.is_file():
        return {}
    return (yaml.safe_load(compose.read_text()) or {}).get("services", {}) or {}


class TerminalBenchCoreAdapter(BenchmarkAdapter):
    name = "terminalbench-core"
    # verification is the task's own pytest suite; no generic build gate applies
    build_artifact = None
    reference_binary = None

    def instances(self) -> list[str]:
        """Single-container tasks (one compose service) carrying a task.yaml."""
        out = []
        for d in sorted(TBC_ROOT.iterdir()):
            if not (d / "task.yaml").is_file():
                continue
            if len(_services(d.name)) == 1:
                out.append(d.name)
        return out

    def image(self, instance: str) -> str:
        """Build the task image (context = task dir) and add a git layer: the
        team substrate (share/merge/diff) and patch submission need git, and the
        TB base images ship without it. WORKDIR is pinned to the task workdir."""
        base = f"tbc/{instance}:local"
        tag = f"tbc/{instance}:git"
        if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
            return tag
        if subprocess.run(["docker", "image", "inspect", base], capture_output=True).returncode != 0:
            subprocess.run(
                ["docker", "build", "-t", base, str(_task_dir(instance))],
                check=True, capture_output=True, text=True,
            )
        dockerfile = (
            f"FROM {base}\n"
            f"WORKDIR {WORKSPACE}\n"
            "USER root\n"
            "RUN command -v git >/dev/null 2>&1 || "
            "(apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*) || "
            "apk add --no-cache git || dnf install -y git\n"
        )
        subprocess.run(
            ["docker", "build", "-t", tag, "-f", "-", "."],
            input=dockerfile, check=True, capture_output=True, text=True,
        )
        return tag

    def task_for(self, instance: str, agent_index: int = 0, team_size: int = 1) -> str:
        instruction = str(_manifest(instance).get("instruction", "")).strip()
        return (
            "## Task\n\n" + instruction + "\n\n## Submission convention\n\n"
            f"Work in {WORKSPACE}. Make the required changes to the files there; your git "
            "diff is the submission. Do NOT `git stash`/`reset` or package your work into a "
            "patch file as the deliverable — leave every created/modified file in place.\n"
        )

    def env_kwargs(self) -> dict:
        return {"repo_path": WORKSPACE, "keepalive": "8h"}

    def brief(self, instance: str) -> str:
        return (
            f"ENVIRONMENT: you are in the task's working directory ({WORKSPACE}) inside its "
            "container, git-initialized at the base state. Make the required changes there and "
            "leave every created/modified file in place — your git diff is the submission."
        )

    def setup_env(self, env, agent_id: str) -> None:
        # TB images carry no git repo; init one at the workdir with a
        # deterministic base commit so git_diff() captures the agent's work.
        env.execute(
            f"mkdir -p {WORKSPACE} && cd {WORKSPACE} && if [ ! -d .git ]; then git init -q -b main && "
            "git config user.email tb@local && git config user.name tb && "
            "git add -A . >/dev/null 2>&1; "
            "GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' "
            "git commit -qm base --allow-empty; fi"
        )
        sha = env.execute("git rev-parse HEAD").stdout.strip()
        if sha:
            env._base_commit = sha

    def agent_timeout_s(self, instance: str) -> int:
        return int(float(_manifest(instance).get("max_agent_timeout_sec", 3600)))

    def submit(self, instance: str, patch: str, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "integrated.patch").write_text(patch)

    def _reward_from_pytest(self, output: str) -> float:
        """1.0 iff the pytest run reported only passes (>=1 passed, 0 failed/errored)."""
        passed = sum(int(m) for m in re.findall(r"(\d+) passed", output))
        failed = sum(int(m) for m in re.findall(r"(\d+) failed", output))
        errored = sum(int(m) for m in re.findall(r"(\d+) error", output))
        return 1.0 if passed > 0 and failed == 0 and errored == 0 else 0.0

    def evaluate(self, run_dir: Path) -> dict:
        """Score each instance: apply its patch to a fresh task container, run the
        task's own pytest suite, and read pass/fail. Returns {instance: reward},
        where reward is None when the instance could not be scored (image build
        failed) — a single bad task never aborts the batch."""
        out: dict[str, float | None] = {}
        for inst_dir in sorted(Path(run_dir).resolve().iterdir()):
            patch_file = inst_dir / "integrated.patch"
            if not patch_file.is_file():
                continue
            instance = inst_dir.name
            td = _task_dir(instance)
            try:
                env = DockerEnv(self.image(instance), **self.env_kwargs())
            except Exception as e:  # noqa: BLE001 - image build/pull failed; skip, don't abort
                out[instance] = None
                (inst_dir / "verifier_output.txt").write_text(
                    f"[evaluate skipped] image build failed: {type(e).__name__}: {e}\n")
                continue
            try:
                self.setup_env(env, "eval")
                patch = patch_file.read_text()
                if patch.strip():
                    env.write_file("/tmp/final.patch", patch)
                    env.execute(f"cd {WORKSPACE} && git apply --whitespace=nowarn /tmp/final.patch")
                # stage the task's test harness at /tests and run it (TB convention)
                for f in sorted((td / "tests").iterdir()):
                    if f.is_file():
                        env.write_file(f"/tests/{f.name}", f.read_text())
                env.write_file("/tests/run-tests.sh", (td / "run-tests.sh").read_text())
                timeout = int(float(_manifest(instance).get("max_test_timeout_sec", 600)))
                r = env.execute(
                    f"cd {WORKSPACE} && TEST_DIR=/tests bash /tests/run-tests.sh 2>&1",
                    timeout=timeout + 120,
                )
                reward = self._reward_from_pytest(r.stdout or "")
                (inst_dir / "verifier_output.txt").write_text(r.stdout or "")
            except Exception as e:  # noqa: BLE001 - run/apply error scores 0, keep going
                reward = 0.0
                (inst_dir / "verifier_output.txt").write_text(
                    f"[evaluate error] {type(e).__name__}: {e}\n")
            finally:
                env.cleanup()
            out[instance] = reward
        return out


__all__ = ["TerminalBenchCoreAdapter"]
