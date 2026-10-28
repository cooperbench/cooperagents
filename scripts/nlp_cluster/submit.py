"""Snapshot committed, non-secret inputs and submit one reproducible CPU job."""

import argparse
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="sc")
    parser.add_argument("--root", default="/nlp/scr/chency/projects/cooperagents")
    parser.add_argument("--partition", default="john")
    parser.add_argument("--mode", choices=["dummy", "real"], default="dummy")
    parser.add_argument("--env-file", help="Existing cluster credential file; never copied into records")
    parser.add_argument("--pairs", nargs="+", default=["go_chi_task:27:3,4"])
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory", default="16G")
    parser.add_argument("--qualification-report", type=Path, help="Passed local report; pins the job to its image's node")
    parser.add_argument("--qualification-dir", type=Path, help="Directory of passed task reports for a multi-pair run")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--eval-concurrency", type=int, default=1)
    parser.add_argument("--wall-time", help="Slurm time limit, HH:MM:SS")
    parser.add_argument("--round", type=int, choices=(1, 2, 3))
    parser.add_argument("--no-coordinator", action="store_true")
    parser.add_argument("--initial-plan-dir", type=Path, help="Reviewed planning checkpoints for the selected pairs")
    parser.add_argument("--no-implementation-coordinator", action="store_true",
                        help="skip coordinator model decisions during implementation; retain planning and chat")
    parser.add_argument("--no-coordinator-notebook", action="store_true")
    parser.add_argument("--planning-step-limit", type=int, default=200)
    parser.add_argument("--planning-time-limit", type=int, default=600)
    parser.add_argument("--worker-reminder-interval", type=int, default=10, help="remind every N worker decisions; 0 disables")
    parser.add_argument("--coordination-mode", choices=("direct", "group-chat", "group-chat-planning"), default="direct")
    parser.add_argument("--coordination-variant", choices=("auto-prompt", "human-prompt"), default="auto-prompt")
    parser.add_argument("--step-limit", type=int, default=1000)
    parser.add_argument("--agent-time-limit", type=int, default=3600)
    parser.add_argument("--collect-trajectories", action="store_true", help="record full I/O and skip official evaluation")
    parser.add_argument("--record-trajectories", action="store_true", help="record full I/O alongside official evaluation")
    parser.add_argument("--checkpoint-repair", action="store_true", help="save worker/repair filesystems and full I/O")
    parser.add_argument("--stop-after-repair-checkpoint", action="store_true",
                        help="save before-integrator1 and stop; implies --collect-trajectories")
    parser.add_argument("--only-first-repair-checkpoint", action="store_true",
                        help="save only before-integrator1; requires --stop-after-repair-checkpoint")
    parser.add_argument("--langfuse", action="store_true", help="export worker and coordinator traces to Langfuse")
    parser.add_argument("--repair-integrator", action="store_true")
    parser.add_argument("--repair-attempts", type=int, default=1)
    parser.add_argument("--cooperbench-dir", help="Immutable patched CooperBench checkout on the cluster")
    args = parser.parse_args()
    if args.only_first_repair_checkpoint and not args.stop_after_repair_checkpoint:
        parser.error("--only-first-repair-checkpoint requires --stop-after-repair-checkpoint")
    if args.stop_after_repair_checkpoint:
        if args.mode != "real" or not (args.checkpoint_repair and args.repair_integrator):
            parser.error("--stop-after-repair-checkpoint requires --mode real, --checkpoint-repair and --repair-integrator")
        args.collect_trajectories = True
    if args.no_implementation_coordinator and (args.no_coordinator or args.mode != "real"):
        parser.error("--no-implementation-coordinator requires a real run with coordinator enabled")
    if args.worker_reminder_interval < 0:
        parser.error("Worker reminder interval must be nonnegative")
    if args.planning_step_limit < 1 or args.planning_time_limit < 1:
        parser.error("Planning budgets must be positive")
    if args.cpus < 1 or args.concurrency < 1 or args.eval_concurrency < 1 or args.repair_attempts < 1:
        parser.error("CPUs, concurrency, and repair attempts must be positive")
    if args.step_limit < 1 or args.agent_time_limit < 1:
        parser.error("Worker step and time limits must be positive")
    if args.coordination_variant == "human-prompt" and (args.mode != "real" or args.no_coordinator or args.no_coordinator_notebook):
        parser.error("human-prompt requires a real run with coordinator and notebook enabled")
    if args.coordination_mode != "direct" and (args.mode != "real" or args.no_coordinator or args.no_coordinator_notebook):
        parser.error("group coordination requires a real run with coordinator and notebook")
    if args.langfuse and args.mode != "real":
        parser.error("Langfuse tracing requires a real run")
    if args.checkpoint_repair and args.mode != "real":
        parser.error("Repair checkpoints require a real run")
    if args.qualification_report and args.qualification_dir:
        parser.error("Choose one qualification source")
    reports = []
    if args.qualification_report:
        reports = [json.loads(args.qualification_report.read_text())]
    if args.qualification_dir:
        reports = [json.loads(path.read_text()) for path in sorted(args.qualification_dir.glob("*.json"))]
        if not reports:
            parser.error("Qualification directory contains no reports")
    qualification = reports[0] if len(reports) == 1 else None
    permitted = set()
    images = {}
    checksums = {}
    nodes = set()
    for report in reports:
        task = report["task"]
        if not report.get("passed") or not report.get("sha256") or not report.get("image_path"):
            parser.error("Every qualification report must have passed and contain an image checksum")
        permitted.update(f"{task['repo']}:{task['task_id']}:{','.join(map(str, sorted(pair)))}" for pair in task["pairs"])
        images[report["image"]] = report["image_path"]
        checksums[report["image"]] = report["sha256"]
        nodes.add(report["node"].split(".")[0])
    if reports and (not set(args.pairs) <= permitted or len(nodes) != 1 or len(images) != len(reports)):
        parser.error("Qualification must cover all pairs on one node with distinct task images")
    if args.mode == "real" and not args.env_file:
        parser.error("--env-file is required for real runs")
    repo = Path(__file__).resolve().parents[2]
    initial_plans = {}
    plan_files = {}
    if args.initial_plan_dir is not None:
        if (args.mode != "real" or args.coordination_mode != "group-chat"
                or args.no_coordinator or args.no_coordinator_notebook):
            parser.error("Initial plans require a real group-chat run with coordinator and notebook")
        sys.path.insert(0, str(repo / "src"))
        from cooperagents.initial_plan import InitialPlan, initial_plan_path

        for pair in args.pairs:
            path = initial_plan_path(args.initial_plan_dir, pair)
            plan = InitialPlan.load(path, expected_pair=pair)
            initial_plans[pair] = plan.bundle_sha256
            plan_files[f"data/initial-plans/{path.name}"] = path.read_bytes()
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "src", "scripts", "configs", "datasets/cb-mixture-36", "pyproject.toml"], cwd=repo
    )
    if dirty:
        raise RuntimeError("Commit runtime inputs before submitting an immutable snapshot")
    run_id = (f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-cooperbench-{args.coordination_variant}-{args.coordination_mode}"
              f"{f'-round{args.round}' if args.round else ''}-{sha[:8]}")
    code = f"{args.root}/code/cooperagents-{run_id}"
    run = f"{args.root}/runs/{run_id}"
    archive = subprocess.check_output(
        [
            "git",
            "archive",
            "--format=tar",
            "HEAD",
            "src",
            "scripts",
            "datasets/cb-mixture-36/manifest.json",
            "datasets/cb-mixture-36/pairs.txt",
            "pyproject.toml",
        ],
        cwd=repo,
    )
    wall_time = args.wall_time or ("00:30:00" if args.mode == "dummy" else "02:00:00")
    metadata = dict(
        run_id=run_id,
        git_commit=sha,
        cooperbench_commit="63b9d44d9f39a02fccf5bf0052db48a917a011fd",
        mode=args.mode,
        collect_trajectories=args.collect_trajectories,
        record_trajectories=args.record_trajectories or args.collect_trajectories or args.checkpoint_repair,
        checkpoint_repair=args.checkpoint_repair,
        stop_after_repair_checkpoint=args.stop_after_repair_checkpoint,
        only_first_repair_checkpoint=args.only_first_repair_checkpoint,
        langfuse=args.langfuse,
        official_evaluation=not args.collect_trajectories,
        pairs=args.pairs,
        runtime="apptainer",
        partition=args.partition,
        cpus=args.cpus,
        step_limit=args.step_limit if args.mode == "real" else 8,
        agent_time_limit=args.agent_time_limit if args.mode == "real" else None,
        wall_time=wall_time,
        memory=args.memory,
        qualification=qualification,
        qualifications=reports if args.qualification_dir else None,
        concurrency=args.concurrency,
        eval_concurrency=args.eval_concurrency,
        round=args.round,
        coordinator=not args.no_coordinator,
        coordinator_notebook=not args.no_coordinator and not args.no_coordinator_notebook,
        coordination_variant=args.coordination_variant,
        coordination_mode=args.coordination_mode,
        coordinator_implementation_enabled=not args.no_implementation_coordinator,
        initial_plans=initial_plans,
        planning_step_limit=args.planning_step_limit,
        planning_time_limit=args.planning_time_limit,
        worker_reminder_interval=args.worker_reminder_interval,
        repair_integrator=args.repair_integrator,
        repair_attempts=args.repair_attempts if args.repair_integrator else 0,
    )
    buffer = io.BytesIO(archive)
    with tarfile.open(fileobj=buffer, mode="a") as tar:
        records = {
            "metadata.json": json.dumps(metadata, indent=2),
            "variant.toml": (
                "workers = 2\n"
                f"coordinator = {str(not args.no_coordinator).lower()}\n"
                f"coordinator_notebook = {str(not args.no_coordinator and not args.no_coordinator_notebook).lower()}\n"
                f"coordination_variant = {args.coordination_variant!r}\n"
                f"coordination_mode = {args.coordination_mode!r}\n"
                f"coordinator_implementation_enabled = {str(not args.no_implementation_coordinator).lower()}\n"
                f"planning_step_limit = {args.planning_step_limit}\n"
                f"planning_time_limit = {args.planning_time_limit}\n"
                f"worker_reminder_interval = {args.worker_reminder_interval}\n"
                "completion_gate = true\npresub_merge = false\n"
                f"repair = {str(args.repair_integrator).lower()}\n"
                f"repair_attempts = {args.repair_attempts if args.repair_integrator else 0}\n"
                f"checkpoint_repair = {str(args.checkpoint_repair).lower()}\n"
                f"stop_after_repair_checkpoint = {str(args.stop_after_repair_checkpoint).lower()}\n"
                f"only_first_repair_checkpoint = {str(args.only_first_repair_checkpoint).lower()}\n"
            ),
            "checkpoint.json": json.dumps(
                dict(enabled=args.checkpoint_repair, format="checkpoint-tar-delta-v1",
                     location="logs/real/team/<repo>/<task>/<features>/checkpoints")
            ),
        }
        records.update(plan_files)
        if reports:
            records["images.json"] = json.dumps(images)
            records["images.sha256.json"] = json.dumps(checksums)
        for name, value in records.items():
            data = value if isinstance(value, bytes) else value.encode()
            info = tarfile.TarInfo(f"_run/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    env = os.environ.copy()
    env.setdefault("KRB5CCNAME", str(Path.home() / ".kerberos-cache"))
    q = shlex.quote
    subprocess.run(
        [
            "ssh",
            args.host,
            f"mkdir -p {q(args.root)}/code {q(args.root)}/runs && mkdir {q(code)} && mkdir {q(run)} "
            f"&& mkdir {q(run)}/logs && tar -xf - -C {q(code)} && cp -r {q(code)}/_run/. {q(run)}/",
        ],
        input=buffer.getvalue(),
        env=env,
        check=True,
    )
    exports = dict(
        COOPER_CODE=code,
        COOPER_RUN=run,
        COOPERBENCH_DIR=args.cooperbench_dir or f"{args.root}/runtime/cooperbench-63b9d44",
        COOPER_IMAGE_MANIFEST=f"{run}/images.json" if reports else f"{args.root}/runtime/images.json",
        COOPER_MODE=args.mode,
        COOPER_PAIRS=" ".join(args.pairs),
        COOPER_CONCURRENCY=str(args.concurrency),
        COOPER_EVAL_CONCURRENCY=str(args.eval_concurrency),
        COOPER_COORDINATOR="0" if args.no_coordinator else "1",
        COOPER_COORDINATOR_NOTEBOOK="0" if args.no_coordinator_notebook else "1",
        COOPER_COORDINATION_VARIANT=args.coordination_variant,
        COOPER_COORDINATION_MODE=args.coordination_mode,
        COOPER_COORDINATOR_IMPLEMENTATION_ENABLED="0" if args.no_implementation_coordinator else "1",
        COOPER_INITIAL_PLAN_DIR=f"{run}/data/initial-plans" if initial_plans else "",
        COOPER_PLANNING_STEP_LIMIT=str(args.planning_step_limit),
        COOPER_PLANNING_TIME_LIMIT=str(args.planning_time_limit),
        COOPER_WORKER_REMINDER_INTERVAL=str(args.worker_reminder_interval),
        COOPER_STEP_LIMIT=str(args.step_limit),
        COOPER_AGENT_TIME_LIMIT=str(args.agent_time_limit),
        COOPER_REPAIR="1" if args.repair_integrator else "0",
        COOPER_REPAIR_ATTEMPTS=str(args.repair_attempts),
        COOPER_COLLECT_TRAJECTORIES="1" if args.collect_trajectories else "0",
        COOPER_RECORD_TRAJECTORIES="1" if args.record_trajectories or args.checkpoint_repair else "0",
        COOPER_CHECKPOINT_REPAIR="1" if args.checkpoint_repair else "0",
        COOPER_STOP_AFTER_REPAIR_CHECKPOINT="1" if args.stop_after_repair_checkpoint else "0",
        COOPER_ONLY_FIRST_REPAIR_CHECKPOINT="1" if args.only_first_repair_checkpoint else "0",
        COOPER_LANGFUSE="1" if args.langfuse else "0",
        COOPER_CREDENTIAL_FILE=args.env_file or "",
    )
    command = (
        "export "
        + " ".join(f"{k}={q(v)}" for k, v in exports.items())
        + "; "
        + shlex.join(
            [
                "sbatch",
                "--parsable",
                "--account=nlp",
                f"--partition={args.partition}",
                "--ntasks=1",
                f"--cpus-per-task={args.cpus}",
                f"--mem={args.memory}",
                *([f"--nodelist={next(iter(nodes))}"] if reports else []),
                f"--time={wall_time}",
                *(["--no-requeue"] if args.mode == "real" else []),
                "--job-name=ca-dummy" if args.mode == "dummy" else "--job-name=ca-real",
                f"--output={run}/logs/slurm-%j.out",
                f"{code}/scripts/nlp_cluster/job.sh",
            ]
        )
    )
    # Do not record credential paths in the run record.
    submit_text = (
        "#!/bin/bash\n# Resource request is recorded in metadata.json.\n"
        + shlex.join(
            [
                "sbatch",
                "--account=nlp",
                f"--partition={args.partition}",
                "--ntasks=1",
                f"--cpus-per-task={args.cpus}",
                f"--mem={args.memory}",
                *([f"--nodelist={next(iter(nodes))}"] if reports else []),
                f"--time={wall_time}",
                *(["--no-requeue"] if args.mode == "real" else []),
                f"{code}/scripts/nlp_cluster/job.sh",
            ]
        )
        + "\n"
    )
    subprocess.run(["ssh", args.host, f"cat > {q(run)}/submit.sh"], input=submit_text.encode(), env=env, check=True)
    job = subprocess.check_output(["ssh", args.host, command], env=env, text=True).strip()
    subprocess.run(["ssh", args.host, f"printf %s {q(job)} > {q(run)}/job-id.txt"], env=env, check=True)
    print(json.dumps({"job_id": job, "run_dir": run, "code_dir": code}, indent=2))


if __name__ == "__main__":
    main()
