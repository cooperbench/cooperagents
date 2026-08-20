"""Terminal-Bench 3.0 adapter (Harbor framework tasks) — skeleton.

Instance = a task directory name from the downloaded dataset (e.g.
"atrx-vep-crispr"); the dataset root defaults to ~/terminalbench/terminal-bench
(override with TB3_ROOT).

Integration shape (see docs/TERMINALBENCH_PLAN.md): Harbor is task source +
evaluator only. The agent environment image is built from the task's
environment/Dockerfile; the task statement is instruction.md verbatim; scoring
builds the task's tests/Dockerfile, injects the declared artifacts from the
submitted tree, runs test.sh, and reads /logs/verifier/reward.json.

Status: image/task/instances implemented; evaluate() is wired but the
artifact-extraction path is pilot-stage (single-container tasks only).
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

from cooperagents.adapters.base import BenchmarkAdapter

TB3_ROOT = Path(os.getenv("TB3_ROOT", "~/terminalbench/terminal-bench")).expanduser()


def _task_dir(instance: str) -> Path:
    d = TB3_ROOT / instance
    if not d.is_dir():
        raise FileNotFoundError(f"terminal-bench task not found: {d}")
    return d


def _manifest(instance: str) -> dict:
    return tomllib.loads((_task_dir(instance) / "task.toml").read_text())


class TerminalBenchAdapter(BenchmarkAdapter):
    # No build gate / reference binary: verification is the task's own
    # pytest suite, run in the task's separate verifier image.
    build_artifact = None
    reference_binary = None

    def instances(self) -> list[str]:
        """Single-container tasks only (pilot scope)."""
        out = []
        for d in sorted(TB3_ROOT.iterdir()):
            if (d / "task.toml").is_file() and not (d / "environment" / "docker-compose.yaml").exists():
                out.append(d.name)
        return out

    def image(self, instance: str) -> str:
        """Build (cached) the agent-environment image for this task."""
        tag = f"tb3/{instance}:local"
        have = subprocess.run(["docker", "image", "inspect", tag],
                              capture_output=True).returncode == 0
        if not have:
            subprocess.run(
                ["docker", "build", "-t", tag, str(_task_dir(instance) / "environment")],
                check=True, capture_output=True, text=True)
        return tag

    def task_for(self, instance: str) -> str:
        return (_task_dir(instance) / "instruction.md").read_text()

    def agent_timeout_s(self, instance: str) -> int:
        return int(_manifest(instance).get("agent", {}).get("timeout_sec", 18000))

    def artifacts(self, instance: str) -> list[str]:
        return list(_manifest(instance).get("artifacts", []))

    def submit(self, env, run_dir: Path, instance: str) -> Path:
        """Extract the declared artifact paths from the (merged/selected)
        environment into run_dir/artifacts/, preserving absolute layout."""
        out = run_dir / "artifacts"
        out.mkdir(parents=True, exist_ok=True)
        for a in self.artifacts(instance):
            res = env.execute(f"cat {a} 2>/dev/null | base64 -w0")
            if res.exit_code == 0 and res.stdout.strip():
                import base64
                dst = out / a.lstrip("/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(base64.b64decode(res.stdout.strip()))
        return out

    def evaluate(self, run_dir: Path, instance: str) -> float:
        """Build the verifier image, mount artifacts + logs, run test.sh,
        read reward.json. Returns reward in [0, 1]."""
        td = _task_dir(instance)
        vtag = f"tb3v/{instance}:local"
        if subprocess.run(["docker", "image", "inspect", vtag], capture_output=True).returncode != 0:
            subprocess.run(["docker", "build", "-t", vtag, str(td / "tests")],
                           check=True, capture_output=True, text=True)
        logs = run_dir / "verifier_logs"
        logs.mkdir(exist_ok=True)
        mounts = ["-v", f"{logs}:/logs"]
        art_root = run_dir / "artifacts"
        for a in self.artifacts(instance):
            src = art_root / a.lstrip("/")
            if src.exists():
                mounts += ["-v", f"{src}:{a}:ro"]
        timeout = int(_manifest(instance).get("verifier", {}).get("timeout_sec", 600))
        subprocess.run(["docker", "run", "--rm", *mounts, vtag, "bash", "/tests/test.sh"],
                       capture_output=True, text=True, timeout=timeout + 60)
        reward_file = logs / "verifier" / "reward.json"
        if reward_file.exists():
            data = json.loads(reward_file.read_text())
            return float(data.get("reward", data.get("score", 0.0)))
        return 0.0
