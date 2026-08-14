"""ProgramBench single-instance comparison: solo vs coop+git vs complete-Team.

One indivisible task per instance (reconstruct a program from its binary +
docs), so this directly tests whether a 2-agent team beats 1 agent on the
SAME task — no benchmark-provided work split (removes CooperBench's
separability confound). Agent held constant: vendored mini-swe.

Arms
  solo     1 agent
  coopgit  2 agents, same task, live git-share substrate, mechanical 3-way
           merge, NO repair (the ablation report's Coop+git protocol)
  teamfull 2 agents, team_roles: lead/member prompts + task board +
           scratchpad volume; the LEAD's merged tree is the submission

Output layout (per arm/rep): runs/pb-<arm>-<rep>/<instance>/submission.tar.gz
— directly consumable by `programbench eval`.

Usage:
  ENV_FILE=.env.qwen .venv/bin/python scripts/bench_programbench.py \
      --instance abishekvashok__cmatrix.5c082c6 --arm teamfull --rep a \
      [--step-limit 100]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.docker import DockerEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.types import Assignment, TeamSpec

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


NEGOTIATE_LEAD = """

TEAM DIVISION PROTOCOL — this ONE task is shared by a 2-agent team; you must
NOT implement everything yourself. Before any coding:
1. FIRST ACTION: send_message to agent2 with a concrete division proposal
   (add "wait": true so the reply comes back in the same call). Example
   division: one agent owns the core program logic and screen rendering; the
   other owns option parsing, help/version/error output, man-page behaviors,
   and compile.sh.
2. Exchange messages until you have EXPLICIT agreement — at least one
   proposal and one confirmation.
3. The plan you write to /workspace/shared/PLAN.md and the board tasks you
   create MUST be the agreed division. Implement ONLY the components you own;
   your teammate's components arrive via their patch at merge time."""

NEGOTIATE_MEMBER = """

TEAM DIVISION PROTOCOL — this ONE task is shared by a 2-agent team; you must
NOT implement everything yourself. Before any coding:
1. FIRST ACTION: send_message to agent1 stating which components you propose
   to own (add "wait": true so the reply comes back in the same call).
2. Exchange messages until you have EXPLICIT agreement — at least one
   proposal and one confirmation.
3. Claim the board task matching the agreed division and implement ONLY the
   components you own. Define clean interfaces (shared header / function
   signatures) in /workspace/shared/ so the merged program compiles."""


def image_for(instance: str) -> str:
    return f"programbench/{instance.replace('__', '_1776_')}:task_cleanroom_v6"


BUILD_GATE = "rm -f ./executable; [ -f ./compile.sh ] && bash ./compile.sh >/tmp/build.log 2>&1; test -x ./executable"

# Iteration 5: behavioral probes beyond flag handling, all reference-comparative
# (the same probe is run on the reference binary; only the comparison matters,
# so the mechanism is program-general, not cmatrix-specific).
#   RATE_PROBE  — bytes the binary writes to a pty in 2s. coopgitc2-r3's binary
#     built, matched 2 flag probes, then wrote 11.3MB in 2s (reference: 88KB) —
#     a busy-loop redraw that starved the evaluator's terminal emulation until
#     every branch hit its 1h cap (results_read_failed). A candidate emitting
#     >10x the reference rate is ranked below any well-paced build.
#   QUIT_PROBE  — feed 'q' in a pty, compare exit codes. The reference exits 0
#     promptly; a binary that ignores input gets killed at the timeout (124).
#     Survivable for grading (coopgitc2-r2: 85.8 despite failing this) so it
#     ranks candidates but does not gate repair.
RATE_PROBE = ("TERM=xterm timeout 2 script -qec './executable' /dev/null "
              "</dev/null 2>/dev/null | wc -c")
QUIT_PROBE = ("printf q | TERM=xterm timeout 8 script -qec './executable' /dev/null "
              ">/dev/null 2>&1; echo RC=$?")
FIREHOSE_FLOOR = 200_000  # bytes/2s; below this no candidate is called a firehose


def _rate(env) -> int:
    out = env.execute(RATE_PROBE, timeout=30).stdout.strip().splitlines()
    try:
        return int(out[-1])
    except (ValueError, IndexError):
        return 0


def _quit_rc(env) -> int:
    out = env.execute(QUIT_PROBE, timeout=30).stdout
    m = re.search(r"RC=(\d+)", out)
    return int(m.group(1)) if m else -1


def _is_firehose(cand_rate: int, ref_rate: int) -> bool:
    return cand_rate > max(10 * ref_rate, FIREHOSE_FLOOR)

REPAIR_TASK = """You are the integration repairer for a team that just merged its work into
THIS tree. The task was: write source code + a `./compile.sh` build script so
that `bash compile.sh` produces `./executable` (a program matching the
documented behavior of the original; see README.md / the man page).

The build is currently BROKEN (or compile.sh is missing). Evidence:

```
{evidence}
```

Fix the build WITHOUT discarding any teammate's work:
- resolve any merge conflict markers (<<<<<<< ======= >>>>>>>) by RECONCILING
  both sides (keep both contributions where possible);
- reconcile duplicate or missing definitions across files;
- create or fix `./compile.sh` if it is missing or wrong;
- then run `bash compile.sh` and confirm it produces a working `./executable`
  (try `./executable --help` and `./executable --version`).
Do not start over; repair what exists."""

BEHAVIOR_REPAIR_TASK = """You are the integration repairer for a team that just merged its work into
THIS tree. The task was: write source code + a `./compile.sh` build script so
that `bash compile.sh` produces `./executable` (a program matching the
documented behavior of the original; see README.md / the man page).

The build SUCCEEDS, but the produced binary misbehaves at runtime. Evidence:

```
{evidence}
```

The usual cause is a main loop that redraws without its frame delay (a
missing/removed sleep, usleep, napms, or timer between frames), so the program
floods the terminal with output instead of animating at a paced rate. Find the
main loop, restore correct frame pacing (the documentation describes the
update speed options), rebuild with `bash compile.sh`, and verify:
`timeout 2 ./executable | wc -c` run in a terminal should produce output on
the order of tens of kilobytes, not megabytes.
Do not start over; repair what exists."""


def build_gate_and_repair(image: str, patch: str, *, model: str, step_limit: int = 150,
                          time_limit_s: int = 2400, command_timeout: int = 300) -> tuple[str, dict]:
    """Iteration-1 mechanism (team-goal loop): mechanical build gate + repair.

    Apply the integrated patch to a fresh cleanroom tree; if `compile.sh`
    builds `./executable`, pass through unchanged. Otherwise run ONE repair
    agent in that tree with the build error as evidence (Coop+Repair's
    mechanism with the BUILD as the gate). Returns (patch, meta)."""
    from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

    env = DockerEnv(image, repo_path=WORKSPACE, network="none", user="agent", keepalive="24h")
    try:
        subprocess.run(["docker", "exec", "-u", "root", env.name, "bash", "-c",
                        "chown -R agent:agent /workspace 2>/dev/null; true"], capture_output=True)
        env.execute("printf 'executable\nshared/\n' >> .git/info/exclude")
        # Reference behavior must be captured BEFORE the first build: the
        # pristine tree's ./executable is the (execute-only) reference binary.
        ref_rate = _rate(env)
        if patch.strip():
            env.write_file("/tmp/final.patch", patch)
            env.execute(
                "git apply --whitespace=nowarn /tmp/final.patch 2>/dev/null"
                " || git apply --3way /tmp/final.patch 2>/dev/null"
                " || git apply --reject /tmp/final.patch 2>/dev/null || true"
            )

        def gate_state() -> tuple[str, str] | None:
            """None if the tree passes; else (failure_kind, evidence)."""
            g = env.execute(BUILD_GATE, timeout=600)
            if g.exit_code != 0:
                ev = env.execute("tail -c 3000 /tmp/build.log 2>/dev/null; ls compile.sh 2>&1").stdout[-3000:]
                return "build", ev
            # Iteration 5: builds-but-floods is a gate failure too (coopgitc2-r3
            # shipped an 11.3MB/2s busy-loop that DNF'd the evaluator).
            cand_rate = _rate(env)
            if _is_firehose(cand_rate, ref_rate):
                return "firehose", (
                    f"`timeout 2 ./executable` in a terminal wrote {cand_rate} bytes; "
                    f"the reference binary writes ~{ref_rate} bytes in the same window. "
                    f"The binary floods output instead of animating at a paced rate."
                )
            return None

        failure = gate_state()
        if failure is None:
            return patch, {"repair": "not_needed"}
        # Iteration 3: repair is stochastic at 9B (k=3 finding: 1 success in 4
        # firings, and a failed repair was submitted 3 times → three zeros).
        # Retry a failed repair once with fresh evidence, and NEVER trust the
        # repair agent's self-report — submit the mechanically best candidate.
        meta = {"repair": "ran", "attempts": []}
        candidates = [patch]
        for attempt in range(2):
            kind, evidence = failure
            task_tpl = BEHAVIOR_REPAIR_TASK if kind == "firehose" else REPAIR_TASK
            res = run_mini_swe_agent(
                env,
                task=task_tpl.format(evidence=evidence),
                agent_id=f"repair{attempt + 1}",
                role="integrator",
                model_name=model,
                step_limit=step_limit,
                cost_limit=5.0,
                command_timeout=command_timeout,
                time_limit_s=time_limit_s,
            )
            env.execute(r"find . -path ./.git -prune -o \( -name '*.rej' -o -name '*.orig' \) -print0 | xargs -0 -r rm -f")
            candidates.append(env.git_diff())
            failure = gate_state()
            meta["attempts"].append({"steps": res.steps, "status": res.status,
                                     "gate_after": 0 if failure is None else failure[0]})
            if failure is None:
                break
        scores = [pb_score(image, c) for c in candidates]
        best = max(range(len(candidates)), key=lambda i: scores[i])
        meta["candidate_scores"] = scores
        meta["chosen"] = best
        return candidates[best], meta
    finally:
        env.cleanup()


def make_submission(image: str, patch: str, out_dir: Path) -> None:
    """Apply the integrated patch to a fresh cleanroom tree and tar it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = DockerEnv(image, repo_path=WORKSPACE, network="none", keepalive="24h")
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
        subprocess.run(
            ["docker", "cp", f"{env.name}:/tmp/submission.tar.gz", str(out_dir / "submission.tar.gz")],
            check=True,
        )
    finally:
        env.cleanup()


PROBES = [
    "./executable --help",
    "./executable -h",
    "./executable --version",
    "./executable -V",
    "./executable --definitely-not-a-flag",
]


def pb_score(image: str, patch: str) -> tuple[int, int, int, int]:
    """Mechanical candidate score, compared lexicographically:
    (build_ok_tier, not_firehose, quit_matches_ref, flag_probes_matched).

    Signals are all self-available: does compile.sh build, does the binary's
    pty output rate stay near the reference's (iteration 5 — a flooding
    busy-loop kills the evaluator), does it exit on 'q' like the reference,
    and do fast flag probes (--help/--version/invalid-flag output + exit
    codes) match the reference binary, which agents may legitimately run."""
    if not patch.strip():
        return (-2, 0, 0, 0)
    env = DockerEnv(image, repo_path=WORKSPACE, network="none", keepalive="24h")
    try:
        ref = [env.execute(f"timeout 20 {p} 2>&1; echo RC=$?", timeout=40).stdout for p in PROBES]
        ref_rate, ref_quit = _rate(env), _quit_rc(env)
        env.write_file("/tmp/c.patch", patch)
        env.execute(
            "git apply --whitespace=nowarn /tmp/c.patch 2>/dev/null"
            " || git apply --3way /tmp/c.patch 2>/dev/null"
            " || git apply --reject /tmp/c.patch 2>/dev/null || true"
        )
        b = env.execute("rm -f ./executable; [ -f ./compile.sh ] && bash ./compile.sh", timeout=600)
        if b.exit_code != 0 or env.execute("test -x ./executable").exit_code != 0:
            return (-1, 0, 0, 0)
        cand = [env.execute(f"timeout 20 {p} 2>&1; echo RC=$?", timeout=40).stdout for p in PROBES]
        return (
            1,
            0 if _is_firehose(_rate(env), ref_rate) else 1,
            1 if _quit_rc(env) == ref_quit else 0,
            sum(a.strip() == c.strip() for a, c in zip(ref, cand)),
        )
    finally:
        env.cleanup()


def pb_selector(image: str):
    """Board Best-of-2's mechanical selector, adapted to ProgramBench."""
    def select(cands) -> int:
        scores = [pb_score(image, c.integrated.patch if c.integrated else "") for c in cands]
        print(f"[selector] scores={scores}")
        return max(range(len(scores)), key=lambda i: scores[i])

    return select


def build_assignments(arm: str) -> list:
    if arm == "solo":
        return [Assignment(agent_id="agent1", role="lead", feature_id=None, task=TASK)]
    if arm in ("divide", "dividebo2"):
        return [
            Assignment(agent_id="agent1", role="lead", feature_id=None, task=TASK + NEGOTIATE_LEAD),
            Assignment(agent_id="agent2", role="member", feature_id=None, task=TASK + NEGOTIATE_MEMBER),
        ]
    return [
        Assignment(agent_id="agent1", role="lead", feature_id=None, task=TASK),
        Assignment(agent_id="agent2", role="member", feature_id=None, task=TASK),
    ]


def run_team_once(arm: str, img: str, *, step_limit: int, agent_time_limit: int | None):
    """One full team run for `arm`; returns (patch, RunResult). Own run_id →
    own bus, own scratchpad/git-share volumes (safe to call concurrently)."""
    run_id = uuid.uuid4().hex[:8]
    assignments = build_assignments(arm)
    base = arm.replace("bo2", "")  # dividebo2 attempts run the divide config
    spec = TeamSpec(
        run_id=run_id,
        repo="programbench",
        task_id=0,
        features=[],
        assignments=assignments,
        shared_workspace=True,
        worker="mini_swe",
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Qwen/Qwen3.5-9B"),
        seed_prior=False,
        coop_tools=base in ("coopgit", "coopgitc2", "teamfull", "divide"),
        tool_protocol=base == "divide",
        wait_protocol=base == "divide",
        agent_time_limit=agent_time_limit,
        git_share=base in ("coopgit", "coopgitc2"),
        coordinator=base == "coopgitc2",
        task_board=base in ("teamfull", "divide"),
        team_roles=base in ("teamfull", "divide"),
        temperature=0.0,
    )
    vols = []
    if spec.git_share:
        vols.append(f"cbs{run_id}:/cbshared")
    if spec.team_roles:
        vols.append(f"cbt{run_id}:{WORKSPACE}/shared")

    def make_env(_id: str, _v=vols or None) -> DockerEnv:
        env = DockerEnv(img, repo_path=WORKSPACE, volumes=_v, network="none", user="agent", keepalive="24h")
        # Shared volumes are created root-owned; agents run as uid "agent" and
        # must be able to write PLAN.md / patch exports / the git-share repo.
        subprocess.run(
            ["docker", "exec", "-u", "root", env.name, "bash", "-c",
             "chown -R agent:agent /workspace/shared /cbshared 2>/dev/null; true"],
            capture_output=True,
        )
        # The reference binary is execute-only, which makes `git add -A` FATAL
        # ("unable to index file 'executable'") and empties every diff. Keep it
        # (and the cross-container scratchpad) out of the index via the
        # repo-local exclude file — invisible to the agent's .gitignore.
        env.execute("printf 'executable\nshared/\n' >> .git/info/exclude")
        return env

    harness = UnifiedHarness(bus=InMemoryBus(run_id), step_limit=step_limit, command_timeout=300)
    res = harness.run(spec, env_factory=make_env)
    return (res.integrated.patch or ""), res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="abishekvashok__cmatrix.5c082c6")
    ap.add_argument("--arm", choices=["solo", "coopgit", "teamfull", "divide", "coopgitc2", "dividebo2"], required=True)
    ap.add_argument("--rep", default="a")
    ap.add_argument("--step-limit", type=int, default=100)
    ap.add_argument("--repair", action="store_true", help="iteration 1: mechanical build gate + repair agent on the integrated tree")
    ap.add_argument("--agent-time-limit", type=int, default=0, help="wall-clock cap (s) per team agent; 0 = uncapped")
    ap.add_argument("--runs-dir", default="runs")
    args = ap.parse_args()

    img = image_for(args.instance)
    run_name = f"pb-{args.arm}-{args.rep}"
    out_root = Path(args.runs_dir) / run_name / args.instance
    t0 = time.time()
    bo2_meta = {}

    if args.arm == "dividebo2":
        # Iteration 2: Board Best-of-2 transplant — each attempt is a FULL
        # negotiated-division team (isolated bus + volumes); the harness
        # mechanically selects the better team output (pb_score).
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(run_team_once, args.arm, img,
                              step_limit=args.step_limit,
                              agent_time_limit=args.agent_time_limit or None)
                    for _ in range(2)]
            attempts = [f.result() for f in futs]
        scores = [pb_score(img, patch) for patch, _ in attempts]
        chosen = max(range(2), key=lambda i: scores[i])
        print(f"[{run_name}] bo2 scores={scores} chosen={chosen}")
        patch, res = attempts[chosen]
        bo2_meta = {"bo2_scores": scores, "bo2_chosen": chosen,
                    "bo2_all_steps": [r.total_steps for _, r in attempts]}
    else:
        patch, res = run_team_once(args.arm, img,
                                   step_limit=args.step_limit,
                                   agent_time_limit=args.agent_time_limit or None)

    repair_meta = {}
    if args.repair:
        patch, repair_meta = build_gate_and_repair(img, patch, model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Qwen/Qwen3.5-9B"))
        print(f"[{run_name}] repair: {repair_meta}")
    dur = time.time() - t0
    print(f"[{run_name}] duration={dur:.0f}s steps={res.total_steps} patch_bytes={len(patch)}")
    if not patch.strip():
        print(f"[{run_name}] EMPTY PATCH — writing empty submission for the record")
    make_submission(img, patch, out_root)
    import json as _json
    traj_dir = out_root / "trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    for aid, r in res.seeds.items():
        (traj_dir / f"{aid}.json").write_text(_json.dumps({
            "agent_id": aid, "status": r.status, "steps": r.steps,
            "error": r.error, "messages": r.messages,
        }, indent=1))
    (out_root / "integrated.patch").write_text(patch)
    (out_root / "metrics.json").write_text(_json.dumps({**res.metrics, **repair_meta, **bo2_meta}, indent=1, default=str))
    (out_root / "run_meta.txt").write_text(
        f"arm={args.arm} rep={args.rep} duration_s={dur:.0f} steps={res.total_steps} "
        f"patch_bytes={len(patch)}\n"
    )
    print(f"[{run_name}] submission at {out_root}/submission.tar.gz")


if __name__ == "__main__":
    main()
