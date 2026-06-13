"""CooperAgents command line.

cooperagents validate --subset flash [--limit N] [--max-agents M]
    Offline end-to-end run over real flash specs using LocalEnv + the
    built-in DemoPolicy (no API key, no Docker).  Writes CooperBench-
    compatible outputs and prints the eval command to run.

cooperagents run --subset flash --model <m> [--max-agents M] [--mode ...]
    Live run: DockerEnv (CooperBench task images) + a real LLM via
    litellm.  Writes outputs; pass --eval to score with CooperBench.

cooperagents eval -n <run_name> [--backend docker]
    Run CooperBench's evaluator on a previously-written run.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from cooperagents.bus import InMemoryBus
from cooperagents.env import LocalEnv
from cooperagents.eval.cooperbench import build_eval_command, run_eval, write_run_outputs
from cooperagents.eval.dataset import find_cooperbench, image_name, load_subset, read_feature
from cooperagents.harness import UnifiedHarness
from cooperagents.policies import DemoPolicy
from cooperagents.types import Assignment, TeamSpec


def _spec_for(item, run_id: str, *, max_agents: int | None, mode: str, cooperbench_dir: str | None) -> TeamSpec:
    feats = sorted(item.features)
    if mode == "shared":
        objective = "\n\n---\n\n".join(
            f"## Feature {f}\n\n{read_feature(item.repo, item.task_id, f, cooperbench_dir=cooperbench_dir)}" for f in feats
        )
        return TeamSpec(
            run_id=run_id,
            repo=item.repo,
            task_id=item.task_id,
            features=feats,
            objective=objective,
            team_size=len(feats),
            max_agents=max_agents,
        )
    assignments = [
        Assignment(
            agent_id=f"agent{i + 1}",
            role="lead" if i == 0 else "member",
            feature_id=f,
            task=read_feature(item.repo, item.task_id, f, cooperbench_dir=cooperbench_dir),
        )
        for i, f in enumerate(feats)
    ]
    return TeamSpec(run_id=run_id, repo=item.repo, task_id=item.task_id, features=feats, assignments=assignments, max_agents=max_agents)


def _cmd_validate(args: argparse.Namespace) -> int:
    items = load_subset(args.subset, cooperbench_dir=args.cooperbench_dir)
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("no tasks found", file=sys.stderr)
        return 1
    logs_dir = Path(args.log_dir).resolve()
    print(f"validate: {len(items)} pair(s) from '{args.subset}' (offline, DemoPolicy + LocalEnv)\n")
    n_ok = 0
    n_spawned = 0
    for item in items:
        run_id = uuid.uuid4().hex[:8]
        spec = _spec_for(item, run_id, max_agents=args.max_agents, mode=args.mode, cooperbench_dir=args.cooperbench_dir)
        harness = UnifiedHarness(bus=InMemoryBus(run_id))
        result = harness.run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
        log_dir = write_run_outputs(result, run_name=args.name, logs_dir=logs_dir, model="demo")
        seeds_ok = all(r.status == "submitted" for r in result.seeds.values())
        n_ok += int(seeds_ok)
        n_spawned += result.spawn_metrics.get("granted", 0)
        feat = "+".join(str(f) for f in result.features)
        helpers = f" +{len(result.helpers)} helper(s)" if result.helpers else ""
        print(f"  {'ok ' if seeds_ok else 'ERR'} {item.repo}/{item.task_id} [{feat}]{helpers}  -> {log_dir.relative_to(logs_dir.parent)}")
    print(f"\nseeds-ok: {n_ok}/{len(items)} | helpers spawned: {n_spawned}")
    print("\nnext, score with CooperBench:")
    print("  " + " ".join(build_eval_command(args.name, logs_dir=logs_dir, cooperbench_dir=args.cooperbench_dir)))
    return 0 if n_ok == len(items) else 2


def _make_llm_factory(args: argparse.Namespace):
    """Pick the LLM backend: OpenAI-compatible endpoint if a base URL is
    resolvable (flag or ``AZURE_OPENAI_BASE_URL`` env), else litellm."""
    import os

    base_url = args.base_url or os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if base_url:
        from cooperagents.llm import OpenAIClient

        api_key = os.getenv(args.api_key_env) or os.getenv("OPENAI_API_KEY") or ""
        if not api_key:
            raise SystemExit(f"no API key found (looked in ${args.api_key_env}, $OPENAI_API_KEY)")

        def openai_factory(_agent_id: str, _role: str):
            return OpenAIClient(args.model, base_url=base_url, api_key=api_key)

        return openai_factory, f"openai-compat ({base_url})"

    from cooperagents.llm import LiteLLMClient

    def litellm_factory(_agent_id: str, _role: str):
        return LiteLLMClient(args.model)

    return litellm_factory, "litellm"


def _cmd_run(args: argparse.Namespace) -> int:
    from cooperagents.env.docker import DockerEnv

    items = load_subset(args.subset, cooperbench_dir=args.cooperbench_dir)
    if args.limit:
        items = items[: args.limit]
    logs_dir = Path(args.log_dir).resolve()
    llm_factory, backend_desc = _make_llm_factory(args)
    print(f"run: {len(items)} pair(s), model={args.model} via {backend_desc}, mode={args.mode}, max_agents={args.max_agents}\n")
    for item in items:
        run_id = uuid.uuid4().hex[:8]
        spec = _spec_for(item, run_id, max_agents=args.max_agents, mode=args.mode, cooperbench_dir=args.cooperbench_dir)
        spec.model = args.model
        harness = UnifiedHarness(bus=InMemoryBus(run_id), step_limit=args.step_limit, cost_limit=args.cost_limit)
        img = image_name(item.repo, item.task_id)

        def make_env(_agent_id: str, _img: str = img) -> DockerEnv:
            return DockerEnv(_img)

        result = harness.run(spec, env_factory=make_env, llm_factory=llm_factory)
        log_dir = write_run_outputs(result, run_name=args.name, logs_dir=logs_dir, model=args.model)
        statuses = ",".join(f"{r.role[:1]}:{r.status}" for r in result.seeds.values())
        print(
            f"  {item.repo}/{item.task_id} [{statuses}] +{len(result.helpers)}h -> {log_dir.name}  "
            f"steps={result.total_steps} {result.duration_seconds:.0f}s"
        )
    if args.eval:
        print("\nrunning CooperBench eval...")
        proc = run_eval(args.name, logs_dir=logs_dir, cooperbench_dir=args.cooperbench_dir, backend=args.backend)
        print(proc.stdout if hasattr(proc, "stdout") else proc)
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    logs_dir = Path(args.log_dir).resolve()
    proc = run_eval(
        args.name,
        logs_dir=logs_dir,
        cooperbench_dir=args.cooperbench_dir,
        backend=args.backend,
        concurrency=args.concurrency,
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(" ".join(proc))  # type: ignore[arg-type]
        return 0
    print(proc.stdout)  # type: ignore[union-attr]
    if proc.returncode != 0:  # type: ignore[union-attr]
        print(proc.stderr, file=sys.stderr)  # type: ignore[union-attr]
    return proc.returncode  # type: ignore[union-attr,return-value]


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (no dependency).

    Existing environment variables win; lines starting with ``#`` are ignored.
    """
    import os

    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="cooperagents", description="Unified self-evolving team-of-agents harness")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-n", "--name", default="cooperagents-run", help="run name (log dir)")
        p.add_argument("-s", "--subset", default="flash", help="CooperBench subset (default: flash)")
        p.add_argument("--limit", type=int, default=None, help="cap number of pairs")
        p.add_argument("--max-agents", type=int, default=None, help="cap on total agents (seeds+helpers); raise to allow helpers")
        p.add_argument("--mode", choices=["features", "shared"], default="features")
        p.add_argument("--log-dir", default="logs", help="where to write outputs (default: ./logs)")
        p.add_argument("--cooperbench-dir", default=None, help="path to CooperBench checkout")

    p_val = sub.add_parser("validate", help="offline end-to-end run (no API/Docker)")
    add_common(p_val)
    p_val.set_defaults(func=_cmd_validate)

    p_run = sub.add_parser("run", help="live run (Docker + LLM)")
    add_common(p_run)
    p_run.add_argument("-m", "--model", default="claude-sonnet-4-6", help="model id / deployment name")
    p_run.add_argument(
        "--base-url",
        default=None,
        help="OpenAI-compatible base URL (else $AZURE_OPENAI_BASE_URL / $OPENAI_BASE_URL; falls back to litellm)",
    )
    p_run.add_argument("--api-key-env", default="AZURE_OPENAI_API_KEY", help="env var holding the API key")
    p_run.add_argument("--step-limit", type=int, default=40)
    p_run.add_argument("--cost-limit", type=float, default=5.0)
    p_run.add_argument("--backend", default="docker")
    p_run.add_argument("--eval", action="store_true", help="run CooperBench eval after the run")
    p_run.set_defaults(func=_cmd_run)

    p_eval = sub.add_parser("eval", help="score a prior run with CooperBench")
    p_eval.add_argument("-n", "--name", required=True)
    p_eval.add_argument("--log-dir", default="logs")
    p_eval.add_argument("--cooperbench-dir", default=None)
    p_eval.add_argument("--backend", default="docker")
    p_eval.add_argument("-c", "--concurrency", type=int, default=10)
    p_eval.add_argument("--force", action="store_true")
    p_eval.add_argument("--dry-run", action="store_true", help="print the eval command without running it")
    p_eval.set_defaults(func=_cmd_eval)

    # Surface where CooperBench resolved to (handy for debugging).
    args = parser.parse_args(argv)
    try:
        find_cooperbench(args.cooperbench_dir if hasattr(args, "cooperbench_dir") else None)
    except FileNotFoundError as e:
        if getattr(args, "command", None) in {"validate", "run", "eval"}:
            print(f"error: {e}", file=sys.stderr)
            return 1
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
