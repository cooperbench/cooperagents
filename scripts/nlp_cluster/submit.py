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
    args = parser.parse_args()
    if args.cpus < 1:
        parser.error("--cpus must be positive")
    qualification = None
    if args.qualification_report:
        qualification = json.loads(args.qualification_report.read_text())
        task = qualification["task"]
        permitted = {f"{task['repo']}:{task['task_id']}:{','.join(map(str, sorted(pair)))}" for pair in task["pairs"]}
        if not qualification.get("passed") or not set(args.pairs) <= permitted:
            parser.error("Qualification must have passed and cover every requested pair")
    if args.mode == "real" and not args.env_file:
        parser.error("--env-file is required for real runs")
    repo = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "src", "scripts", "configs", "pyproject.toml"], cwd=repo)
    if dirty:
        raise RuntimeError("Commit runtime inputs before submitting an immutable snapshot")
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-cooperbench-{sha[:8]}"
    code = f"{args.root}/code/cooperagents-{run_id}"
    run = f"{args.root}/runs/{run_id}"
    archive = subprocess.check_output(["git", "archive", "--format=tar", "HEAD", "src", "scripts", "pyproject.toml"], cwd=repo)
    wall_time = "00:30:00" if args.mode == "dummy" else "02:00:00"
    metadata = dict(
        run_id=run_id,
        git_commit=sha,
        cooperbench_commit="63b9d44d9f39a02fccf5bf0052db48a917a011fd",
        mode=args.mode,
        pairs=args.pairs,
        runtime="apptainer",
        partition=args.partition,
        cpus=args.cpus,
        step_limit=1000 if args.mode == "real" else 8,
        agent_time_limit=3600 if args.mode == "real" else None,
        wall_time=wall_time,
        memory=args.memory,
        qualification=qualification,
    )
    buffer = io.BytesIO(archive)
    with tarfile.open(fileobj=buffer, mode="a") as tar:
        records = {
            "metadata.json": json.dumps(metadata, indent=2),
            "variant.toml": "workers = 2\ncoordinator = true\ncompletion_gate = true\npresub_merge = false\nrepair = false\n",
            "checkpoint.json": '{"applicable":false}',
        }
        if qualification:
            records["images.json"] = json.dumps({qualification["image"]: qualification["image_path"]})
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
        COOPERBENCH_DIR=f"{args.root}/runtime/cooperbench-63b9d44",
        COOPER_IMAGE_MANIFEST=f"{run}/images.json" if qualification else f"{args.root}/runtime/images.json",
        COOPER_MODE=args.mode,
        COOPER_PAIRS=" ".join(args.pairs),
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
                *([f"--nodelist={qualification['node'].split('.')[0]}"] if qualification else []),
                f"--time={wall_time}",
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
                *([f"--nodelist={qualification['node'].split('.')[0]}"] if qualification else []),
                f"--time={wall_time}",
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
