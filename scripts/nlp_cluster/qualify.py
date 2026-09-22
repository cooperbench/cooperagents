"""Qualify one selected task image with upstream gold tests, without model calls."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from cooperagents.eval.apptainer import ApptainerEvalBackend
from cooperagents.eval.dataset import image_name


def main():
    selection, output = map(Path, sys.argv[1:3])
    task = json.loads(selection.read_text())["tasks"][int(os.environ["SLURM_ARRAY_TASK_ID"])]
    image = image_name(task["repo"], task["task_id"])
    root = Path(os.environ["COOPER_SCRATCH"])
    root.mkdir(parents=True, exist_ok=True)
    sif = root / "task.sif"
    report = dict(task=task, image=image, node=socket.gethostname(), image_path=str(sif), features={})
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{task['repo']}-{task['task_id']}.json"
    try:
        subprocess.run(["apptainer", "pull", "--arch", "amd64", str(sif), f"docker://{image}"], check=True, timeout=5400)
        with sif.open("rb") as handle:
            report["sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
        from cooperbench.eval.sandbox import run_patch_test

        backend = ApptainerEvalBackend({image: str(sif)}, str(root / "sandboxes"))
        for feature in sorted({f for pair in task["pairs"] for f in pair}):
            result = run_patch_test(
                task["repo"], task["task_id"], feature, backend=backend, dataset_dir=Path(os.environ["COOPERBENCH_DIR"]) / "dataset"
            )
            report["features"][str(feature)] = result
            destination.write_text(json.dumps(report, indent=2))
        report["passed"] = all(r["passed"] and not r["error"] for r in report["features"].values())
    except Exception as exc:
        report.update(passed=False, error=str(exc))
    finally:
        report["seconds"] = time.monotonic() - started
        destination.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "features"}), flush=True)
    raise SystemExit(0 if report.get("passed") else 1)


if __name__ == "__main__":
    main()
