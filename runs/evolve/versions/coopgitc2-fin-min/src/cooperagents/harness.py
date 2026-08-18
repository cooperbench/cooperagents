"""Minimal coopgitc2 harness — the exact fin3 orchestration slice.

Extracted from the full UnifiedHarness (1665 lines) without behavior change:
the three support classes (_GitShareSync, _TeammatePoller, _Coordinator) are
VERBATIM copies; run_coopgitc2_team() reproduces the coop-isolated path for
the fin3 flag set (git_share + coordinator + coop_tools + selection
integration), including the exact teammates prompt block and the exact
run_mini_swe_agent keyword mapping. Nothing else from the original module
is reachable under this configuration.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from cooperagents.patching import strip_test_sections
from cooperagents.types import Assignment
from cooperagents.workers.mini_swe_worker import BusComm, run_mini_swe_agent

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




def run_coopgitc2_team(
    *,
    assignments: list[Assignment],
    env_factory,
    bus,
    model: str,
    step_limit: int,
    agent_time_limit: int | None,
    completion_gate,
    select_integration,
    coordinator_on: bool,
    git_share: bool,
    command_timeout: int = 300,
    cost_limit: float = 5.0,
) -> SimpleNamespace:
    """The fin3 slice of UnifiedHarness._run_isolated, verbatim in behavior."""
    envs = {a.agent_id: env_factory(a.agent_id) for a in assignments}
    gitsync = None
    if git_share:
        first = True
        for _aid, _e in envs.items():
            if first:
                _e.execute(f"git init -q --bare {_GITSHARE} 2>/dev/null || true")
                first = False
            _e.execute(f"git remote add shared {_GITSHARE} 2>/dev/null || true")
            _e.execute(f"git push -q shared HEAD:refs/heads/{_aid} 2>/dev/null || true")
        gitsync = _GitShareSync(envs)
        threading.Thread(target=gitsync.run, daemon=True).start()
    coordinator = _Coordinator(envs, model) if coordinator_on else None
    if coordinator is not None:
        threading.Thread(target=coordinator.run, daemon=True).start()

    seeds: dict[str, object] = {}
    patches: dict[str, str] = {}

    def run_one(a: Assignment):
        env = envs[a.agent_id]
        mates = ", ".join(
            f"{x.agent_id} (feature {x.feature_id})" for x in assignments if x.agent_id != a.agent_id
        )
        task = a.task + (
            f"\n\nTEAMMATES: {mates} are implementing their features RIGHT NOW in parallel "
            "copies of this repo; your diffs will be merged at the end. Coordinate via the "
            "send_message tool (incoming messages appear as [Message from ...]): agree on "
            "shared names/signatures early and avoid editing the same regions."
        ) if len(assignments) > 1 else a.task
        poller = _TeammatePoller(a.agent_id, envs) if (git_share or coordinator_on) else None
        if poller is not None and git_share:
            poller.watch_gitshare()
        if poller is not None and coordinator is not None:
            poller.watch_coordinator(coordinator)
        r = run_mini_swe_agent(
            env,
            task=task,
            agent_id=a.agent_id,
            role=a.role,
            model_name=model,
            step_limit=step_limit,
            cost_limit=cost_limit,
            feature_id=a.feature_id,
            command_timeout=command_timeout,
            guard_git=False,
            temperature=0.0,
            comm=BusComm(bus, a.agent_id) if len(assignments) > 1 else None,
            poller=poller,
            tool_protocol=False,
            task_board=None,
            wait_protocol=False,
            spawn_handler=None,
            time_limit_s=agent_time_limit,
            monitor=coordinator,
            git_share=git_share,
            completion_gate=completion_gate,
        )
        return a.agent_id, r, strip_test_sections(env.git_diff())

    from concurrent.futures import ThreadPoolExecutor

    try:
        with ThreadPoolExecutor(max_workers=len(assignments)) as ex:
            for aid, r, diff in ex.map(run_one, assignments):
                seeds[aid] = r
                patches[aid] = diff
    finally:
        if gitsync is not None:
            gitsync.stop()
        if coordinator is not None:
            coordinator.stop()
        for e in envs.values():
            e.cleanup()

    ordered = [patches[a.agent_id] for a in assignments]
    if select_integration is not None and len(ordered) > 1:
        idx = select_integration(ordered)
        integrated = ordered[idx]
        print(f"[harness] integration=selected chosen={idx} sizes={[len(p) for p in ordered]}")
    else:
        integrated = ordered[0] if ordered else ""

    metrics: dict = {"statuses": {aid: r.status for aid, r in seeds.items()}}
    if coordinator is not None:
        metrics["coordinator_events"] = coordinator.events()
    return SimpleNamespace(
        seeds=seeds,
        integrated=SimpleNamespace(patch=integrated),
        total_steps=sum(r.steps for r in seeds.values()),
        metrics=metrics,
    )
