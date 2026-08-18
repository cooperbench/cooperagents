"""The unified harness orchestrator.

**Hard constraint: every agent runs in its OWN container/environment** — agents
never share a live workspace. The coordinated team path (``_run_isolated``)
seeds each fresh container with teammates' cumulative diff so an agent still
builds on prior work, but execution is always isolated per agent.

One ``run`` call drives a whole team on one problem.  Unlike a two-level
design (a team harness that wraps an opaque agent harness), the orchestrator
and every agent share one :class:`TeamBus`, so the team can reshape itself
while it works:

  1. **Seed** the team from the spec — one agent per assignment
     (``N tasks for N agents``) or a lead + members on a shared objective
     (``one task for the whole team``).
  2. Run every seed agent concurrently, each in its own environment.
  3. A **supervisor** drains the spawn queue: when an agent calls
     ``spawn_helper``, it launches a fresh helper agent on that sub-task —
     up to ``max_agents`` — which itself may recruit further help.
  4. When the pool goes idle, harvest patches + coordination/spawn metrics.

The result is eval-ready: per-feature seed patches are what CooperBench
scores; helper output reaches the score through the agent that integrates
it (the conventional lead-merges-the-team pattern).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from cooperagents.agent import Agent
from cooperagents.bus.base import TeamBus
from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.base import Environment
from cooperagents.llm import LLMClient
from cooperagents.metrics import coordination_metrics, spawn_metrics
from cooperagents.patching import strip_test_sections
from cooperagents.planner import ancestors, plan_decomposition, topo_levels
from cooperagents.types import AgentResult, Assignment, RunResult, SubTask, TeamSpec

Planner = Callable[[list[tuple[int, str]], "str | None"], "tuple[list[SubTask], str]"]


def _seed_patch(env: Environment, patch: str) -> None:
    """Apply a teammate/ancestor delta into a fresh container and commit it, so
    the agent starts from a clean coherent base (a dirty tree confuses it)."""
    if not patch.strip():
        return
    env.write_file(".cb_prior.patch", patch)
    env.execute(
        "git apply --whitespace=nowarn .cb_prior.patch 2>/dev/null "
        "|| git apply --3way .cb_prior.patch 2>/dev/null "
        "|| git apply --reject .cb_prior.patch 2>/dev/null || true"
    )
    env.execute(
        "rm -f .cb_prior.patch && git add -A && "
        "git -c user.email=team@cooperagents.local -c user.name=cooperagents commit -q -m 'seed' || true"
    )


_GIT_ID = "-c user.email=team@cooperagents.local -c user.name=cooperagents"


_HEALTH_CMD = """
if [ -f go.mod ] && command -v go >/dev/null 2>&1; then
  go build ./... >/dev/null 2>&1 || exit 1
fi
if command -v python3 >/dev/null 2>&1; then
python3 - <<'CB_HEALTH_EOF'
import ast, pathlib, sys
bad = []
for p in pathlib.Path(".").rglob("*.py"):
    s = str(p)
    if ".git/" in s or s.startswith(".cb_"):
        continue
    try:
        ast.parse(p.read_bytes(), filename=s)
    except SyntaxError:
        bad.append(s)
    except Exception:
        pass
sys.exit(1 if bad else 0)
CB_HEALTH_EOF
fi
"""


def _tree_health(env: Environment) -> bool:
    """Mechanical repo health check for the Q1 do-no-harm gate: build (go) or
    AST-parse every source file (python — no bytecode, so no __pycache__ noise
    in the diff). Only a DEFINITE defect (exit 1) counts as broken; timeouts or
    missing toolchains read as healthy — the gate must never discard work it
    cannot judge."""
    res = env.execute(_HEALTH_CMD, timeout=180)
    return res.exit_code != 1


def _apply_commit(env: Environment, patch: str, msg: str) -> None:
    env.write_file(".cb_d.patch", patch)
    env.execute(
        "git apply --whitespace=nowarn .cb_d.patch 2>/dev/null "
        "|| git apply --3way .cb_d.patch 2>/dev/null || true; rm -f .cb_d.patch"
    )
    env.execute(f"git add -A && git {_GIT_ID} commit -q -m '{msg}' || true")


def _threeway_merge(env: Environment, deltas: list[str]) -> tuple[bool, str]:
    """Real 3-way merge of independent branch deltas against the base commit.

    Each delta becomes a branch off the base; they are merged one by one into an
    accumulator. A true 3-way merge (unlike ``git apply --check``) resolves
    non-overlapping edits to the same file and only conflicts on genuine region
    overlaps — the correct separability test. Returns (conflict, merged_diff).
    """
    nz = [d for d in deltas if d.strip()]
    if not nz:
        return False, ""
    base = (env.execute("git rev-parse HEAD").stdout or "").strip() or "HEAD"
    env.execute(f"git checkout -q -B _acc {base}")
    _apply_commit(env, nz[0], "d0")
    for i, d in enumerate(nz[1:], 1):
        env.execute(f"git checkout -q -B _b{i} {base}")
        _apply_commit(env, d, f"d{i}")
        env.execute("git checkout -q _acc")
        m = env.execute(f"git {_GIT_ID} merge --no-edit _b{i} 2>&1")
        if m.exit_code != 0:  # genuine region overlap → coupled
            env.execute("git merge --abort 2>/dev/null || true")
            return True, ""
    return False, strip_test_sections(env.git_diff())

EnvFactory = Callable[[str], Environment]
LLMFactory = Callable[[str, str], LLMClient]


_SPEC_FIDELITY = (
    "IMPORTANT — API fidelity: a hidden automated test suite grades your work by "
    "referencing the EXACT public names and signatures the spec describes "
    "(types, functions, methods, struct fields, parameters, constants). Implement "
    "them verbatim as named or strongly implied by the spec — do not rename, "
    "abbreviate, pluralize differently, or invent API surface. Match the spec's "
    "wording when choosing identifiers.\n\n"
)


_TDD_PREAMBLE = (
    "WORKFLOW — verify as you go: BEFORE writing code, derive from the spec a short "
    "checklist of concrete acceptance criteria (exact public API names, expected "
    "behaviors, edge cases). For each, write a THROWAWAY local check you can run "
    "(a `python -c ...` one-liner or a scratch script OUTSIDE any tests/ directory) "
    "— never add or edit files in the project's real test suite. Implement until "
    "every check passes, then DELETE your scratch checks before you finish. Do not "
    "submit until your own checks confirm each acceptance criterion holds.\n\n"
)


_MINE_CONVENTIONS = (
    "WORKFLOW — mine conventions first: BEFORE editing, inspect how this repo "
    "already does things in the area you will touch. Grep for the public symbols "
    "the spec mentions and read the neighbouring code, existing tests, and similar "
    "features to learn the naming, signatures, error handling, and patterns in use. "
    "Mirror those existing conventions in your implementation rather than inventing "
    "new ones, so your feature integrates cleanly with what is already there.\n\n"
)


_CONTRACT_PROMPT = (
    "Two features below will be implemented IN PARALLEL by separate engineers on "
    "separate copies of the same repository; their diffs are merged afterwards. "
    "Write the SHARED INTERFACE CONTRACT they must both follow so the merge is "
    "clean: the exact public names, signatures, and file locations of anything "
    "both features touch or one provides for the other (new helpers, config "
    "fields, registration points). Be concrete and minimal — a short bulleted "
    "list, no prose. If the features are fully independent, list the files each "
    "should keep to.\n\nFEATURES:\n\n{specs}"
)


def _build_contract(assignments: list[Assignment], model: str | None = None) -> str:
    """TK1/Q6: one planner call producing the shared-interface contract.

    Offline-safe: no creds / any error → empty string (feature silently off)."""
    from cooperagents.planner import _default_planner_complete

    fn = _default_planner_complete(model, None, None)
    if fn is None:
        return ""
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(f"### Feature {i + 1}\n{t[:3000]}" for i, t in enumerate(specs))
    try:
        out = (fn(_CONTRACT_PROMPT.format(specs=bundle)) or "").strip()
    except Exception:  # noqa: BLE001 - contract is best-effort
        return ""
    return out[:1800]


class _Coordinator:
    """C2 (user-proposed): a live monitor over the parallel agents.

    MECHANICAL triggers decide WHEN to intervene (LLM judgment at 9B loses to
    mechanics everywhere it was measured); the LLM composes only the nudge
    text; injection rides the existing pushed team_poller channel. Triggers:
      LOOP      — >=4 of the last 6 commands are near-duplicates
      COLLISION — both agents' dirty-file sets intersect
      STALL     — last 4 observations are identical errors
    Max 3 nudges per agent; a static fallback nudge is used when the LLM
    composer is unavailable (offline-safe)."""

    def __init__(self, envs: dict[str, Environment], model: str | None = None) -> None:
        self._envs = envs
        self._agents: dict[str, Any] = {}
        self._queues: dict[str, list[str]] = {}
        self._sent: dict[str, int] = {}
        self._model = model
        self._stop = threading.Event()
        self._fired: list[dict] = []  # attribution log

    def register(self, agent_id: str, agent: Any) -> None:
        self._agents[agent_id] = agent
        self._queues.setdefault(agent_id, [])
        self._sent.setdefault(agent_id, 0)

    def drain(self, agent_id: str) -> list[str]:
        q = self._queues.get(agent_id, [])
        out, q[:] = list(q), []
        return out

    def events(self) -> list[dict]:
        return list(self._fired)

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _commands(agent) -> list[str]:
        cmds = []
        for m in getattr(agent, "messages", [])[-24:]:
            if m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                try:
                    import json as _json

                    cmds.append(_json.loads(tc["function"]["arguments"]).get("command", ""))
                except Exception:  # noqa: BLE001
                    pass
        return cmds[-8:]

    def _detect(self, aid: str, agent) -> str | None:
        cmds = self._commands(agent)
        if len(cmds) >= 6:
            heads = [" ".join(c.split()[:2]) for c in cmds[-6:]]
            if max(heads.count(h) for h in set(heads)) >= 4:
                return "LOOP"
        obs = [m.get("content", "")[:120] for m in getattr(agent, "messages", [])[-8:] if m.get("role") == "tool"]
        if len(obs) >= 4 and len(set(obs[-4:])) == 1 and ("rror" in obs[-1] or "returncode\": 1" in obs[-1]):
            return "STALL"
        try:
            mine = set(self._envs[aid].execute("git status --porcelain | awk '{print $2}'").stdout.split())
            for oid, oenv in self._envs.items():
                if oid == aid:
                    continue
                theirs = set(oenv.execute("git status --porcelain | awk '{print $2}'").stdout.split())
                overlap = (mine & theirs) - {".cb_checks"}
                if overlap:
                    return "COLLISION:" + ",".join(sorted(overlap)[:3])
        except Exception:  # noqa: BLE001
            pass
        return None

    _FALLBACK = {
        "LOOP": "You appear to be repeating near-identical commands without progress. Step back: state "
        "your goal in one sentence, then take a DIFFERENT approach (e.g. rewrite the whole file "
        "instead of patching it line by line).",
        "STALL": "Your last several commands returned the same error. Do not retry it again — read the "
        "error carefully and fix its CAUSE, or route around it another way.",
        "COLLISION": "You and your teammate are editing the same file(s): {files}. Confine your edits to "
        "clearly separate regions and reuse their public names, or your merged work will conflict.",
    }

    def _compose(self, kind: str, agent) -> str:
        base = kind.split(":")[0]
        files = kind.split(":", 1)[1] if ":" in kind else ""
        fallback = self._FALLBACK[base].format(files=files)
        try:
            from cooperagents.planner import _default_planner_complete

            fn = _default_planner_complete(self._model, None, None)
            if fn is None:
                return fallback
            tail = "\n".join(self._commands(agent)[-5:])[:1200]
            out = fn(
                f"An agent shows this issue: {base}. Its recent commands:\n{tail}\n\n"
                "Write ONE corrective instruction to it (max 40 words, imperative, specific)."
            )
            out = (out or "").strip()
            return out[:400] if out else fallback
        except Exception:  # noqa: BLE001
            return fallback

    def run(self) -> None:
        while not self._stop.wait(20):
            for aid, agent in list(self._agents.items()):
                if self._sent.get(aid, 0) >= 3:
                    continue
                kind = self._detect(aid, agent)
                if kind:
                    nudge = self._compose(kind, agent)
                    self._queues[aid].append(f"[coordinator] {nudge}")
                    self._sent[aid] += 1
                    self._fired.append({"agent": aid, "kind": kind.split(":")[0]})


_GITSHARE = "/cbshared/repo.git"


class _GitShareSync:
    """TK-git: harness-side push of each agent's working tree to a per-agent
    branch on the shared bare repository. `git stash create` produces a commit
    object from the dirty tree while leaving the agent's HEAD, index, and
    working tree unchanged; a clean tree pushes HEAD."""

    def __init__(self, envs: dict[str, Environment]) -> None:
        self._envs = envs
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(45):
            for aid, env in list(self._envs.items()):
                try:
                    env.execute(
                        "C=$(git stash create 2>/dev/null); "
                        f"git push -q -f shared ${{C:-HEAD}}:refs/heads/{aid} 2>/dev/null || true",
                        timeout=60,
                    )
                except Exception:  # noqa: BLE001 - sync must never disturb the run
                    pass


class _TeammatePoller:
    """TK2/Q9: pushed teammate awareness for parallel agents.

    poll() (called by the agent loop each step via the ``team_poller`` hook)
    reports which files each TEAMMATE is currently editing — but only when
    that set CHANGES, so the token cost stays near zero."""

    def __init__(self, self_id: str, envs: dict[str, Environment]) -> None:
        self._self = self_id
        self._envs = envs
        self._last: dict[str, str] = {}
        self._board_bus = None
        self._board_last = ""
        self._coordinator = None
        self._gitshare = False
        self._gitshare_last: dict[str, str] = {}

    def watch_board(self, bus) -> None:
        self._board_bus = bus

    def watch_coordinator(self, coord) -> None:
        self._coordinator = coord

    def watch_gitshare(self) -> None:
        self._gitshare = True

    def poll(self) -> str:
        notes: list[str] = []
        for aid, env in self._envs.items():
            if aid == self._self:
                continue
            try:
                out = env.execute("git status --porcelain 2>/dev/null | awk '{print $2}' | head -20").stdout
            except Exception:  # noqa: BLE001 - a dead teammate env must not kill this agent
                continue
            files = sorted(x for x in out.split() if x and not x.startswith(".cb_"))
            cur = ", ".join(files)
            if cur and cur != self._last.get(aid):
                self._last[aid] = cur
                notes.append(
                    f"[team] {aid} is currently editing: {cur}. Avoid colliding edits to these "
                    "files; if you must touch them, reuse that teammate's public names."
                )
        if self._gitshare:
            env = self._envs.get(self._self)
            for aid in self._envs:
                if aid == self._self or env is None:
                    continue
                try:
                    env.execute(f"git fetch -q shared {aid} 2>/dev/null || true", timeout=45)
                    files = env.execute(
                        f"git diff --name-only HEAD...shared/{aid} 2>/dev/null | grep -v '^.cb_' | head -8"
                    ).stdout.split()
                    cur = ", ".join(sorted(files))
                    if cur and cur != self._gitshare_last.get(aid):
                        self._gitshare_last[aid] = cur
                        notes.append(
                            f"[git] {aid}'s in-progress branch is fetched locally as shared/{aid} "
                            f"(changed files: {cur}). Inspect: `git diff HEAD...shared/{aid} -- <file>`. "
                            f"Take their version of a file: `git checkout shared/{aid} -- <file>`. Reuse "
                            "their public names instead of inventing parallel ones."
                        )
                except Exception:  # noqa: BLE001
                    pass
        if self._coordinator is not None:
            notes.extend(self._coordinator.drain(self._self))
        if self._board_bus is not None:
            try:
                rows = self._board_bus.list_tasks()
                cur = "; ".join(
                    f"{t.get('owner') or '?'}:{t.get('status','open')}:{t.get('title','')[:40]}"
                    for t in rows
                    if (t.get("owner") or "") != self._self
                )
                if cur and cur != self._board_last:
                    self._board_last = cur
                    notes.append(f"[board] teammate tasks: {cur}")
            except Exception:  # noqa: BLE001
                pass
        return "\n".join(notes)


def _tree_health_behavioral(env: Environment) -> bool:
    """Q10: BEHAVIORAL merge gate. The AST gate misses semantically-broken
    "clean" 3-way merges (q5g regression: 15.7 -> 13.3). Stages, cheap first:
    syntax/build, then the agents' own published checks (.cb_checks/*, present
    with preserve_invariants), then fail-fast repo tests. Any DEFINITE failure
    -> broken; missing tooling/timeouts read healthy (never repair blind)."""
    if not _tree_health(env):
        return False
    names = env.execute("ls .cb_checks/*.py 2>/dev/null").stdout.split()
    for n in names:
        r = env.execute(f"timeout 60 python3 {n} >/dev/null 2>&1; echo rc=$?")
        if r.stdout.strip().endswith("rc=1"):
            return False
    t = env.execute("python3 -m pytest -q -x --co -q >/dev/null 2>&1 && python3 -m pytest -q -x 2>&1 | tail -1", timeout=420)
    if "failed" in (t.stdout or "") or "error" in (t.stdout or "").lower():
        return False
    return True


def _gather_merge_evidence(env: Environment) -> str:
    """R2: collect concrete merge-damage evidence for the repair brief."""
    parts: list[str] = []
    rej = env.execute(
        "for f in $(find . -path ./.git -prune -o -name '*.rej' -print | head -6); do "
        "echo \"=== $f\"; head -40 $f; done"
    ).stdout.strip()
    if rej:
        parts.append("REJECTED HUNKS (apply these changes manually where they belong):\n" + rej[:3000])
    marks = env.execute("grep -rn '<<<<<<<' --include='*.py' --include='*.go' . 2>/dev/null | head -10").stdout.strip()
    if marks:
        parts.append("CONFLICT MARKERS at:\n" + marks[:1000])
    bad = env.execute(
        "python3 - <<'CB_EV_EOF'\n"
        "import ast, pathlib\n"
        "for p in pathlib.Path('.').rglob('*.py'):\n"
        "    s = str(p)\n"
        "    if '.git/' in s or s.startswith('.cb_'):\n"
        "        continue\n"
        "    try:\n"
        "        ast.parse(p.read_bytes(), filename=s)\n"
        "    except SyntaxError as e:\n"
        "        print(f'{s}:{e.lineno}: {e.msg}')\n"
        "    except Exception:\n"
        "        pass\n"
        "CB_EV_EOF"
    ).stdout.strip()
    if bad:
        parts.append("SYNTAX ERRORS:\n" + bad[:800])
    checks = env.execute(
        "for f in .cb_checks/*.py; do [ -f \"$f\" ] || continue; "
        "out=$(timeout 60 python3 $f 2>&1); rc=$?; "
        "if [ $rc -ne 0 ]; then echo \"=== $f FAILED (rc=$rc)\"; echo \"$out\" | tail -8; fi; done"
    ).stdout.strip()
    if checks:
        parts.append("FAILING ACCEPTANCE CHECKS:\n" + checks[:2000])
    return "\n\n".join(parts)[:7000]


_SCRATCHPAD = "/workspace/shared"


def _team_lead_block(members: list[Assignment]) -> str:
    """team_roles: lead-role prompt block (CooperBench team-mode analogue).

    The lead plans, assigns board tasks, implements its own feature, then
    MERGES member patches from the shared scratchpad — the lead's tree is
    what the team submits."""
    names = ", ".join(f"{m.agent_id} (feature {m.feature_id})" if m.feature_id is not None else m.agent_id for m in members)
    patch_list = ", ".join(f"{_SCRATCHPAD}/{m.agent_id}.patch" for m in members)
    return (
        f"\n\nROLE — TEAM LEAD. Teammates working RIGHT NOW in parallel copies of this repo: {names}. "
        f"A shared scratchpad directory {_SCRATCHPAD}/ is mounted in EVERY container; files there are "
        "not graded. The team is graded on YOUR container's final tree — it must contain EVERY feature.\n"
        f"1. FIRST: write a short plan to {_SCRATCHPAD}/PLAN.md dividing files/regions between the "
        "features, and post each teammate's task on the board (task_create). Then implement your own "
        "feature.\n"
        f"2. Each teammate exports finished work to its patch file ({patch_list}) and marks its board "
        "task done.\n"
        "3. Before you finish — MANDATORY integration: check `ls " + _SCRATCHPAD + "/*.patch`; apply each "
        "teammate patch with `git apply <patch>` (on failure try `git apply --3way <patch>`, then fix "
        "conflicts by hand until the code is consistent). If a patch has not appeared yet and you still "
        "have steps, keep checking between your own steps (`sleep 30` then `ls` is acceptable). "
        "Confirm with `git diff` that ALL features are present before finishing. Submitting only your "
        "own feature fails the whole team."
    )


def _team_member_block(a: Assignment, lead: Assignment) -> str:
    """team_roles: member-role prompt block (CooperBench team-mode analogue)."""
    lead_desc = f"{lead.agent_id} (feature {lead.feature_id})" if lead.feature_id is not None else lead.agent_id
    return (
        f"\n\nROLE — TEAM MEMBER. You are {a.agent_id}. The team lead {lead_desc} "
        "works in a parallel copy of this repo and merges the team result; "
        f"the team is graded on the LEAD's merged tree. A shared scratchpad directory {_SCRATCHPAD}/ is "
        "mounted in EVERY container; files there are not graded.\n"
        f"1. FIRST: read {_SCRATCHPAD}/PLAN.md if it exists and check the board (task_list) — they say "
        "which files/regions your feature owns. Claim your task (task_claim) and stay inside your "
        "regions.\n"
        "2. Implement YOUR feature.\n"
        f"3. When done — MANDATORY export: `git add -A && git diff --cached > {_SCRATCHPAD}/{a.agent_id}.patch`, "
        "then mark your board task done (task_update). Without this file your work cannot be merged and "
        "the team fails. Export a few steps BEFORE your step budget runs out."
    )


def _merge_repair_task(assignments: list[Assignment]) -> str:
    """Q5 integrator brief: the mechanical merge of parallel branches broke the
    tree; repair conflicts WITHOUT discarding either feature."""
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(f"### Feature {i + 1}\n{s}" for i, s in enumerate(specs))
    return (
        "You are the merge integrator. Teammates implemented the features below in PARALLEL "
        "copies of this repo and their diffs were just merged mechanically into THIS tree — "
        "the merge left it BROKEN (conflict markers like <<<<<<<, partially applied hunks, "
        "or syntax errors). Your job:\n"
        "1. Find the damage: search for conflict markers (`grep -rn '<<<<<<<' --include='*.py' .`), "
        "run a syntax check, look at `git diff` for incoherent hunks.\n"
        "2. Repair it so BOTH features work together — keep both implementations, reconciling "
        "names/regions where they collided. Do not delete a feature to make the tree build.\n"
        "3. Verify: syntax-check the files you touched and run any quick relevant tests.\n\n"
        "The features:\n\n" + bundle
    )


def _completeness_task(assignments: list[Assignment]) -> str:
    """T3 reviewer brief — verify EACH feature is fully implemented; fill gaps."""
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(f"### Feature {i + 1}\n{s}" for i, s in enumerate(specs))
    return (
        "You are a completeness reviewer. The team has implemented the features below in THIS repo "
        "(see `git diff`). Teams frequently implement one feature fully but OMIT or half-finish "
        "another. Go feature by feature:\n"
        "1. For EACH feature, confirm its required public API/behavior actually exists in the code "
        "(grep for the names/symbols the feature requires).\n"
        "2. If a feature is missing, partial, or only stubbed, IMPLEMENT it fully now.\n"
        "3. Ensure the project still builds. Do NOT create or edit test files.\n"
        "Submit only when every feature below is genuinely present and the code builds.\n\n"
        f"{bundle}"
    )


def _repair_task(assignments: list[Assignment]) -> str:
    """Integration/repair brief for the S5 verify-and-fix pass."""
    # Distinct feature specs (skip duplicates from shared-objective mode).
    specs: list[str] = []
    for a in assignments:
        if a.task not in specs:
            specs.append(a.task)
    bundle = "\n\n---\n\n".join(specs)
    return (
        "Your teammates have implemented the features below in THIS repository "
        "(see `git diff`). Your job is integration + repair, not new features:\n"
        "1. Make sure EVERY feature below is fully and correctly implemented.\n"
        "2. Make the project BUILD/COMPILE and its existing test suite pass — fix "
        "any compile errors, broken imports, or half-finished work you find.\n"
        "3. Resolve any inconsistencies between the features (naming, signatures).\n"
        "Do NOT create or edit test files. When it builds and is complete, submit.\n\n"
        f"## Features that must all work\n\n{bundle}"
    )


class UnifiedHarness:
    """Runs one team on one task, growing it on demand."""

    def __init__(
        self,
        *,
        bus: TeamBus | None = None,
        step_limit: int = 40,
        cost_limit: float = 5.0,
        command_timeout: int = 60,
        quiet: bool = True,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.bus = bus
        self.step_limit = step_limit
        self.cost_limit = cost_limit
        self.command_timeout = command_timeout
        self.quiet = quiet
        self._on_event = on_event

    def _emit(self, msg: str) -> None:
        if self._on_event is not None:
            self._on_event(msg)

    def _build_assignments(self, spec: TeamSpec) -> list[Assignment]:
        """Resolve a spec into concrete seed assignments.

        ``assignments`` given → used verbatim.  Otherwise a single
        ``objective`` is fanned out to ``team_size`` agents (lead first).
        """
        if spec.assignments:
            return spec.assignments
        if spec.objective is None:
            raise ValueError("TeamSpec needs either assignments or an objective")
        seeds = []
        for i in range(max(1, spec.team_size)):
            seeds.append(
                Assignment(
                    agent_id=f"agent{i + 1}",
                    task=spec.objective,
                    role="lead" if i == 0 else "member",
                    feature_id=spec.features[i] if i < len(spec.features) else None,
                )
            )
        return seeds

    def _run_isolated(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
    ) -> RunResult:
        """Coordinated team where **every agent runs in its OWN container**
        (hard constraint — no shared live container).

        Agents run sequentially; each agent's fresh container is seeded with the
        cumulative diff of the teammates before it (applied via ``git apply``),
        so it builds on their committed work without sharing a live workspace.
        The last agent's cumulative diff is the integrated submission; an
        optional verify-fix integrator also runs in its own container.
        """

        def pick_llm(agent_id: str, role: str) -> LLMClient:
            if llm_factory is not None:
                return llm_factory(agent_id, role)
            if llm is None:
                raise ValueError("either llm or llm_factory must be provided")
            return llm

        def run_on_shared(
            env: Environment,
            agent_id: str,
            role: str,
            task: str,
            feature_id: int | None,
            step_limit: int | None = None,
            poller=None,
            time_limit_s: int | None = None,
            monitor=None,
        ) -> AgentResult:
            """Run one agent (mini-swe or builtin worker) on the shared tree."""
            if spec.spec_fidelity:  # S8: team injects spec-fidelity policy into the agent prompt
                task = _SPEC_FIDELITY + task
            if spec.tdd_preamble:  # T2: in-loop self-verification workflow
                task = _TDD_PREAMBLE + task
            if spec.mine_conventions:  # T4: in-loop convention-mining workflow
                task = _MINE_CONVENTIONS + task
            if spec.worker == "mini_swe":
                from cooperagents.workers.mini_swe_worker import BusComm, TaskBoard, run_mini_swe_agent

                return run_mini_swe_agent(
                    env,
                    task=task,
                    agent_id=agent_id,
                    role=role,
                    model_name=spec.model,
                    step_limit=step_limit or self.step_limit,
                    cost_limit=self.cost_limit,
                    feature_id=feature_id,
                    command_timeout=self.command_timeout,
                    guard_git=spec.guard_git,
                    temperature=spec.temperature,
                    comm=BusComm(bus, agent_id) if spec.coop_tools else None,
                    poller=poller,
                    tool_protocol=spec.tool_protocol,
                    task_board=TaskBoard(bus, agent_id) if spec.task_board else None,
                    wait_protocol=spec.wait_protocol,
                    spawn_handler=make_spawn_handler(agent_id) if spec.allow_spawn_tool else None,
                    time_limit_s=time_limit_s,
                    monitor=monitor,
                    git_share=spec.git_share,
                    completion_gate=spec.completion_gate,
                )
            agent = Agent(
                agent_id=agent_id,
                role=role,
                task=task,
                env=env,
                llm=pick_llm(agent_id, role),
                bus=bus,
                feature_id=feature_id,
                allow_spawn=False,
                step_limit=self.step_limit,
                cost_limit=self.cost_limit,
                command_timeout=self.command_timeout,
            )
            return agent.run()

        def seed_prior(env: Environment, patch: str) -> None:
            """Seed a fresh container with teammates' cumulative work via ``git apply``,
            then COMMIT it so the agent starts from a clean, coherent base (a dirty
            seeded tree confuses the agent and pollutes its own diff)."""
            if not patch.strip():
                return
            env.write_file(".cb_prior.patch", patch)
            env.execute(
                "git apply --whitespace=nowarn .cb_prior.patch 2>/dev/null "
                "|| git apply --3way .cb_prior.patch 2>/dev/null "
                "|| git apply --reject .cb_prior.patch 2>/dev/null || true"
            )
            env.execute(
                "rm -f .cb_prior.patch && git add -A && "
                "git -c user.email=team@cooperagents.local -c user.name=cooperagents commit -q -m 'teammate work' || true"
            )

        seeds: dict[str, AgentResult] = {}
        gate_discards: list[str] = []  # agents whose delta the do_no_harm gate rejected
        coordinator = None  # set in the coop branch when spec.coordinator
        start = time.time()
        integrated_patch = ""  # cumulative diff across the team so far (seed mode)
        member_patches: list[str] = []  # each agent's own diff (no-seed mode)
        team_lead_patch: str | None = None  # team_roles: lead's merged tree is the submission
        prior: list[str] = []
        prior_fids: list[int] = []  # feature ids done so far (preserve_invariants)
        # HARD CONSTRAINT: every agent runs in its OWN container. With seed mode
        # (default) each fresh container is seeded by teammates' cumulative diff;
        # with no-seed each agent works independently and an integrator merges.
        assignments_all = list(assignments)
        if spec.coop_tools:
            # Q4 (qwen program): CooperBench-team-harness shape inside the unified
            # harness — agents run CONCURRENTLY from base (own containers), and
            # coordinate via the bus (`send_message` tool + inbox drained into
            # observations each step). Requires no-seed; the standard no-seed
            # integration tail below merges the member patches.
            from concurrent.futures import ThreadPoolExecutor

            roster = {a.agent_id: a for a in assignments}
            if spec.claim_mode:
                # TK6: seed one unclaimed board task per feature; agents divide
                # the work themselves via task_claim.
                for a in assignments:
                    if a.feature_id is not None:
                        bus.create_task(
                            title=f"Feature {a.feature_id} — see objective section 'Feature {a.feature_id}'",
                            created_by="harness",
                            owner="",
                        )
            spawn_lock = threading.Lock()
            spawn_count = [0]
            helper_patches: list[str] = []

            def make_spawn_handler(parent_id: str):
                def spawn(action: dict) -> dict:
                    cap = (spec.max_agents or len(assignments)) - len(assignments)
                    with spawn_lock:
                        if spawn_count[0] >= max(0, cap):
                            return {"output": f"spawn denied: helper cap ({cap}) reached", "returncode": 1, "exception_info": ""}
                        spawn_count[0] += 1
                        hid = f"helper{spawn_count[0]}"
                    brief = str(action.get("task", ""))[:4000]

                    def run_helper():
                        henv = env_factory(hid)
                        try:
                            hr = run_on_shared(henv, hid, "helper", brief, None)
                            seeds[hid] = hr
                            helper_patches.append(strip_test_sections(henv.git_diff()))
                        except Exception:  # noqa: BLE001 - a failed helper must not kill the parent
                            pass
                        finally:
                            henv.cleanup()

                    th = threading.Thread(target=run_helper, daemon=True)
                    th.start()
                    spawn_threads.append(th)
                    return {"output": f"{hid} spawned by {parent_id} on: {brief[:120]}", "returncode": 0, "exception_info": ""}

                return spawn

            spawn_threads: list[threading.Thread] = []
            # TK1/Q6: shared-interface contract, one planner call, pushed into
            # every brief. Empty (feature off) when offline or on any error.
            contract = _build_contract(assignments, spec.model) if spec.contract_first else ""
            # Envs are created up front (not per-thread) so the TK2 poller can
            # observe teammates' trees across containers.
            coop_envs: dict[str, Environment] = {a.agent_id: env_factory(a.agent_id) for a in assignments}
            gitsync = None
            if spec.git_share:
                first = True
                for _aid, _e in coop_envs.items():
                    if first:
                        _e.execute(f"git init -q --bare {_GITSHARE} 2>/dev/null || true")
                        first = False
                    _e.execute(f"git remote add shared {_GITSHARE} 2>/dev/null || true")
                    _e.execute(f"git push -q shared HEAD:refs/heads/{_aid} 2>/dev/null || true")
                gitsync = _GitShareSync(coop_envs)
                threading.Thread(target=gitsync.run, daemon=True).start()
            coordinator = _Coordinator(coop_envs, spec.model) if spec.coordinator else None
            if coordinator is not None:
                threading.Thread(target=coordinator.run, daemon=True).start()

            def run_coop(a: Assignment) -> tuple[str, AgentResult, str]:
                env = coop_envs[a.agent_id]
                if spec.team_roles:
                    # Complete-Team cell: role block replaces the generic
                    # coordination paragraphs (mirrors CooperBench team mode,
                    # where the team block substitutes for the coop block).
                    lead = assignments[0]
                    if a.agent_id == lead.agent_id:
                        others = [x for x in assignments if x.agent_id != a.agent_id]
                        task = a.task + _team_lead_block(others)
                    else:
                        task = a.task + _team_member_block(a, lead)
                    poller = _TeammatePoller(a.agent_id, coop_envs) if spec.task_board else None
                    if poller is not None:
                        poller.watch_board(bus)
                    r = run_on_shared(env, a.agent_id, a.role, task, a.feature_id, poller=poller, time_limit_s=spec.agent_time_limit)
                    return a.agent_id, r, strip_test_sections(env.git_diff())
                if True:
                    mates = ", ".join(
                        f"{x.agent_id} (feature {x.feature_id})" for x in assignments if x.agent_id != a.agent_id
                    )
                    task = a.task + (
                        f"\n\nTEAMMATES: {mates} are implementing their features RIGHT NOW in parallel "
                        "copies of this repo; your diffs will be merged at the end. Coordinate via the "
                        "send_message tool (incoming messages appear as [Message from ...]): agree on "
                        "shared names/signatures early and avoid editing the same regions."
                    )
                    if spec.preserve_invariants and a.feature_id is not None:
                        # Q8: the published check is used ONLY for best-of-N candidate
                        # selection (cross-attempt agreement probes); it is stripped
                        # before grading and never ships.
                        task += (
                            f"\n\nPUBLISH YOUR ACCEPTANCE CHECK — before finishing, create "
                            f"`.cb_checks/f{a.feature_id}.py`: a minimal STANDALONE python script (no "
                            "pytest) that exercises YOUR feature's public behavior exactly as the spec "
                            "describes and exits non-zero if it is broken or absent. Run it to confirm "
                            "it passes. It must terminate in seconds — no loops that can hang."
                        )
                    if spec.claim_mode:
                        task += (
                            "\n\nWORK ALLOCATION — the objective above contains MULTIPLE features and the "
                            "board has one UNCLAIMED task per feature. FIRST action: task_list, then "
                            "task_claim one task. Implement ONLY features you claimed. When done, mark it "
                            "done and claim more unclaimed work if any remains. If a claim fails, someone "
                            "else owns it — pick another."
                        )
                    if spec.task_board:
                        task += (
                            "\n\nTASK BOARD PROTOCOL — before coding, post 2-4 short tasks describing "
                            "your plan (task_create), mark each 'doing' when you start and 'done' when "
                            "finished (task_update). Check the whole board (task_list) before editing "
                            "files a teammate's tasks mention. Keep titles short; spend steps on code."
                        )
                    if spec.wait_protocol:
                        task += (
                            "\n\nIf you need an agreed public name/signature from a teammate BEFORE you "
                            "can proceed, use send_message with wait:true — the reply comes back in the "
                            "same tool output."
                        )
                    if spec.tool_protocol:
                        first_mate = next((x.agent_id for x in assignments if x.agent_id != a.agent_id), "your teammate")
                        task += (
                            f"\n\nCOORDINATION PROTOCOL — your FIRST action must be a send_message to "
                            f"{first_mate} stating the public names, signatures, and files you plan to "
                            "create for your feature. Before editing any file you suspect your teammate "
                            "also touches, check your observations for [Message from ...] notes and "
                            "reconcile names with what they declared. Keep messages short; spend your "
                            "steps on code."
                        )
                    if contract:
                        task += (
                            "\n\nSHARED INTERFACE CONTRACT — the team agreed on this up front; "
                            "follow it EXACTLY (names, signatures, file locations). Deviating breaks "
                            "the merge with your teammates:\n" + contract
                        )
                    poller = (
                        _TeammatePoller(a.agent_id, coop_envs)
                        if (spec.live_awareness or spec.task_board or spec.coordinator or spec.git_share)
                        else None
                    )
                    if poller is not None and spec.git_share:
                        poller.watch_gitshare()
                    if poller is not None and spec.task_board:
                        poller.watch_board(bus)
                    if poller is not None and coordinator is not None:
                        poller.watch_coordinator(coordinator)
                    r = run_on_shared(env, a.agent_id, a.role, task, a.feature_id,
                                      poller=poller, monitor=coordinator, time_limit_s=spec.agent_time_limit)

                    return a.agent_id, r, strip_test_sections(env.git_diff())
                return None  # unreachable

            try:
                with ThreadPoolExecutor(max_workers=len(roster)) as ex:
                    for aid, r, diff in ex.map(run_coop, assignments):
                        seeds[aid] = r
                        member_patches.append(diff)
                        if spec.team_roles and aid == assignments[0].agent_id:
                            team_lead_patch = diff
                        prior.append(f"feature {roster[aid].feature_id}" if roster[aid].feature_id is not None else roster[aid].role)
            finally:
                for _e in coop_envs.values():
                    _e.cleanup()
            if gitsync is not None:
                gitsync.stop()
            if coordinator is not None:
                coordinator.stop()
            for th in spawn_threads:
                th.join(timeout=1200)
            member_patches.extend(p for p in helper_patches if p.strip())
            assignments = []  # sequential loop below is skipped
        for a in assignments:
            env = env_factory(a.agent_id)
            try:
                if spec.seed_prior:
                    seed_prior(env, integrated_patch)
                task = a.task
                if prior and spec.teammate_context:
                    if spec.seed_prior:
                        task += (
                            f"\n\nTeammates before you already implemented {', '.join(prior)} — their code is "
                            "ALREADY in this repository (run `git diff` to see it). Build on it, reuse their "
                            "public names/signatures, and do not duplicate or revert it."
                        )
                    elif integrated_patch.strip():
                        drafts = integrated_patch[:6000]
                        task += (
                            f"\n\nTeammates are implementing {', '.join(prior)} in parallel; their drafts:\n"
                            f"```diff\n{drafts}\n```\nReuse their public names/signatures."
                        )
                if spec.preserve_invariants:
                    # Coordination-under-interdependence: each agent publishes a runnable
                    # regression check for its OWN feature into the shared tree (.cb_checks/,
                    # stripped before grading); later agents MUST keep all prior checks green —
                    # directly targeting the dominant coupled failure (a later agent silently
                    # breaking an earlier teammate's feature while editing shared code).
                    if prior_fids:
                        checks = " ".join(f".cb_checks/f{f}.py" for f in prior_fids)
                        task += (
                            f"\n\nTEAMMATE INVARIANTS — features {', '.join(str(f) for f in prior_fids)} are already "
                            f"implemented and WORKING, each verified by a check script: {checks}. FIRST run "
                            f"`python {checks}` to see them pass. As you add your feature you MUST keep ALL of them "
                            "passing — do not change or break a teammate's behavior. Re-run them before finishing; "
                            "if you broke one, fix your code (not the check) until it passes again."
                        )
                    if a.feature_id is not None:
                        task += (
                            f"\n\nPUBLISH YOUR INVARIANT — before finishing, create `.cb_checks/f{a.feature_id}.py`: a "
                            "minimal STANDALONE python script (no pytest) that exercises YOUR feature's public "
                            "behavior and raises/exits non-zero if it regresses. Run it to confirm it passes. This is "
                            "your contract to teammates who build on your work; keep it small and fast."
                        )
                pre_healthy = _tree_health(env) if spec.do_no_harm else True
                seeds[a.agent_id] = run_on_shared(env, a.agent_id, a.role, task, a.feature_id)
                diff = strip_test_sections(env.git_diff())
                if spec.do_no_harm and pre_healthy and not _tree_health(env):
                    # Q1 do-no-harm gate: the agent broke a previously-healthy
                    # tree — discard its delta so the team keeps the last
                    # healthy state instead of shipping corrupted code.
                    gate_discards.append(a.agent_id)
                    diff = integrated_patch if spec.seed_prior else ""
            finally:
                env.cleanup()
            member_patches.append(diff)
            if spec.seed_prior:
                integrated_patch = diff  # cumulative
            else:
                integrated_patch = "\n".join(member_patches)  # text-only context for next agent
            prior.append(f"feature {a.feature_id}" if a.feature_id is not None else a.role)
            if a.feature_id is not None:
                prior_fids.append(a.feature_id)

        # Integration. Seed mode already has the cumulative diff; no-seed must merge
        # the independent member patches in a fresh integrator container.
        if not spec.seed_prior:
            integrated_patch = ""  # rebuild from member patches below
        if spec.team_roles and team_lead_patch is not None:
            # Complete-Team cell: the lead already integrated the member's
            # scratchpad patch in its own container; the lead's tree is the
            # team submission (matches CooperBench team-mode scoring).
            integrated_patch = team_lead_patch
        elif spec.verify_fix and len(assignments) > 1:
            env = env_factory("integrator")
            try:
                if spec.seed_prior:
                    seed_prior(env, integrated_patch)
                else:
                    for p in member_patches:
                        seed_prior(env, p)
                seeds["integrator"] = run_on_shared(env, "integrator", "integrator", _repair_task(assignments), None)
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
        elif not spec.seed_prior and len(member_patches) > 1 and spec.select_integration is not None:
            # Iteration 7 completion (pre-submission merge arms): each agent's
            # final tree ALREADY contains the merged team work and passed the
            # completion gate; 3-way merging two both-merged trees re-creates
            # the damage the gate just prevented (observed: tuijournal/fx i7
            # regressions to 0 with zero gate rejections). Select the best
            # tree mechanically instead of re-merging.
            idx = spec.select_integration(member_patches)
            integrated_patch = member_patches[idx]
            print(f"[harness] integration=selected chosen={idx} "
                  f"sizes={[len(p) for p in member_patches]}")
        elif not spec.seed_prior and len(member_patches) > 1:
            # No-seed without an LLM integrator: mechanically merge the independent
            # member patches in a fresh container. A real 3-way merge goes first —
            # it auto-resolves non-overlapping edits to the same file, so clean
            # merges skip the apply-chain's .rej fallout (and usually the repair
            # pass). Only on genuine region conflicts fall back to the apply chain
            # (partial applies + .rej) for the repair agent to reconcile.
            env = env_factory("merge")
            try:
                merge_base = (env.execute("git rev-parse HEAD").stdout or "").strip()
                if spec.apply_chain_merge:
                    conflict = True  # TK8: force the apply-chain path (repairable damage)
                else:
                    conflict, _merged = _threeway_merge(env, member_patches)
                if conflict:
                    env.execute(f"git checkout -q -B _fb {merge_base}" if merge_base else "true")
                    for p in member_patches:
                        seed_prior(env, p)
                # Merge hygiene: apply-fallback artifacts (.rej/.orig) are not part
                # of any feature — without this they ship inside the integrated
                # diff (observed on qwen14-q4: 7/14 pairs polluted).
                env.execute("find . -path ./.git -prune -o \\( -name '*.rej' -o -name '*.orig' \\) -print0 2>/dev/null | xargs -0 -r rm -f")
                _gate = _tree_health_behavioral if spec.behavioral_gate else _tree_health
                if spec.repair_integrator and not _gate(env):
                    repair_brief = _merge_repair_task(assignments_all)
                    if spec.focused_repair:
                        ev = _gather_merge_evidence(env)
                        if ev:
                            repair_brief += (
                                "\n\nEVIDENCE — the harness already located the damage; fix THESE "
                                "directly instead of searching:\n\n" + ev
                            )
                    # Q5: repair ON DEMAND — only when the merge demonstrably broke
                    # the tree (conflict markers / partial applies → syntax errors).
                    # One agent, in the merged container, with a focused brief.
                    seeds["integrator"] = run_on_shared(
                        env,
                        "integrator",
                        "integrator",
                        repair_brief,
                        None,
                        step_limit=spec.repair_step_limit,
                        time_limit_s=spec.repair_time_limit,
                    )
                    env.execute(
                        "find . -path ./.git -prune -o \\( -name '*.rej' -o -name '*.orig' \\) -print0 2>/dev/null | xargs -0 -r rm -f"
                    )
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
        elif not spec.seed_prior:
            integrated_patch = member_patches[0] if member_patches else ""

        # T3: completeness review — own container, seeded with the full diff,
        # enumerates each feature and fills gaps (the dominant failure mode).
        if spec.completeness_review and len(assignments) > 1:
            env = env_factory("reviewer")
            try:
                seed_prior(env, integrated_patch)
                seeds["reviewer"] = run_on_shared(env, "reviewer", "reviewer", _completeness_task(assignments), None)
                integrated_patch = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()

        integrated = AgentResult(
            agent_id="team",
            role="integrated",
            status="submitted" if integrated_patch.strip() else "error",
            patch=integrated_patch,
            cost=sum(r.cost for r in seeds.values()),
            steps=sum(r.steps for r in seeds.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=seeds,
            integrated=integrated,
            duration_seconds=time.time() - start,
            metrics={
                **coordination_metrics(bus.task_events(), final_tasks=bus.list_tasks()),
                **({"do_no_harm_discards": gate_discards} if spec.do_no_harm else {}),
                **({"coordinator_events": coordinator.events()} if spec.coordinator and coordinator is not None else {}),
            },
        )

    def _run_worker(
        self,
        spec: TeamSpec,
        env: Environment,
        *,
        agent_id: str,
        role: str,
        task: str,
        feature_id: int | None,
        bus: TeamBus,
        llm: LLMClient | None,
    ) -> AgentResult:
        """Run one agent (mini-swe or builtin) in its own container, applying the
        team-level prompt seams (spec-fidelity / TDD / convention-mining)."""
        if spec.spec_fidelity:
            task = _SPEC_FIDELITY + task
        if spec.tdd_preamble:
            task = _TDD_PREAMBLE + task
        if spec.mine_conventions:
            task = _MINE_CONVENTIONS + task
        if spec.worker == "mini_swe":
            from cooperagents.workers.mini_swe_worker import run_mini_swe_agent

            return run_mini_swe_agent(
                env,
                task=task,
                agent_id=agent_id,
                role=role,
                model_name=spec.model,
                step_limit=self.step_limit,
                cost_limit=self.cost_limit,
                feature_id=feature_id,
                command_timeout=self.command_timeout,
                guard_git=spec.guard_git,
                temperature=spec.temperature,
            )
        if llm is None:
            raise ValueError("builtin worker requires an LLM client")
        agent = Agent(
            agent_id=agent_id,
            role=role,
            task=task,
            env=env,
            llm=llm,
            bus=bus,
            feature_id=feature_id,
            allow_spawn=False,
            step_limit=self.step_limit,
            cost_limit=self.cost_limit,
            command_timeout=self.command_timeout,
        )
        return agent.run()

    def _run_decomposed(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
        planner: Planner | None,
    ) -> RunResult:
        """G1+G2+G3: plan an independence-maximizing subtask DAG, run it with
        independent subtasks in PARALLEL (own containers, seeded only along DAG
        edges), then merge the branch deltas.

        Each subtask is one agent in its own container (hard constraint). A
        subtask is seeded with the deltas of its transitive ancestors only — not
        the whole shared state — so two independent branches never see each
        other's edits, eliminating the interference that coupled sequential
        seeding causes (Round 6). Whether the merge is clean is exactly the test
        of decomposition quality.
        """
        start = time.time()
        specs: list[tuple[int, str]] = [
            (a.feature_id if a.feature_id is not None else i + 1, a.task) for i, a in enumerate(assignments)
        ]
        cap = spec.max_agents if spec.max_agents is not None else len(assignments)
        max_sub = max(1, min(cap, len(assignments) + 1))
        if planner is not None:
            subs, rationale = planner(specs, spec.objective)
        else:
            subs, rationale = plan_decomposition(specs, objective=spec.objective, max_subtasks=max_sub, model=spec.model)
        by_id = {s.id: s for s in subs}
        levels = topo_levels(subs)
        topo_order = [s.id for level in levels for s in level]

        def pick_llm(agent_id: str, role: str) -> LLMClient | None:
            return llm_factory(agent_id, role) if llm_factory is not None else llm

        deltas: dict[str, str] = {}
        results: dict[str, AgentResult] = {}
        lock = threading.Lock()

        def ownership_preamble(s: SubTask) -> str:
            """Hard write-set boundary: the key to conflict-free re-division —
            this agent edits ONLY its regions; teammates' regions are off-limits
            so two subtasks on the same file (disjoint regions) merge cleanly."""
            if not s.owns:
                return ""
            others = sorted({o for t in subs if t.id != s.id for o in t.owns})
            msg = (
                "OWNERSHIP — you may edit ONLY these regions (your write-set):\n  - "
                + "\n  - ".join(s.owns)
                + "\nDo NOT edit anything outside them."
            )
            if others:
                msg += (
                    " Teammates own these regions in parallel — do NOT touch them; if you need their "
                    "code, assume the public interface described in the spec:\n  - " + "\n  - ".join(others)
                )
            return msg + "\n\n"

        def publish_preamble(s: SubTask) -> str:
            # Guarded-merge (loss-free parallelism): each parallel branch publishes a
            # runnable check for its feature so the integrator can detect & repair any
            # feature the merge breaks (Round 9: parallel split+merge loses 7/28 features).
            if not (spec.preserve_invariants and s.features):
                return ""
            return (
                "PUBLISH YOUR INVARIANT — before finishing, create `.cb_checks/f"
                f"{s.features[0]}.py`: a minimal STANDALONE python script (no pytest) that exercises YOUR "
                "feature's public behavior and exits non-zero if it regresses. Run it to confirm it passes. "
                "The integrator runs it after merging all branches to ensure the merge didn't break you.\n\n"
            )

        def run_sub(s: SubTask) -> None:
            env = env_factory(s.id)
            try:
                anc = ancestors(s, by_id)
                for aid in [x for x in topo_order if x in anc]:
                    with lock:
                        seed = deltas.get(aid, "")
                    _seed_patch(env, seed)
                res = self._run_worker(
                    spec,
                    env,
                    agent_id=s.id,
                    role="member",
                    task=publish_preamble(s) + ownership_preamble(s) + s.task,
                    feature_id=s.features[0] if s.features else None,
                    bus=bus,
                    llm=pick_llm(s.id, "member"),
                )
                delta = strip_test_sections(env.git_diff())
            except Exception as e:  # noqa: BLE001 - one subtask must not kill the run
                res = AgentResult(agent_id=s.id, role="member", status="error", error=str(e))
                delta = ""
            finally:
                env.cleanup()
            with lock:
                deltas[s.id] = delta
                results[s.id] = res

        # Run level by level; subtasks within a level are independent → parallel.
        for level in levels:
            threads = [threading.Thread(target=run_sub, args=(s,)) for s in level]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Integrate: a fresh container, apply every delta in topo order. Clean if
        # the decomposition was well-separated; conflicts (.rej) if it wasn't.
        env = env_factory("integrator")
        guarded = spec.preserve_invariants and len(subs) > 1
        try:
            for sid in topo_order:
                _seed_patch(env, deltas.get(sid, ""))
            if guarded:
                # Loss-free parallelism: the merge may have broken a feature that worked
                # in its own branch. The integrator runs every published check and repairs
                # the merge until all pass — turning a lossy split+merge into a guarded one.
                feats = sorted({f for s in subs for f in s.features})
                checks = " ".join(f".cb_checks/f{f}.py" for f in feats)
                repair = (
                    "You are the integrator. Branches were developed in parallel and merged into this repo "
                    f"(some merges may have left conflicts or broken a feature). Run `python {checks}` — these "
                    "are per-feature checks each branch published. Any that FAIL means the merge broke that "
                    "feature: fix the integration (resolve conflict markers / .rej, reconcile shared code) until "
                    "EVERY check passes. Do NOT edit the check files or delete features. Submit when all pass."
                )
                results["integrator"] = self._run_worker(
                    spec,
                    env,
                    agent_id="integrator",
                    role="integrator",
                    task=repair,
                    feature_id=None,
                    bus=bus,
                    llm=pick_llm("integrator", "integrator"),
                )
            integrated_patch = strip_test_sections(env.git_diff())
        finally:
            env.cleanup()

        integrated = AgentResult(
            agent_id="team",
            role="integrated",
            status="submitted" if integrated_patch.strip() else "error",
            patch=integrated_patch,
            cost=sum(r.cost for r in results.values()),
            steps=sum(r.steps for r in results.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=results,
            integrated=integrated,
            duration_seconds=time.time() - start,
            metrics={
                "decompose": True,
                "n_subtasks": len(subs),
                "n_edges": sum(len(s.depends_on) for s in subs),
                "levels": [[s.id for s in level] for level in levels],
                "max_parallel": max((len(level) for level in levels), default=0),
                "guarded_merge": guarded,
                "rationale": rationale,
            },
        )

    def _run_adaptive(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        bus: TeamBus,
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
    ) -> RunResult:
        """Let the work decide sequential vs parallel (runtime topology selection).

        Phase 1: run every feature in PARALLEL from base (own containers), each
        publishing a runnable invariant check. Phase 2: probe the merge — if the
        branches apply cleanly onto each other and the checks stay green, KEEP the
        parallel result. Phase 3: on a conflict (the work was coupled), FALL BACK
        to the sequential build-on-prior handoff for the remaining features,
        reusing the first branch. The conflict is the decision — no ex-ante guess.
        """
        start = time.time()

        def pick_llm(agent_id: str, role: str) -> LLMClient | None:
            return llm_factory(agent_id, role) if llm_factory is not None else llm

        def publish_pre(a: Assignment) -> str:
            if a.feature_id is None:
                return ""
            return (
                f"PUBLISH YOUR INVARIANT — before finishing, create `.cb_checks/f{a.feature_id}.py`: a minimal "
                "STANDALONE python script (no pytest) that exercises YOUR feature's public behavior and exits "
                "non-zero if it regresses. Run it to confirm it passes.\n\n"
            )

        # Phase 1 — parallel from base (own container each), publish checks.
        deltas: dict[str, str] = {}
        par_results: dict[str, AgentResult] = {}
        lock = threading.Lock()

        def run_par(a: Assignment) -> None:
            env = env_factory(a.agent_id)
            try:
                res = self._run_worker(
                    spec, env, agent_id=a.agent_id, role=a.role, task=publish_pre(a) + a.task,
                    feature_id=a.feature_id, bus=bus, llm=pick_llm(a.agent_id, a.role),
                )
                delta = strip_test_sections(env.git_diff())
            except Exception as e:  # noqa: BLE001 - one branch must not kill the run
                res = AgentResult(agent_id=a.agent_id, role=a.role, status="error", feature_id=a.feature_id, error=str(e))
                delta = ""
            finally:
                env.cleanup()
            with lock:
                deltas[a.agent_id] = delta
                par_results[a.agent_id] = res

        threads = [threading.Thread(target=run_par, args=(a,)) for a in assignments]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Phase 2 — merge probe: do the parallel branches compose under a real
        # 3-way merge? (base-aware; only genuine region overlaps conflict.)
        order = [a.agent_id for a in assignments]
        conflict = False
        integrated_patch = ""
        env = env_factory("merge-probe")
        try:
            conflict, integrated_patch = _threeway_merge(env, [deltas.get(aid, "") for aid in order])
            if not conflict and integrated_patch.strip():
                # secondary signal: a feature silently broke even though it merged
                probe = env.execute(
                    'ok=1; for f in .cb_checks/*.py; do [ -e "$f" ] || continue; python "$f" >/dev/null 2>&1 || ok=0; done; [ "$ok" = 1 ]'
                )
                if probe.exit_code != 0:
                    conflict = True
        finally:
            env.cleanup()

        if not conflict and integrated_patch.strip():
            integrated = AgentResult(
                agent_id="team", role="integrated", status="submitted", patch=integrated_patch,
                cost=sum(r.cost for r in par_results.values()), steps=sum(r.steps for r in par_results.values()),
                feature_id=sorted(spec.features)[0] if spec.features else None,
            )
            return RunResult(
                run_id=spec.run_id, repo=spec.repo, task_id=spec.task_id, features=sorted(spec.features),
                seeds=par_results, integrated=integrated, duration_seconds=time.time() - start,
                metrics={"adaptive": True, "topology": "parallel", "conflict": False},
            )

        # Phase 3 — coupled: fall back to sequential build-on-prior, reusing branch 1.
        seq_results: dict[str, AgentResult] = {order[0]: par_results.get(order[0], AgentResult(order[0], assignments[0].role, "error"))}
        cumulative = deltas.get(order[0], "")
        for a in assignments[1:]:
            env = env_factory(f"{a.agent_id}-seq")
            try:
                _seed_patch(env, cumulative)
                prior = [f"feature {p.feature_id}" for p in assignments[: assignments.index(a)] if p.feature_id is not None]
                task = (
                    publish_pre(a) + a.task
                    + f"\n\nTeammates already implemented {', '.join(prior)} — their working code is ALREADY in "
                    "this repo (run `git diff`). Build on it and do NOT break it."
                )
                res = self._run_worker(
                    spec, env, agent_id=a.agent_id, role=a.role, task=task,
                    feature_id=a.feature_id, bus=bus, llm=pick_llm(a.agent_id, a.role),
                )
                cumulative = strip_test_sections(env.git_diff())
            finally:
                env.cleanup()
            seq_results[a.agent_id] = res

        integrated = AgentResult(
            agent_id="team", role="integrated", status="submitted" if cumulative.strip() else "error", patch=cumulative,
            cost=sum(r.cost for r in seq_results.values()), steps=sum(r.steps for r in seq_results.values()),
            feature_id=sorted(spec.features)[0] if spec.features else None,
        )
        return RunResult(
            run_id=spec.run_id, repo=spec.repo, task_id=spec.task_id, features=sorted(spec.features),
            seeds=seq_results, integrated=integrated, duration_seconds=time.time() - start,
            metrics={"adaptive": True, "topology": "sequential-fallback", "conflict": True},
        )

    def _run_best_of_n(
        self,
        spec: TeamSpec,
        assignments: list[Assignment],
        env_factory: EnvFactory,
        llm: LLMClient | None,
        llm_factory: LLMFactory | None,
        selector: Callable[[list[RunResult]], int] | None,
    ) -> RunResult:
        """T6: run the isolated team ``best_of_n`` times (each its own containers),
        then a self-available selector picks the candidate to submit.

        Run-to-run variance is the headroom; the selector must use only
        self-available signal (LLM judge / build probe), never the hidden grader.
        If no selector is given, fall back to the candidate whose integrated diff
        touches the most files (a weak coverage heuristic, offline-safe).
        """
        def run_attempt(i: int) -> RunResult:
            cand_bus = InMemoryBus(f"{spec.run_id}-c{i}")
            self._emit(f"best-of-{spec.best_of_n}: attempt {i + 1}")
            # Q3: attempt 1 stays at the pinned temperature (reproducible floor);
            # later attempts sample hotter so selection has genuinely different
            # candidates (at temp 0 both attempts usually converge — Q2 finding).
            spec_i = spec
            if i > 0 and spec.diversity_temperature is not None:
                from dataclasses import replace

                spec_i = replace(spec, temperature=spec.diversity_temperature)
            return self._run_isolated(spec_i, assignments, cand_bus, env_factory, llm, llm_factory)

        if llm is None and llm_factory is None:
            # Live worker path: attempts are independent whole-team runs — run them
            # CONCURRENTLY so best-of-N costs ~1 attempt of wall-clock, not N.
            # (Scripted/demo policies stay sequential: a shared ScriptedLLM queue
            # is not safe to consume from two attempts at once.)
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=spec.best_of_n) as ex:
                candidates = list(ex.map(run_attempt, range(spec.best_of_n)))
        else:
            candidates = [run_attempt(i) for i in range(spec.best_of_n)]

        def coverage(r: RunResult) -> int:
            patch = r.integrated.patch if r.integrated else ""
            return sum(1 for line in patch.splitlines() if line.startswith("+++ "))

        if selector is not None:
            try:
                chosen = selector(candidates)
            except Exception as e:  # noqa: BLE001 - a flaky selector must not kill the run
                self._emit(f"selector failed ({e}); falling back to coverage heuristic")
                chosen = max(range(len(candidates)), key=lambda j: coverage(candidates[j]))
        else:
            chosen = max(range(len(candidates)), key=lambda j: coverage(candidates[j]))

        result = candidates[chosen]
        result.metrics = {
            **result.metrics,
            "best_of_n": spec.best_of_n,
            "chosen_index": chosen,
            "candidate_coverage": [coverage(c) for c in candidates],
            "candidate_patch_lines": [c.integrated.patch_lines if c.integrated else 0 for c in candidates],
        }
        result.duration_seconds = sum(c.duration_seconds for c in candidates)
        return result

    def run(
        self,
        spec: TeamSpec,
        *,
        env_factory: EnvFactory,
        llm: LLMClient | None = None,
        llm_factory: LLMFactory | None = None,
        selector: Callable[[list[RunResult]], int] | None = None,
        planner: Planner | None = None,
    ) -> RunResult:
        if llm is None and llm_factory is None and spec.worker != "mini_swe":
            raise ValueError("provide either llm or llm_factory")
        bus = self.bus or InMemoryBus(spec.run_id)
        assignments = self._build_assignments(spec)
        if spec.adaptive:
            return self._run_adaptive(spec, assignments, bus, env_factory, llm, llm_factory)
        if spec.decompose:
            return self._run_decomposed(spec, assignments, bus, env_factory, llm, llm_factory, planner)
        if spec.shared_workspace and spec.best_of_n > 1:
            return self._run_best_of_n(spec, assignments, env_factory, llm, llm_factory, selector)
        if spec.shared_workspace:
            return self._run_isolated(spec, assignments, bus, env_factory, llm, llm_factory)
        n_seed = len(assignments)
        cap = spec.max_agents if spec.max_agents is not None else n_seed
        spawning = spec.allow_spawn and cap > n_seed

        def pick_llm(agent_id: str, role: str) -> LLMClient:
            if llm_factory is not None:
                return llm_factory(agent_id, role)
            if llm is None:
                raise ValueError("either llm or llm_factory must be provided")
            return llm

        seeds: dict[str, AgentResult] = {}
        helpers: dict[str, AgentResult] = {}
        envs: list[Environment] = []
        threads: list[threading.Thread] = []
        lock = threading.Lock()
        live = 0
        total_agents = n_seed

        def worker(agent_id: str, role: str, task: str, feature_id: int | None, *, is_helper: bool) -> None:
            nonlocal live
            try:
                env = env_factory(agent_id)
                with lock:
                    envs.append(env)
                agent = Agent(
                    agent_id=agent_id,
                    role=role,
                    task=task,
                    env=env,
                    llm=pick_llm(agent_id, role),
                    bus=bus,
                    feature_id=feature_id,
                    allow_spawn=spawning,
                    step_limit=self.step_limit,
                    cost_limit=self.cost_limit,
                    command_timeout=self.command_timeout,
                )
                result = agent.run()
            except Exception as e:  # noqa: BLE001 - never let a worker kill the run
                result = AgentResult(agent_id=agent_id, role=role, status="error", feature_id=feature_id, error=str(e))
            with lock:
                (helpers if is_helper else seeds)[agent_id] = result
                live -= 1
            self._emit(f"{agent_id} done: {result.status}")

        def start(agent_id: str, role: str, task: str, feature_id: int | None, *, is_helper: bool) -> None:
            nonlocal live
            with lock:
                live += 1
            t = threading.Thread(target=worker, args=(agent_id, role, task, feature_id), kwargs={"is_helper": is_helper})
            threads.append(t)
            t.start()

        def supervise() -> None:
            nonlocal total_agents
            while True:
                req = bus.spawn_pop(timeout=0.5)
                if req is None:
                    with lock:
                        if live == 0:
                            break
                    continue
                with lock:
                    granted = total_agents < cap
                    if granted:
                        total_agents += 1
                if not granted:
                    bus.spawn_mark(req.id, outcome="capped")
                    self._emit(f"spawn capped (cap={cap}) from {req.requested_by}")
                    continue
                idx = bus.spawn_next_index()
                helper_id = f"helper{idx}"
                bus.spawn_mark(req.id, outcome="granted", agent_id=helper_id)
                self._emit(f"spawned {helper_id} for {req.requested_by}")
                start(helper_id, req.role or "helper", req.task, None, is_helper=True)

        start_time = time.time()
        supervisor: threading.Thread | None = None
        try:
            for a in assignments:
                start(a.agent_id, a.role, a.task, a.feature_id, is_helper=False)
            if spawning:
                supervisor = threading.Thread(target=supervise, daemon=True)
                supervisor.start()
                supervisor.join()  # returns only when the whole pool is idle
            for t in list(threads):
                t.join()
        finally:
            for env in envs:
                env.cleanup()

        duration = time.time() - start_time
        return RunResult(
            run_id=spec.run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=seeds,
            helpers=helpers,
            duration_seconds=duration,
            metrics=coordination_metrics(bus.task_events(), final_tasks=bus.list_tasks()),
            spawn_metrics=spawn_metrics(bus.spawn_events()) if spawning else {},
        )


__all__ = ["UnifiedHarness", "EnvFactory", "LLMFactory"]
