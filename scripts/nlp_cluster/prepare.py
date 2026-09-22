"""Validate benchmark inputs and build selected task images on an allocated CPU node."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from cooperagents.eval.dataset import dataset_dir, image_name, load_subset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subset", default="all")
    parser.add_argument("--images", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    items = load_subset(args.subset)
    root = dataset_dir()
    for item in items:
        for feature in item.features:
            for name in ("feature.md", "feature.patch", "tests.patch"):
                path = root / item.repo / f"task{item.task_id}" / f"feature{feature}" / name
                if not path.is_file() or not path.stat().st_size:
                    raise FileNotFoundError(path)
    print(f"Validated {len(items)} feature pairs", flush=True)
    if args.check_only:
        return
    if not os.getenv("SLURM_JOB_ID"):
        raise RuntimeError("Build images on an allocated compute node")
    args.images.mkdir(parents=True, exist_ok=True)
    manifest, hashes = {}, {}
    for item in items:
        image = image_name(item.repo, item.task_id)
        if image in manifest:
            continue
        sif = args.images.resolve() / f"{item.repo}-{item.task_id}.sif"
        if not sif.exists():
            temporary = sif.with_suffix(".partial.sif")
            subprocess.run(["apptainer", "pull", "--force", "--arch", "amd64", str(temporary), f"docker://{image}"], check=True)
            temporary.replace(sif)
        with sif.open("rb") as handle:
            hashes[image] = hashlib.file_digest(handle, "sha256").hexdigest()
        manifest[image] = str(sif)
        args.manifest.write_text(json.dumps(manifest, indent=2))
        args.manifest.with_suffix(".sha256.json").write_text(json.dumps(hashes, indent=2))
    print(f"Ready: {len(manifest)} task images", flush=True)


if __name__ == "__main__":
    main()
