import os
import runpy
import subprocess
from pathlib import Path


def test_scripted_repair_resolves_committed_conflict_markers(tmp_path):
    command = runpy.run_path(str(Path(__file__).parents[1] / "scripts/probe_official_repair.py"))["COMMAND"]
    env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.fsync", GIT_CONFIG_VALUE_0="none")

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
        )

    git("init")
    source = tmp_path / "example.py"
    source.write_text("<<<<<<< ours\nvalue = 1\n=======\nvalue = 2\n>>>>>>> theirs\n")
    git("add", "example.py")
    git("commit", "-qm", "captured conflicts")
    result = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_text() == "value = 1\n"
    assert "CONFLICTS_RESOLVED 1" in result.stdout
