"""Select a task runtime; Docker remains the default."""

import json
import os
from pathlib import Path

from cooperagents.env.base import Environment


def task_environment(image: str, *, volumes: list[str] | None = None) -> Environment:
    runtime = os.getenv("COOPER_RUNTIME", "docker")
    if runtime == "docker":
        from cooperagents.env.docker import DockerEnv

        return DockerEnv(image, volumes=volumes)
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
    return ApptainerEnv(images[image], scratch=str(scratch), shared=shared)
