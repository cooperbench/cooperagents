"""Snapshot committed, non-secret inputs and submit one reproducible CPU job."""

import argparse
import io
import json
import os
import shlex
import subprocess
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
    parser.add_argument("--no-coordinator-notebook", action="store_true")
    parser.add_argument("--coordination-variant", choices=("current", "human_in_loop"), default="current")
    parser.add_argument("--step-limit", type=int, default=1000)
    parser.add_argument("--agent-time-limit", type=int, default=3600)
    parser.add_argument("--collect-trajectories", action="store_true", help="record full I/O and skip official evaluation")
    parser.add_argument("--record-trajectories", action="store_true", help="record full I/O alongside official evaluation")
    parser.add_argument("--langfuse", action="store_true", help="export worker and coordinator traces to Langfuse")
    parser.add_argument("--repair-integrator", action="store_true")
    parser.add_argument("--repair-attempts", type=int, default=1)
    parser.add_argument("--cooperbench-dir", help="Immutable patched CooperBench checkout on the cluster")
    args = parser.parse_args()
    if args.cpus < 1 or args.concurrency < 1 or args.eval_concurrency < 1 or args.repair_attempts < 1:
        parser.error("CPUs, concurrency, and repair attempts must be positive")
    if args.step_limit < 1 or args.agent_time_limit < 1:
        parser.error("Worker step and time limits must be positive")
    if args.coordination_variant == "human_in_loop" and (
        args.mode != "real" or args.no_coordinator or args.no_coordinator_notebook
    ):
        parser.error("human_in_loop requires a real run with coordinator and notebook enabled")
    if args.langfuse and args.mode != "real":
        parser.error("Langfuse tracing requires a real run")
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
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "src", "scripts", "configs", "datasets/cb-mixture-36", "pyproject.toml"], cwd=repo
    )
    if dirty:
        raise RuntimeError("Commit runtime inputs before submitting an immutable snapshot")
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-cooperbench{f'-round{args.round}' if args.round else ''}-{sha[:8]}"
    code = f"{args.root}/code/cooperagents-{run_id}"
    run = f"{args.root}/runs/{run_id}"
    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", "HEAD", "src", "scripts", "datasets/cb-mixture-36/manifest.json", "pyproject.toml"], cwd=repo
    )
    wall_time = args.wall_time or ("00:30:00" if args.mode == "dummy" else "02:00:00")
    metadata = dict(
        run_id=run_id,
        git_commit=sha,
        cooperbench_commit="63b9d44d9f39a02fccf5bf0052db48a917a011fd",
        mode=args.mode,
        collect_trajectories=args.collect_trajectories,
        record_trajectories=args.record_trajectories or args.collect_trajectories,
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
                "completion_gate = true\npresub_merge = false\n"
                f"repair = {str(args.repair_integrator).lower()}\n"
                f"repair_attempts = {args.repair_attempts if args.repair_integrator else 0}\n"
            ),
            "checkpoint.json": '{"applicable":false}',
        }
        if reports:
            records["images.json"] = json.dumps(images)
            records["images.sha256.json"] = json.dumps(checksums)
        for name, value in records.items():
            data = value.encode()
            info = tarfile.TarInfo(f"_run/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    env = os.environ.copy()
    env.setdefault("KRB5CCNAME", str(Path.home() / ".kerberos-cache"))
    q = shlex.quote
    subprocess.run(
        ["ssh", args.host, f"mkdir -p {q(code)} {q(run)}/logs; tar -xf - -C {q(code)}; cp {q(code)}/_run/* {q(run)}/"],
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
        COOPER_STEP_LIMIT=str(args.step_limit),
        COOPER_AGENT_TIME_LIMIT=str(args.agent_time_limit),
        COOPER_REPAIR="1" if args.repair_integrator else "0",
        COOPER_REPAIR_ATTEMPTS=str(args.repair_attempts),
        COOPER_COLLECT_TRAJECTORIES="1" if args.collect_trajectories else "0",
        COOPER_RECORD_TRAJECTORIES="1" if args.record_trajectories else "0",
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
