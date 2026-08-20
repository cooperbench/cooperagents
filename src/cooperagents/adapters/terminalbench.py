"""Terminal-Bench 3.0 adapter (Harbor framework tasks).

Instance = a task directory name from the downloaded dataset (e.g.
"cli-2ph-simplex"); dataset root defaults to ~/terminalbench/terminal-bench
(override with TB3_ROOT).

Integration shape (docs/TERMINALBENCH_PLAN.md): Harbor is task source +
evaluator only. The agent-environment image is built from the task's
environment/Dockerfile; the task statement is instruction.md verbatim.
Submission applies the team's merged patch to a fresh task container and
extracts the task.toml-declared artifact paths; evaluation builds the task's
verifier image (tests/Dockerfile), mounts the artifacts read-only at their
declared paths, runs test.sh, and reads /logs/verifier/reward.json.

Pilot scope: single-container tasks (62/74). Known seam caveat: state outside
the git-tracked workdir does not transfer through a patch — measured in the
pilot; fallback is whole-container selection via select_integration.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tomllib
import os
from pathlib import Path

from cooperagents.adapters.base import BenchmarkAdapter
from cooperagents.env.docker import DockerEnv

TB3_ROOT = Path(os.getenv("TB3_ROOT", "~/terminalbench/terminal-bench")).expanduser()
WORKSPACE = "/app"


def _task_dir(instance: str) -> Path:
    d = TB3_ROOT / instance
    if not d.is_dir():
        raise FileNotFoundError(f"terminal-bench task not found: {d}")
    return d


def _manifest(instance: str) -> dict:
    return tomllib.loads((_task_dir(instance) / "task.toml").read_text())


class TerminalBenchAdapter(BenchmarkAdapter):
    name = "terminalbench"
    # verification is the task's own pytest suite in its verifier image;
    # no generic build gate / reference binary applies
    build_artifact = None
    reference_binary = None

    def instances(self) -> list[str]:
        return [d.name for d in sorted(TB3_ROOT.iterdir())
                if (d / "task.toml").is_file()
                and not (d / "environment" / "docker-compose.yaml").exists()]

    def image(self, instance: str) -> str:
        """Task image + a git layer: TB images ship without git, but the
        team substrate (share/merge/diff) and patch-based submission need it."""
        base = f"tb3/{instance}:local"
        tag = f"tb3/{instance}:git"
        if subprocess.run(["docker", "image", "inspect", tag],
                          capture_output=True).returncode == 0:
            return tag
        if subprocess.run(["docker", "image", "inspect", base],
                          capture_output=True).returncode != 0:
            subprocess.run(
                ["docker", "build", "-t", base, str(_task_dir(instance) / "environment")],
                check=True, capture_output=True, text=True)
        dockerfile = (
            f"FROM {base}\n"
            "USER root\n"
            "RUN command -v git >/dev/null 2>&1 || "
            "(apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*) || "
            "apk add --no-cache git || dnf install -y git\n"
        )
        subprocess.run(["docker", "build", "-t", tag, "-f", "-", "."],
                       input=dockerfile, check=True, capture_output=True, text=True)
        return tag

    def task_for(self, instance: str, agent_index: int = 0, team_size: int = 1) -> str:
        return (_task_dir(instance) / "instruction.md").read_text() + (
            "\n\n## Submission convention\n\n"
            "Your final WORKING TREE is your submission — the files as they sit "
            "in the workspace when you finish. Do NOT `git stash`, `git reset`, "
            "or otherwise clean the tree before finishing, and do not package "
            "your work into a patch file as the deliverable: leave every "
            "created/modified file in place.\n"
        )

    def env_kwargs(self) -> dict:
        return {"repo_path": WORKSPACE, "keepalive": "8h"}

    def setup_env(self, env, agent_id: str) -> None:
        # TB images carry no git repo; the team substrate (share/merge/diff)
        # needs one rooted at the workdir with a deterministic base commit.
        env.execute(
            "cd %s && if [ ! -d .git ]; then git init -q -b main && "
            "git config user.email tb@local && git config user.name tb && "
            "git add -A . >/dev/null 2>&1; "
            "GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' "
            "git commit -qm base --allow-empty; fi" % WORKSPACE)
        # DockerEnv captured _base_commit before the repo existed; without it,
        # git_diff() falls back to HEAD and an agent that COMMITS its work
        # produces an empty final diff (observed: submitted smoke, 0-byte patch)
        sha = env.execute("git rev-parse HEAD").stdout.strip()
        if sha:
            env._base_commit = sha

    def artifacts(self, instance: str) -> list[str]:
        return list(_manifest(instance).get("artifacts", []))

    def agent_timeout_s(self, instance: str) -> int:
        return int(_manifest(instance).get("agent", {}).get("timeout_sec", 18000))

    def submit(self, instance: str, patch: str, out_dir: Path) -> None:
        """Apply the merged patch in a fresh task container; extract the
        declared artifacts into out_dir/artifacts/ (absolute layout kept)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "integrated.patch").write_text(patch)
        env = DockerEnv(self.image(instance), **self.env_kwargs())
        try:
            self.setup_env(env, "submit")
            env.write_file("/tmp/final.patch", patch)
            env.execute("cd %s && git apply --whitespace=nowarn /tmp/final.patch" % WORKSPACE)
            art_root = out_dir / "artifacts"
            for a in self.artifacts(instance):
                listing = env.execute(
                    f"find {a} -type f 2>/dev/null || true").stdout.split()
                for f in listing or []:
                    r = env.execute(f"base64 -w0 '{f}' 2>/dev/null")
                    if r.exit_code == 0 and r.stdout.strip():
                        dst = art_root / f.lstrip("/")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(base64.b64decode(r.stdout.strip()))
        finally:
            env.cleanup()

    def evaluate(self, run_dir: Path) -> dict:
        """Score every instance directory under run_dir with the task's own
        verifier. Returns {instance: reward}."""
        out = {}
        for inst_dir in sorted(Path(run_dir).iterdir()):
            if not (inst_dir / "integrated.patch").exists():
                continue
            instance = inst_dir.name
            td = _task_dir(instance)
            vtag = f"tb3v/{instance}:local"
            if subprocess.run(["docker", "image", "inspect", vtag],
                              capture_output=True).returncode != 0:
                subprocess.run(["docker", "build", "-t", vtag, str(td / "tests")],
                               check=True, capture_output=True, text=True)
            logs = inst_dir / "verifier_logs"
            logs.mkdir(exist_ok=True)
            mounts = ["-v", f"{logs}:/logs"]
            for a in self.artifacts(instance):
                src = inst_dir / "artifacts" / a.lstrip("/")
                if src.exists():
                    mounts += ["-v", f"{src}:{a}:ro"]
            timeout = int(_manifest(instance).get("verifier", {}).get("timeout_sec", 600))
            try:
                subprocess.run(["docker", "run", "--rm", *mounts, vtag,
                                "bash", "/tests/test.sh"],
                               capture_output=True, text=True, timeout=timeout + 60)
            except subprocess.TimeoutExpired:
                pass
            rf = logs / "verifier" / "reward.json"
            out[instance] = (float(json.loads(rf.read_text()).get("reward", 0.0))
                             if rf.exists() else 0.0)
        return out
