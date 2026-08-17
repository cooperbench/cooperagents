"""Export ProgramBench run trajectories to a HuggingFace dataset for the
reports data viewer.

Layout on the Hub (dataset CooperBench/cooperagents-programbench-traces):
  index.json                 [{run, instance, arm, rep, batch, score,
                               duration_s, steps, patch_bytes, agents:[...]}]
  runs/<run>.json            {meta: {...}, agents: {agent_id: {status, steps,
                               error, messages:[...]}}, repair_meta: {...}}

The viewer (reports repo) fetches index.json once and one runs/<run>.json
per selection via the CORS-enabled resolve endpoint.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / "runs"
OUT = RUNS / "hf_export"

BATCH_PATTERNS = [  # (regex on rep suffix, batch label)
    (r"-t[34]i7", "scalability-t3t4"),
    (r"-i7", "iteration7-presub-merge"),
    (r"-i6", "iteration6-gate-brief"),
    (r"-m1", "mbench10-baseline"),
    (r"-i5r", "iteration5-fitness"),
    (r"-i[34]r", "iterations3-4"),
    (r"", "earlier"),
]


def batch_for(run: str) -> str:
    for pat, label in BATCH_PATTERNS:
        if re.search(pat, run):
            return label
    return "earlier"


def score_of(inst_dir: Path) -> float | None:
    ev = next(iter(inst_dir.glob("*.eval.json")), None)
    if not ev:
        return None
    try:
        tr = json.loads(ev.read_text())["test_results"]
        return round(100 * sum(1 for t in tr if t["status"] == "passed") / len(tr), 1)
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "runs").mkdir(exist_ok=True)
    index = []
    for run_dir in sorted(RUNS.glob("pb-*/")):
        run = run_dir.name
        inst_dirs = [d for d in run_dir.iterdir() if d.is_dir() and (d / "trajectories").is_dir()]
        if not inst_dirs:
            continue
        inst_dir = inst_dirs[0]
        meta_txt = (inst_dir / "run_meta.txt")
        meta = {}
        if meta_txt.exists():
            meta = dict(t.split("=", 1) for t in meta_txt.read_text().split() if "=" in t)
        agents = {}
        for t in sorted((inst_dir / "trajectories").glob("*.json")):
            try:
                agents[t.stem] = json.loads(t.read_text())
            except Exception:  # noqa: BLE001
                continue
        if not agents:
            continue
        repair_meta = {}
        mj = inst_dir / "metrics.json"
        if mj.exists():
            try:
                repair_meta = json.loads(mj.read_text())
            except Exception:  # noqa: BLE001
                pass
        patch = ""
        pf = inst_dir / "integrated.patch"
        if pf.exists():
            patch = pf.read_text(errors="replace")
            if len(patch) > 300_000:
                patch = patch[:200_000] + "\n…[patch clipped for viewer]…\n" + patch[-50_000:]
        entry = {
            "run": run,
            "instance": inst_dir.name,
            "arm": meta.get("arm", run.split("-")[1] if "-" in run else "?"),
            "rep": meta.get("rep", ""),
            "batch": batch_for(run),
            "score": score_of(inst_dir),
            "duration_s": int(meta.get("duration_s", 0) or 0),
            "steps": int(meta.get("steps", 0) or 0),
            "patch_bytes": int(meta.get("patch_bytes", 0) or 0),
            "agents": sorted(agents),
        }
        index.append(entry)
        (OUT / "runs" / f"{run}.json").write_text(json.dumps(
            {"meta": entry, "agents": agents, "repair_meta": repair_meta,
             "integrated_patch": patch},
            ensure_ascii=False))
    (OUT / "index.json").write_text(json.dumps(index, indent=1))
    total = sum(f.stat().st_size for f in (OUT / "runs").glob("*.json"))
    print(f"exported {len(index)} runs, {total/1e6:.0f}MB -> {OUT}")


if __name__ == "__main__":
    main()
    if "--upload" in sys.argv:
        from huggingface_hub import HfApi

        api = HfApi()
        repo = "CooperBench/cooperagents-programbench-traces"
        api.create_repo(repo, repo_type="dataset", exist_ok=True)
        api.upload_folder(folder_path=str(OUT), repo_id=repo, repo_type="dataset",
                          commit_message="ProgramBench team-harness trajectories (baseline + iterations 5-7 + scalability)")
        print(f"uploaded to https://huggingface.co/datasets/{repo}")
