"""coopgit-min — the minimal E0 seed harness, self-contained.

Exactly and only "coop + git": two mini-swe agents (the vendored agent,
unchanged — the held constant) work the SAME task in separate containers,
connected by a shared bare git remote; their diffs are merged mechanically
(3-way, apply-chain fallback with visible damage); the merged tree is the
submission. No coordinator, no repair, no gates, no board, no roles, no
spawn — those are for evolution to add.

Usage (CLI-compatible with TeamHarness.execute):
  python run.py --instance <id> --rep <rep> [--arm coopgit-min]
                [--step-limit 1000] [--runs-dir runs]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

import yaml  # noqa: E402

from cooperagents.vendor.mini_swe.agents.default import DefaultAgent  # noqa: E402
from cooperagents.vendor.mini_swe.exceptions import LimitsExceeded, Submitted  # noqa: E402
from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModel  # noqa: E402

WORKSPACE = "/workspace"
GITSHARE = "/cbshared/repo.git"
GIT_ID = "-c user.name=agent -c user.email=agent@team"

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

GIT_SHARE_SYSTEM = (
    "\n\nA shared git remote named 'shared' connects you to a teammate working "
    "on the SAME task in a parallel copy of this repository. Your work is "
    "pushed to shared/<your-id> automatically every 45s. Run `git fetch shared` "
    "to see theirs; view files with `git diff HEAD...shared/<teammate> -- <file>` "
    "and take their version with `git checkout shared/<teammate> -- <file>`. "
    "Reusing their public names and code avoids conflicts at merge time."
)


class DockerEnv:
    """Minimal container: run as uid 'agent', no network, docker-exec shell."""

    def __init__(self, image: str, volumes: list[str] | None = None):
        self.name = f"cgm-{uuid.uuid4().hex[:10]}"
        self.repo_path = WORKSPACE
        cmd = ["docker", "run", "-d", "--name", self.name, "--entrypoint", "",
               "-u", "agent", "--network", "none"]
        for v in volumes or []:
            cmd += ["-v", v]
        subprocess.run([*cmd, image, "sleep", "24h"], check=True, capture_output=True)
        subprocess.run(["docker", "exec", "-u", "root", self.name, "bash", "-c",
                        "chown -R agent:agent /cbshared 2>/dev/null; true"], capture_output=True)
        # the execute-only reference binary makes `git add -A` fatal; exclude it
        self.execute("printf 'executable\\n' >> .git/info/exclude")
        self.base = self.execute("git rev-parse HEAD").strip()

    def execute(self, command: str, timeout: int = 300) -> str:
        try:
            p = subprocess.run(["docker", "exec", "-i", "-w", WORKSPACE, self.name, "bash", "-s"],
                               input=command, capture_output=True, text=True,
                               errors="replace", timeout=timeout)
            self.rc = p.returncode
            return (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            self.rc = 124
            return f"[timed out after {timeout}s]"

    def git_diff(self) -> str:
        self.execute("git add -A")
        return self.execute(f"git diff --cached {self.base or 'HEAD'}")

    def cleanup(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


class AgentEnv:
    """mini-swe environment protocol over a DockerEnv."""

    config = None

    def __init__(self, denv: DockerEnv, deadline: float):
        self._d = denv
        self._deadline = deadline

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict:
        if time.time() > self._deadline:
            raise LimitsExceeded({"role": "exit", "content": "LimitsExceeded",
                                  "extra": {"exit_status": "LimitsExceeded", "submission": ""}})
        out = self._d.execute(action.get("command", ""), timeout=timeout or 240)
        res = {"output": out, "returncode": self._d.rc, "exception_info": ""}
        lines = out.lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" and self._d.rc == 0:
            sub = "".join(lines[1:])
            raise Submitted({"role": "exit", "content": sub,
                             "extra": {"exit_status": "Submitted", "submission": sub}})
        return res

    def get_template_vars(self, **kw) -> dict:
        import platform
        return {**platform.uname()._asdict(), "cwd": WORKSPACE, **kw}

    def serialize(self) -> dict:
        return {}


def build_model() -> LitellmModel:
    cfg = yaml.safe_load((HERE / "src/cooperagents/vendor/mini_swe/config/solo.yaml").read_text())
    mk = dict(cfg["model"].get("model_kwargs", {}))
    base_url = os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if base_url:
        mk["api_base"] = base_url
    key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if key:
        mk["api_key"] = key
    mk["temperature"] = 0.0
    name = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Qwen/Qwen3.5-9B")
    # self-hosted model has no litellm price entry; a cost lookup would raise
    return LitellmModel(model_name=f"openai/{name}", model_kwargs=mk,
                        cost_tracking="ignore_errors")


def run_agent(aid: str, denv: DockerEnv, step_limit: int, time_limit_s: int) -> dict:
    cfg = yaml.safe_load((HERE / "src/cooperagents/vendor/mini_swe/config/solo.yaml").read_text())
    agent = DefaultAgent(
        build_model(),
        AgentEnv(denv, time.time() + time_limit_s),
        agent_id=aid,
        system_template=cfg["agent"]["system_template"] + GIT_SHARE_SYSTEM,
        instance_template=cfg["agent"]["instance_template"],
        step_limit=step_limit,
        cost_limit=10.0,
    )
    try:
        extra = agent.run(task=TASK)
        status = "submitted" if extra.get("exit_status") == "Submitted" else "limit"
        err = None
    except Exception as e:  # noqa: BLE001
        status, err = "error", str(e)
    return {"agent_id": aid, "status": status, "steps": agent.n_calls,
            "error": err, "messages": agent.messages}


def apply_commit(env: DockerEnv, patch: str, msg: str) -> None:
    import base64
    b = base64.b64encode(patch.encode()).decode()
    env.execute(f"echo {b} | base64 -d > .cb_d.patch")
    env.execute("git apply --whitespace=nowarn .cb_d.patch 2>/dev/null "
                "|| git apply --3way .cb_d.patch 2>/dev/null || true; rm -f .cb_d.patch")
    env.execute(f"git add -A && git {GIT_ID} commit -q -m '{msg}' || true")


def merge(env: DockerEnv, deltas: list[str]) -> str:
    """3-way merge of the agents' diffs; on genuine conflict fall back to the
    apply-chain (second diff applied with --3way/--reject on top of the first,
    damage left visible). Returns the integrated diff vs base."""
    nz = [d for d in deltas if d.strip()]
    if not nz:
        return ""
    base = env.base or "HEAD"
    env.execute(f"git checkout -q -B _acc {base}")
    apply_commit(env, nz[0], "d0")
    conflict = False
    for i, d in enumerate(nz[1:], 1):
        env.execute(f"git checkout -q -B _b{i} {base}")
        apply_commit(env, d, f"d{i}")
        env.execute("git checkout -q _acc")
        env.execute(f"git {GIT_ID} merge --no-edit _b{i} 2>&1")
        if env.rc != 0:
            conflict = True
            env.execute("git merge --abort 2>/dev/null || true")
            break
    if conflict:
        env.execute(f"git checkout -q -B _acc {base}")
        apply_commit(env, nz[0], "d0")
        for i, d in enumerate(nz[1:], 1):
            import base64
            b = base64.b64encode(d.encode()).decode()
            env.execute(f"echo {b} | base64 -d > .cb_d.patch")
            env.execute("git apply --whitespace=nowarn .cb_d.patch 2>/dev/null "
                        "|| git apply --3way .cb_d.patch 2>/dev/null "
                        "|| git apply --reject .cb_d.patch 2>/dev/null || true; rm -f .cb_d.patch")
        env.execute(f"git add -A && git {GIT_ID} commit -q -m chain || true")
    return env.git_diff()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True)
    ap.add_argument("--rep", required=True)
    ap.add_argument("--arm", default="coopgit-min")  # run-dir naming only
    ap.add_argument("--step-limit", type=int, default=1000)
    ap.add_argument("--agent-time-limit", type=int, default=3600)
    ap.add_argument("--runs-dir", default="runs")
    args = ap.parse_args()

    img = f"programbench/{args.instance.replace('__', '_1776_')}:task_cleanroom_v6"
    out = Path(args.runs_dir) / f"pb-{args.arm}-{args.rep}" / args.instance
    out.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:8]
    vol = [f"cgm{run_id}:/cbshared"]
    t0 = time.time()

    envs = {aid: DockerEnv(img, vol) for aid in ("agent1", "agent2")}
    try:
        first = True
        for aid, e in envs.items():
            if first:
                e.execute(f"git init -q --bare {GITSHARE} 2>/dev/null || true")
                first = False
            e.execute(f"git remote add shared {GITSHARE} 2>/dev/null || true")
            e.execute(f"git push -q shared HEAD:refs/heads/{aid} 2>/dev/null || true")

        stop = threading.Event()

        def sync() -> None:  # push each agent's dirty tree to its branch
            while not stop.wait(45):
                for aid, e in list(envs.items()):
                    try:
                        e.execute("C=$(git stash create 2>/dev/null); "
                                  f"git push -q -f shared ${{C:-HEAD}}:refs/heads/{aid} 2>/dev/null || true",
                                  timeout=60)
                    except Exception:  # noqa: BLE001
                        pass

        threading.Thread(target=sync, daemon=True).start()

        results: dict[str, dict] = {}
        ths = [threading.Thread(target=lambda a=a: results.update(
            {a: run_agent(a, envs[a], args.step_limit, args.agent_time_limit)}))
            for a in envs]
        [t.start() for t in ths]
        [t.join() for t in ths]
        stop.set()
        deltas = [envs[a].git_diff() for a in ("agent1", "agent2")]
    finally:
        for e in envs.values():
            e.cleanup()

    menv = DockerEnv(img, vol)
    try:
        integrated = merge(menv, deltas)
        menv.execute("find . -path ./.git -prune -o \\( -name '*.rej' -o -name '*.orig' \\) "
                     "-print0 | xargs -0 -r rm -f")
        menv.execute("tar -czf /tmp/submission.tar.gz --exclude=./.git --exclude=./executable .")
        subprocess.run(["docker", "cp", f"{menv.name}:/tmp/submission.tar.gz",
                        str(out / "submission.tar.gz")], check=True)
        integrated = menv.git_diff()
    finally:
        menv.cleanup()
    subprocess.run(["docker", "volume", "rm", f"cgm{run_id}"], capture_output=True)

    (out / "integrated.patch").write_text(integrated)
    tdir = out / "trajectories"
    tdir.mkdir(exist_ok=True)
    for aid, r in results.items():
        (tdir / f"{aid}.json").write_text(json.dumps(r, indent=1, default=str))
    steps = sum(r["steps"] for r in results.values())
    (out / "run_meta.txt").write_text(
        f"arm={args.arm} rep={args.rep} duration_s={time.time()-t0:.0f} "
        f"steps={steps} patch_bytes={len(integrated)}\n")
    (out / "metrics.json").write_text(json.dumps(
        {"statuses": {a: r["status"] for a, r in results.items()}}, indent=1))
    print(f"[pb-{args.arm}-{args.rep}] done: steps={steps} patch_bytes={len(integrated)}")


if __name__ == "__main__":
    main()
