"""Harness-level verification: validate, score, and repair implemented ONCE.

These functions are part of the harness (they are what the self-evolution
program optimizes), so they are identical across benchmarks. Adapters
contribute only declarative hints:

  build_artifact    file the build must (re)produce (e.g. "executable"),
                    or None when the benchmark has no single build product
  reference_binary  path of a runnable reference implementation provided by
                    the task (e.g. "./executable"), or None

Everything else is discovered mechanically in the workspace:

  build command     first match of: ./compile.sh, Makefile, Cargo.toml
                    (offline), go.mod / *.go, pyproject/setup.py
                    (compileall). Same list everywhere.
  behavior probes   only when a reference binary exists: generic CLI
                    conventions (--help/-h/--version/-V/invalid flag), an
                    output-rate probe, and a quit probe — each run on the
                    reference FIRST, so a probe that does not apply to a
                    given program compares equal and is neutral.

validate(env)  pre-submission check in the agent's own container; None =
               valid, else error text injected back to the agent.
score(...)     comparable tuple ranking one candidate patch in a fresh
               container; richer when a reference binary exists.
repair(env)    None = integrated tree healthy, else a complete repair-agent
               prompt with evidence.
"""

from __future__ import annotations

import re

# -- mechanical build discovery ---------------------------------------------

_DISCOVER = (
    'if [ -f ./compile.sh ]; then echo "bash ./compile.sh"; '
    'elif [ -f Makefile ] || [ -f makefile ]; then echo "make"; '
    'elif [ -f Cargo.toml ]; then echo "cargo build --release --offline"; '
    'elif [ -f go.mod ] || ls ./*.go >/dev/null 2>&1; then echo "go build ./..."; '
    'elif [ -f pyproject.toml ] || [ -f setup.py ]; then echo "python3 -m compileall -q ."; '
    'fi'
)


def discover_build(env) -> str:
    """The workspace's build command, or "" when none is recognizable."""
    return env.execute(_DISCOVER, timeout=30).stdout.strip()


def _build(env, artifact: str | None) -> str | None:
    """Run the discovered build; None = success, else evidence text.

    With an artifact declared, the build must (re)produce it fresh — a
    build that exits 0 without producing its artifact is a failure."""
    cmd = discover_build(env)
    if not cmd:
        return "No build entrypoint found (expected one of: compile.sh, Makefile, Cargo.toml, go.mod, pyproject.toml)."
    marker = "touch /tmp/.v_marker; " if artifact else ""
    r = env.execute(f"{marker}{cmd} >/tmp/v_build.log 2>&1", timeout=600)
    if r.exit_code != 0:
        tail = env.execute("tail -c 2500 /tmp/v_build.log").stdout
        return f"Build failed (`{cmd}`):\n{tail}"
    if artifact:
        ok = env.execute(f"[ -x ./{artifact} ] && [ ./{artifact} -nt /tmp/.v_marker ]")
        if ok.exit_code != 0:
            return (f"`{cmd}` exited 0 but did not (re)build ./{artifact} — "
                    f"the build must produce a fresh ./{artifact}.")
    return None


# -- generic team merge (git substrate) -------------------------------------

_MERGE = (
    "if git remote get-url shared >/dev/null 2>&1; then "
    "git fetch shared 2>/dev/null; "
    "AID=$(cat /tmp/.agent_id 2>/dev/null); "
    "git add -A 2>/dev/null; "
    "if grep -rl '^<<<<<<< ' --exclude-dir=.git . >/dev/null 2>&1; then "
    "echo 'UNRESOLVED_CONFLICT_MARKERS in:'; grep -rl '^<<<<<<< ' --exclude-dir=.git . | head -10; "
    "else "
    "git -c user.name=agent -c user.email=a@t commit -q -m wip 2>/dev/null; "
    "[ -f .git/MERGE_HEAD ] && git -c user.name=agent -c user.email=a@t commit -q --no-edit 2>/dev/null; "
    "ok=1; "
    "for b in $(git for-each-ref --format='%(refname:short)' refs/remotes/shared/ | sed 's|shared/||'); do "
    "[ \"$b\" = \"$AID\" ] && continue; "
    "if ! git -c user.name=agent -c user.email=a@t merge --no-edit shared/$b >/tmp/.mg 2>&1; then "
    "echo \"TEAMMATE_MERGE_CONFLICT merging shared/$b — conflicted files:\"; "
    "git diff --name-only --diff-filter=U | head -10; ok=0; break; fi; done; "
    "[ $ok = 1 ] && echo MERGED_OK; "
    "fi; "
    "else echo MERGED_OK; fi"
)


# -- reference-comparative behavior probes ----------------------------------

_FLAG_PROBES = ["--help", "-h", "--version", "-V", "--definitely-not-a-flag"]
_FIREHOSE_FLOOR = 200_000  # bytes/2s


def _rate(env, ref: str) -> int:
    out = env.execute(
        f"TERM=xterm timeout 2 script -qec '{ref}' /dev/null </dev/null 2>/dev/null | wc -c",
        timeout=30).stdout.strip().splitlines()
    try:
        return int(out[-1])
    except (ValueError, IndexError):
        return 0


def _quit_rc(env, ref: str) -> int:
    out = env.execute(
        f"printf q | TERM=xterm timeout 8 script -qec '{ref}' /dev/null >/dev/null 2>&1; echo RC=$?",
        timeout=30).stdout
    m = re.search(r"RC=(\d+)", out)
    return int(m.group(1)) if m else -1


def _flag_outputs(env, ref: str) -> list[str]:
    return [env.execute(f"timeout 20 {ref} {f} 2>&1; echo RC=$?", timeout=40).stdout
            for f in _FLAG_PROBES]


# -- the three verification functions ---------------------------------------

def validate(env, *, merged: bool = False, build_artifact: str | None = None) -> str | None:
    """Pre-submission check in the agent's own container.

    merged=True first integrates every teammate branch from the shared git
    remote (conflicts go back to the agent); then the discovered build must
    succeed (and refresh build_artifact when declared)."""
    if merged:
        out = env.execute(_MERGE, timeout=300).stdout
        if "MERGED_OK" not in out:
            return (
                "Before finishing you must MERGE your teammates' work into "
                "your tree. The gate attempted it:\n" + out[-2000:] +
                "\nOpen the conflicted files, reconcile BOTH sides (keep both "
                "contributions where possible), remove the <<<<<<< ======= "
                ">>>>>>> markers, `git add -A`, then rebuild and submit again."
            )
    err = _build(env, build_artifact)
    if err is None:
        return None
    return (err + "\nReminder: there is NO network access during builds — "
            "package downloads fail; use the standard library or sources "
            "vendored in-tree. Fix the problem, verify the build, then "
            "submit again.")


def score(image: str, patch: str, *, env_factory, build_artifact: str | None = None,
          reference_binary: str | None = None) -> tuple:
    """Rank one candidate patch in a fresh container.

    Always: (-2,)+pad empty, (-1,)+pad unbuildable, (1, ...) builds. With a
    reference binary the remaining elements are behavioral comparisons
    (not-firehose, quit parity, flag-output matches); without one they fall
    back to patch size (weak, and honestly so)."""
    pad = (0, 0, 0)
    if not patch.strip():
        return (-2, *pad)
    env = env_factory()
    try:
        ref_state = None
        if reference_binary:
            ref_state = (_flag_outputs(env, reference_binary),
                         _rate(env, reference_binary),
                         _quit_rc(env, reference_binary))
        env.write_file("/tmp/c.patch", patch)
        env.execute(
            "git apply --whitespace=nowarn /tmp/c.patch 2>/dev/null"
            " || git apply --3way /tmp/c.patch 2>/dev/null"
            " || git apply --reject /tmp/c.patch 2>/dev/null || true"
        )
        if build_artifact:
            env.execute(f"rm -f ./{build_artifact}")
        if _build(env, build_artifact) is not None:
            return (-1, *pad)
        if ref_state is None or reference_binary is None:
            return (1, min(len(patch), 100_000), 0, 0)
        ref_flags, ref_rate, ref_quit = ref_state
        cand_rate = _rate(env, reference_binary)
        cand_flags = _flag_outputs(env, reference_binary)
        return (
            1,
            0 if cand_rate > max(10 * ref_rate, _FIREHOSE_FLOOR) else 1,
            1 if _quit_rc(env, reference_binary) == ref_quit else 0,
            sum(a.strip() == c.strip() for a, c in zip(ref_flags, cand_flags, strict=False)),
        )
    finally:
        env.cleanup()


_REPAIR_BUILD = """You are the integration repairer for a team that just merged its work into
THIS tree. The merged tree does not build. Evidence:

```
{evidence}
```

Fix the build WITHOUT discarding any teammate's work:
- resolve any merge conflict markers (<<<<<<< ======= >>>>>>>) by RECONCILING
  both sides (keep both contributions where possible);
- reconcile duplicate or missing definitions across files;
- create or fix the build entrypoint if it is missing or wrong;
- then rebuild and confirm it succeeds.
Do not start over; repair what exists."""

_REPAIR_BEHAVIOR = """You are the integration repairer for a team that just merged its work into
THIS tree. The build succeeds, but the produced program floods output far
faster than the reference implementation. Evidence:

```
{evidence}
```

The usual cause is a main loop missing its pacing delay (a removed sleep,
usleep, napms, or timer between iterations). Find the loop, restore correct
pacing, rebuild, and verify the output rate resembles the reference's.
Do not start over; repair what exists."""


def repair(env, *, build_artifact: str | None = None,
           reference_binary: str | None = None) -> str | None:
    """Inspect an integrated tree: None = healthy, else a repair prompt.

    Reference output rate is compared against /tmp/.ref_rate when present
    (captured by env setup before any build overwrote the reference)."""
    err = _build(env, build_artifact)
    if err is not None:
        return _REPAIR_BUILD.format(evidence=err[-3000:])
    if reference_binary:
        try:
            ref_rate = int(env.execute("cat /tmp/.ref_rate 2>/dev/null").stdout.strip() or 0)
        except ValueError:
            ref_rate = 0
        cand_rate = _rate(env, reference_binary)
        if cand_rate > max(10 * ref_rate, _FIREHOSE_FLOOR):
            return _REPAIR_BEHAVIOR.format(evidence=(
                f"candidate writes {cand_rate} bytes to a terminal in 2s; "
                f"the reference writes ~{ref_rate} bytes in the same window."))
    return None
