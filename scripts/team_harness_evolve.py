"""Team-harness self-evolution: the main algorithm of
docs/SELF_EVOLUTION_REPRO_PLAN.md as a runnable program.

Harness representation: CODE-SNAPSHOT VERSIONS. A harness version is a
self-contained folder

    <versions_root>/<name>/
        manifest.json     name, parent, idea, args, entry, created
        src/              the harness package (cooperagents/, vendored agent)
        scripts/          the benchmark runner(s)
        delta.patch       diff vs parent (audit trail; empty for seeds)

The code in the folder IS the identity. Rationale (supersedes the earlier
config-as-flags identity): (1) reproducibility — mainline code drift
silently changes what an old flag set measures; a snapshot pins exact
behavior with no cross-version interference; (2) range of innovations —
ideas are arbitrary code edits to the child folder, with `args` kept only
as a per-version CLI convenience.

Core objects
  TeamHarness  load/save/derive/execute a version folder (also loads the
               legacy submodule/JSON flag-set forms by snapshotting the
               current repo).
  Idea         proposal text + target failure class + args delta and/or a
               code_edit callable applied to the child folder.
  evolve()     measure seed -> propose -> derive child version -> measure
               k reps (fanned over fleet workers) -> accept/reject by
               noise band -> log; single mainline lineage.

Usage
  .venv/bin/python scripts/team_harness_evolve.py evolve \
      --seed cooperagents.harnesses.coopgit \
      --instance abishekvashok__cmatrix.5c082c6 --k 3 --budget 6

  .venv/bin/python scripts/team_harness_evolve.py run \
      --harness runs/evolve/versions/E1 --instance <id> --rep test1
"""

from __future__ import annotations

import argparse
import importlib
import json
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NODES_FILE = REPO / "scripts" / "fleet" / "nodes.txt"
PYTHON = REPO / ".venv" / "bin" / "python"
PROGRAMBENCH = Path.home() / "ProgramBench"
SSH = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
       "-i", str(Path.home() / ".ssh" / "fleet_key")]

_SNAPSHOT_ITEMS = ["src", "scripts", "pyproject.toml"]

_ARG_CLI = {
    "arm": lambda v: ["--arm", str(v)],
    "step_limit": lambda v: ["--step-limit", str(v)],
    "agent_time_limit": lambda v: ["--agent-time-limit", str(v)],
    "repair": lambda v: ["--repair"] if v else [],
    "completion_gate": lambda v: ["--completion-gate"] if v else [],
    "env_brief": lambda v: ["--env-brief"] if v else [],
}


class TeamHarness:
    """One harness version: a self-contained code folder + manifest."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.manifest = json.loads((self.root / "manifest.json").read_text())

    name = property(lambda self: self.manifest["name"])
    args = property(lambda self: self.manifest.get("args", {}))
    lineage = property(lambda self: self.manifest.get("lineage", []))

    # -- create / load / save -------------------------------------------
    @classmethod
    def snapshot(cls, name: str, dest_root: Path, *, source_repo: Path = REPO,
                 args: dict | None = None, parent: TeamHarness | None = None,
                 idea_name: str | None = None,
                 entry: str = "scripts/bench_programbench.py") -> TeamHarness:
        """Freeze code into a new version folder (from the live repo for
        seeds, or from a parent version for derived harnesses)."""
        root = Path(dest_root) / name
        if root.exists():
            raise FileExistsError(root)
        root.mkdir(parents=True)
        src = parent.root if parent else source_repo
        for item in _SNAPSHOT_ITEMS:
            s = src / item
            if s.is_dir():
                shutil.copytree(s, root / item,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif s.exists():
                shutil.copy2(s, root / item)
        manifest = {
            "name": name,
            "parent": parent.name if parent else None,
            "idea": idea_name,
            "lineage": (parent.lineage + [idea_name]) if parent and idea_name else (parent.lineage if parent else []),
            "args": dict(args if args is not None else (parent.args if parent else {})),
            "entry": entry,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return cls(root)

    @classmethod
    def load(cls, source: str, *, versions_root: Path | None = None) -> TeamHarness:
        """Load a version folder; or materialize a legacy flag-set source
        (submodule / JSON file) by snapshotting the CURRENT repo code."""
        p = Path(source)
        if p.is_dir() and (p / "manifest.json").exists():
            return cls(p)
        if p.suffix == ".json" and p.exists():
            d = json.loads(p.read_text())
            flags, name = d["flags"], d["name"]
        else:
            d = importlib.import_module(source).HARNESS
            flags, name = d["flags"], d["name"]
        root = versions_root or (REPO / "runs" / "evolve" / "versions")
        dest = root / name
        if dest.exists():
            return cls(dest)  # already materialized
        return cls.snapshot(name, root, args=flags)

    def derive(self, name: str, idea: Idea) -> TeamHarness:
        """Child version = parent code copy + idea applied (args merge
        and/or arbitrary code edit), with a diff recorded for audit."""
        child = TeamHarness.snapshot(name, self.root.parent, parent=self,
                                     args={**self.args, **idea.delta},
                                     idea_name=idea.name)
        if idea.code_edit is not None:
            idea.code_edit(child.root)
        diff = subprocess.run(
            ["diff", "-ru", "-x", "manifest.json", "-x", "delta.patch",
             str(self.root), str(child.root)],
            capture_output=True, text=True)
        (child.root / "delta.patch").write_text(diff.stdout)
        return child

    # -- execute --------------------------------------------------------
    def cli_args(self) -> list[str]:
        out: list[str] = []
        for k, v in self.args.items():
            out += _ARG_CLI[k](v)
        return out

    def execute(self, instance: str, rep: str, *, node: str | None = None,
                runs_dir: Path = REPO / "runs") -> dict:
        """Run THIS VERSION'S code on one instance; return metric tiers.

        The runner is invoked from the version folder, so `sys.path` picks
        up the version's own src/ — mainline drift cannot leak in. Local:
        blocking subprocess + eval. Fleet node: ship the folder, run
        detached, block on the .DONE marker (callers parallelize reps with
        a thread pool)."""
        arm = self.args.get("arm", "solo")
        run_name = f"pb-{arm}-{rep}"
        entry = self.manifest["entry"]
        if node is None:
            subprocess.run([str(PYTHON), str(self.root / entry), "--instance", instance,
                            "--rep", rep, *self.cli_args(), "--runs-dir", str(runs_dir)],
                           check=True, cwd=REPO)
            subprocess.run(["uv", "run", "programbench", "eval", str(runs_dir / run_name)],
                           cwd=PROGRAMBENCH, check=False, capture_output=True)
            return _read_metrics(runs_dir / run_name, instance)

        rdir = f"harness_versions/{self.name}"
        subprocess.run(["rsync", "-az", "-e", " ".join(SSH), "--delete",
                        f"{self.root}/", f"ubuntu@{node}:{rdir}/"], check=True)
        job = f"""#!/bin/bash
cd $HOME/CooperAgents && set -a && source .env.qwen && set +a
export PATH="$HOME/.local/bin:$PATH"
$HOME/CooperAgents/.venv/bin/python $HOME/{rdir}/{entry} \\
  --instance {shlex.quote(instance)} --rep {shlex.quote(rep)} \\
  {" ".join(self.cli_args())} --runs-dir $HOME/CooperAgents/runs \\
  > runs/{run_name}.launch.log 2>&1
d=runs/{run_name}
[ -d "$d/{instance}" ] && (cd $HOME/ProgramBench && uv run programbench eval $HOME/CooperAgents/$d) >> "$d/eval.log" 2>&1
echo "rc=$? $(date -u +%FT%TZ)" > runs/{run_name}.DONE
"""
        jp = f"/tmp/evojob_{self.name}_{rep}.sh"
        subprocess.run([*SSH, f"ubuntu@{node}", f"cat > {jp}"], input=job, text=True, check=True)
        subprocess.run([*SSH, f"ubuntu@{node}",
                        f"chmod +x {jp} && setsid nohup {jp} </dev/null >/dev/null 2>&1 &"],
                       stdin=subprocess.DEVNULL, timeout=20, check=False)
        marker = f"CooperAgents/runs/{run_name}.DONE"
        while subprocess.run([*SSH, f"ubuntu@{node}", f"test -f {marker}"],
                             stdin=subprocess.DEVNULL, capture_output=True).returncode != 0:
            time.sleep(120)
        subprocess.run(["rsync", "-az", "-e", " ".join(SSH),
                        f"ubuntu@{node}:CooperAgents/runs/{run_name}", str(runs_dir) + "/"],
                       check=False)
        return _read_metrics(runs_dir / run_name, instance)


def _read_metrics(run_dir: Path, instance: str) -> dict:
    """The three metric tiers of the plan's Record representation."""
    out: dict = {"run_dir": str(run_dir), "score": 0.0}
    ev = next(iter((run_dir / instance).glob("*.eval.json")), None)
    if ev:
        tr = json.loads(ev.read_text())["test_results"]
        out["score"] = round(100 * sum(1 for t in tr if t["status"] == "passed") / len(tr), 1)
    meta = run_dir / instance / "run_meta.txt"
    if meta.exists():
        for tok in meta.read_text().split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                out.setdefault(k, v)
    m = run_dir / instance / "metrics.json"
    if m.exists():
        out["diagnostic"] = json.loads(m.read_text())
    return out


# -- ideas and proposers ------------------------------------------------

@dataclass
class Idea:
    name: str                 # lineage entry
    proposal: str             # representation 1: logged verbatim
    target_class: str
    delta: dict               # args delta (may be empty)
    priority: int = 0
    code_edit=None            # optional callable(root: Path) -> None: arbitrary
                              # code change applied to the CHILD version folder


TAXONOMY: list[Idea] = [
    Idea("repair", "mechanical build gate + repair agent on the integrated tree",
         "unbuildable merge", {"repair": True}, 100),
    Idea("time-cap", "per-agent wall-clock cap bounds wait-loop tails",
         "agent loop/stall", {"agent_time_limit": 3600}, 90),
    Idea("coordinator", "loop/stall/collision monitor with LLM-composed nudges",
         "agent loop/stall", {"arm": "coopgitc2"}, 80),
    Idea("completion-gate", "reject agent finish until compile.sh builds a fresh executable in its own container",
         "submitted-but-broken", {"completion_gate": True}, 70),
    Idea("env-brief", "probed toolchain list + operational no-network constraint in the task",
         "offline-unbuildable deps", {"env_brief": True}, 60),
]


class TaxonomyProposer:
    def __call__(self, harness: TeamHarness, d_results: dict) -> Idea | None:
        for idea in sorted(TAXONOMY, key=lambda i: -i.priority):
            if idea.name not in harness.lineage and idea.name not in d_results:
                return idea
        return None


# -- the loop -----------------------------------------------------------

def _measure(h: TeamHarness, instance: str, k: int, nodes: list[str],
             tag: str, runs_dir: Path) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(nodes) or 1)) as ex:
        futs = [ex.submit(h.execute, instance, f"{tag}-r{i+1}",
                          node=(nodes[i % len(nodes)] if nodes else None),
                          runs_dir=runs_dir)
                for i in range(k)]
        return [f.result() for f in futs]


def evolve(seed: TeamHarness, instance: str, *, k: int = 3, budget: int = 6,
           noise_band: float = 25.0, proposer=None, use_fleet: bool = True,
           out_dir: Path = REPO / "runs" / "evolve") -> TeamHarness:
    """Serialized mainline evolution (plan's loop; reps parallelize).

    One idea in flight at a time; k reps fan out across workers. The async
    multi-idea variant with rebase-on-drift is described in the plan doc."""
    proposer = proposer or TaxonomyProposer()
    nodes = [ln.strip() for ln in NODES_FILE.read_text().splitlines() if ln.strip()] if use_fleet else []
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "EVOLUTION_LOG.md"
    d_results: dict[str, dict] = {}

    def log_entry(text: str) -> None:
        with log.open("a") as f:
            f.write(text + "\n")
        print(text)

    e = seed
    base = _measure(e, instance, k, nodes, f"{e.name}-evo0", out_dir)
    base_mean = sum(r["score"] for r in base) / k
    d_results[e.name] = {"base": None, "scores": [r["score"] for r in base], "decision": "seed"}
    log_entry(f"## E0 = {e.name} ({e.root}): scores={[r['score'] for r in base]} mean={base_mean:.1f}")

    for i in range(1, budget + 1):
        idea = proposer(e, d_results)
        if idea is None:
            log_entry(f"iteration {i}: proposer exhausted; stop")
            break
        cand = e.derive(f"E{i}", idea)
        log_entry(f"## iteration {i}: idea={idea.name} target={idea.target_class}\n"
                  f"proposal: {idea.proposal}\nversion: {cand.root}")
        res = _measure(cand, instance, k, nodes, f"evo{i}-{idea.name}", out_dir)
        scores = [r["score"] for r in res]
        mean = sum(scores) / k
        accepted = (mean - base_mean) > noise_band
        d_results[idea.name] = {"base": e.name, "version": str(cand.root),
                                "scores": scores,
                                "decision": "accepted" if accepted else "rejected"}
        log_entry(f"scores={scores} mean={mean:.1f} (baseline {base_mean:.1f}, "
                  f"band ±{noise_band}) -> {'ACCEPTED' if accepted else 'rejected'}")
        if accepted:
            e, base_mean = cand, mean
    (out_dir / "d_results.json").write_text(json.dumps(d_results, indent=2))
    log_entry(f"## final: {e.name} lineage={e.lineage} mean={base_mean:.1f} version={e.root}")
    return e


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evolve")
    ev.add_argument("--seed", default="cooperagents.harnesses.coopgit",
                    help="version folder, submodule, or legacy flags JSON")
    ev.add_argument("--instance", required=True)
    ev.add_argument("--k", type=int, default=3)
    ev.add_argument("--budget", type=int, default=6)
    ev.add_argument("--noise-band", type=float, default=25.0)
    ev.add_argument("--local", action="store_true")
    rn = sub.add_parser("run")
    rn.add_argument("--harness", required=True, help="version folder, submodule, or JSON")
    rn.add_argument("--instance", required=True)
    rn.add_argument("--rep", required=True)
    rn.add_argument("--node", default=None)
    args = ap.parse_args()

    if args.cmd == "evolve":
        seed = TeamHarness.load(args.seed)
        final = evolve(seed, args.instance, k=args.k, budget=args.budget,
                       noise_band=args.noise_band, use_fleet=not args.local)
        print(f"final harness: {final.name} at {final.root}")
    else:
        h = TeamHarness.load(args.harness)
        print(json.dumps(h.execute(args.instance, args.rep, node=args.node), indent=2))


if __name__ == "__main__":
    main()
