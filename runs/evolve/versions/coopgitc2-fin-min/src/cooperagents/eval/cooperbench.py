"""Bridge harness output into CooperBench's evaluator.

We don't modify CooperBench, so we meet it where it already looks: its
``discover_runs`` scans ``logs/<run>/<setting>/<repo>/<task>/<f1>_<f2>/`` for
``solo|coop|team`` and scores ``agent{fid}.patch`` per feature.  We write our
results in exactly that layout under ``setting="team"`` (the multi-agent
bucket) and then invoke ``cooperbench eval``.

Per-feature seed patches are what get scored; helper/member contributions
reach the score through the seed agent that integrates them.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from cooperagents.eval.dataset import find_cooperbench
from cooperagents.patching import strip_for_submission
from cooperagents.types import RunResult

DEFAULT_SETTING = "team"


def write_run_outputs(
    result: RunResult,
    *,
    run_name: str,
    logs_dir: str | Path,
    setting: str = DEFAULT_SETTING,
    agent_framework: str = "cooperagents",
    model: str = "scripted",
) -> Path:
    """Serialize a :class:`RunResult` into the CooperBench log layout.

    Returns the per-pair log directory that was written.
    """
    feature_str = "_".join(f"f{f}" for f in sorted(result.features))
    log_dir = Path(logs_dir) / run_name / setting / result.repo / str(result.task_id) / feature_str
    log_dir.mkdir(parents=True, exist_ok=True)

    if setting == "solo":
        # Solo baseline: one agent owns both features; CooperBench scores a
        # single ``solo.patch`` applied for the whole pair.  Prefer the
        # integrated diff (shared-workspace runs compute the canonical diff
        # from the tree, since per-agent patch may be left empty by the worker).
        seed = next(iter(result.seeds.values()), None)
        if result.integrated is not None:
            patch = result.integrated.patch
        else:
            patch = seed.patch if seed else ""
        (log_dir / "solo.patch").write_text(strip_for_submission(patch))
        if seed is not None:
            (log_dir / "solo_traj.json").write_text(
                json.dumps(
                    {"repo": result.repo, "task_id": result.task_id, "messages": seed.messages, "steps": seed.steps},
                    indent=2,
                    default=str,
                )
            )
    elif result.integrated is not None:
        # Shared-workspace team: one coherent diff implements BOTH features.
        # CooperBench's merged eval applies agent{f1}.patch and runs *both*
        # feature suites against the tree, so put the whole integrated diff in
        # the first slot and leave the rest empty (scored as a clean merge).
        feats = sorted(result.features)
        submit = strip_for_submission(result.integrated.patch)
        (log_dir / f"agent{feats[0]}.patch").write_text(submit)
        for fid in feats[1:]:
            (log_dir / f"agent{fid}.patch").write_text("")
        (log_dir / "integrated.patch").write_text(submit)
        for seed in result.seeds.values():
            (log_dir / f"{seed.agent_id}_traj.json").write_text(
                json.dumps(
                    {"agent_id": seed.agent_id, "role": seed.role, "steps": seed.steps, "messages": seed.messages}, indent=2, default=str
                )
            )
    else:
        # One patch + trajectory per feature-owning seed agent — what eval reads.
        seeds_by_feature = {r.feature_id: r for r in result.seeds.values() if r.feature_id is not None}
        for fid in result.features:
            seed = seeds_by_feature.get(fid)
            patch = strip_for_submission(seed.patch) if seed else ""
            (log_dir / f"agent{fid}.patch").write_text(patch)
            if seed is not None:
                (log_dir / f"agent{fid}_traj.json").write_text(
                    json.dumps(
                        {
                            "repo": result.repo,
                            "task_id": result.task_id,
                            "feature_id": fid,
                            "agent_id": seed.agent_id,
                            "role": seed.role,
                            "status": seed.status,
                            "cost": seed.cost,
                            "steps": seed.steps,
                            "messages": seed.messages,
                        },
                        indent=2,
                        default=str,
                    )
                )

    # Helper patches are auxiliary artifacts (not scored directly).
    for helper_id, r in result.helpers.items():
        (log_dir / f"{helper_id}.patch").write_text(r.patch)

    result_data = {
        "repo": result.repo,
        "task_id": result.task_id,
        "features": sorted(result.features),
        "setting": setting,
        "harness": "cooperagents-unified",
        "run_id": result.run_id,
        "run_name": run_name,
        "agent_framework": agent_framework,
        "model": model,
        "completed_at": datetime.now(UTC).isoformat(),
        "duration_seconds": result.duration_seconds,
        "agents": {
            r.agent_id: {
                "feature_id": r.feature_id,
                "role": r.role,
                "status": r.status,
                "cost": r.cost,
                "steps": r.steps,
                "patch_lines": r.patch_lines,
                "error": r.error,
            }
            for r in result.seeds.values()
        },
        "helpers": {
            r.agent_id: {
                "role": r.role,
                "status": r.status,
                "cost": r.cost,
                "steps": r.steps,
                "patch_lines": r.patch_lines,
                "error": r.error,
            }
            for r in result.helpers.values()
        },
        "total_cost": result.total_cost,
        "total_steps": result.total_steps,
        "metrics": result.metrics,
        "spawn_metrics": result.spawn_metrics,
        "log_dir": str(log_dir),
    }
    (log_dir / "result.json").write_text(json.dumps(result_data, indent=2, default=str))
    return log_dir


def build_eval_command(
    run_name: str,
    *,
    logs_dir: str | Path,
    cooperbench_dir: str | Path | None = None,
    backend: str = "docker",
    concurrency: int = 10,
    force: bool = False,
) -> list[str]:
    """Construct the ``cooperbench eval`` argv (so callers can inspect/dry-run)."""
    cb = find_cooperbench(str(cooperbench_dir) if cooperbench_dir else None)
    cmd = [
        "cooperbench",
        "eval",
        "-n",
        run_name,
        "--log-dir",
        str(Path(logs_dir).resolve()),
        "--dataset-dir",
        str((cb / "dataset").resolve()),
        "--backend",
        backend,
        "-c",
        str(concurrency),
    ]
    if force:
        cmd.append("--force")
    return cmd


def run_eval(
    run_name: str,
    *,
    logs_dir: str | Path,
    cooperbench_dir: str | Path | None = None,
    backend: str = "docker",
    concurrency: int = 10,
    force: bool = False,
    use_uv: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess | list[str]:
    """Invoke CooperBench's evaluator on previously-written outputs.

    Runs ``cooperbench eval`` inside the CooperBench checkout (via ``uv run``
    by default, so its own environment/deps are used).  ``dry_run`` returns
    the argv without executing — used by tests that have no Docker.
    """
    cb = find_cooperbench(str(cooperbench_dir) if cooperbench_dir else None)
    cmd = build_eval_command(run_name, logs_dir=logs_dir, cooperbench_dir=cb, backend=backend, concurrency=concurrency, force=force)
    if use_uv:
        cmd = ["uv", "run", *cmd]
    if dry_run:
        return cmd
    return subprocess.run(cmd, cwd=str(cb), capture_output=True, text=True)


__all__ = ["write_run_outputs", "build_eval_command", "run_eval", "DEFAULT_SETTING"]
