"""DockerEnv coverage — gated on a working Docker daemon.

CooperBench task images are Debian-based with bash, git, and the repo at
/workspace/repo.  These tests stand up a tiny equivalent (alpine + bash +
git + an empty repo) so the Docker execution path is exercised without
needing a built CooperBench image.  Everything skips cleanly when Docker
isn't available (e.g. CI without a daemon).
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from cooperagents.agent import Agent
from cooperagents.bus.memory import InMemoryBus
from cooperagents.llm import Action, ScriptedLLM

_DOCKERFILE = """
FROM alpine:3.19
RUN apk add --no-cache git bash
RUN git config --global user.email a@b.c && git config --global user.name a \
    && mkdir -p /workspace/repo && cd /workspace/repo && git init -q && git commit --allow-empty -qm base
"""


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker not available")


@pytest.fixture(scope="module")
def test_image(tmp_path_factory):
    tag = f"ca-dockerenv-test-{uuid.uuid4().hex[:8]}"
    ctx = tmp_path_factory.mktemp("img")
    (ctx / "Dockerfile").write_text(_DOCKERFILE)
    build = subprocess.run(["docker", "build", "-t", tag, str(ctx)], capture_output=True, text=True)
    if build.returncode != 0:
        pytest.skip(f"could not build test image: {build.stderr[-300:]}")
    yield tag
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def test_docker_env_runs_agent_and_captures_diff(test_image):
    from cooperagents.env.docker import DockerEnv

    env = DockerEnv(test_image)
    assert env._base_commit  # base commit captured from the image
    try:
        agent = Agent(
            agent_id="a1",
            role="lead",
            task="make a file",
            env=env,
            bus=InMemoryBus("d"),
            llm=ScriptedLLM(
                {
                    "*": [
                        Action(tool="write_file", args={"path": "made.txt", "content": "hi\n"}),
                        Action(tool="bash", args={"command": "ls"}),
                        Action(tool="finish"),
                    ]
                }
            ),
        )
        res = agent.run()
        assert res.status == "submitted"
        assert "made.txt" in res.patch
        # the bash observation is in the transcript
        assert any("made.txt" in m["content"] for m in res.messages)
    finally:
        env.cleanup()


def test_docker_env_cleanup_removes_container(test_image):
    from cooperagents.env.docker import DockerEnv

    env = DockerEnv(test_image)
    name = env.name
    env.cleanup()
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"name={name}", "-q"], capture_output=True, text=True)
    assert out.stdout.strip() == ""
