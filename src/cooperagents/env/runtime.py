"""Select a task runtime; Docker remains the default."""

import json
import os
from pathlib import Path

from cooperagents.env.base import Environment


def task_environment(image: str, *, volumes: list[str] | None = None, coordinator_dir: Path | None = None) -> Environment:
    if coordinator_dir is not None:
        coordinator_dir = Path(coordinator_dir).resolve(strict=True)
        if not coordinator_dir.is_dir():
            raise ValueError("Coordinator mount must be a directory")
    runtime = os.getenv("COOPER_RUNTIME", "docker")
    if runtime == "docker":
        from cooperagents.env.docker import DockerEnv

        mounts = list(volumes or [])
        if coordinator_dir is not None:
            mounts.append(f"{coordinator_dir}:/coordination:ro")
        return DockerEnv(image, volumes=mounts or None)
    if runtime != "apptainer":
        raise ValueError(f"Unknown runtime: {runtime}")
    from cooperagents.env.apptainer import ApptainerEnv

    images = json.loads(Path(os.environ["COOPER_IMAGE_MANIFEST"]).read_text())
    scratch = Path(os.environ["COOPER_SCRATCH"]).resolve()
    shared = None
    for volume in volumes or []:
        name, mount = volume.split(":", 1)
        if mount != "/cbshared" or not name.isalnum():
            raise ValueError(f"Unsupported Apptainer shared volume: {volume}")
        shared = str(scratch / "shared" / name)
    return ApptainerEnv(images[image], scratch=str(scratch), shared=shared, coordinator_dir=coordinator_dir)
