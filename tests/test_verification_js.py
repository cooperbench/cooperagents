import shlex
import shutil
from pathlib import Path

import pytest

from cooperagents import verification
from cooperagents.env.local import LocalEnv

HAS_NODE = shutil.which("node") is not None


@pytest.fixture
def repo(tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        yield env
    finally:
        env.cleanup()


def test_default_discovery_script_is_unchanged():
    original = (
        'if [ -f ./compile.sh ]; then echo "bash ./compile.sh"; '
        'elif { [ -f Makefile ] || [ -f makefile ]; } && command -v make >/dev/null 2>&1; then echo "make"; '
        'elif [ -f Cargo.toml ]; then echo "cargo build --release --offline"; '
        'elif [ -f go.mod ] || ls ./*.go >/dev/null 2>&1; then echo "go build ./..."; '
        'elif [ -f pyproject.toml ] || [ -f setup.py ]; then echo "python3 -m compileall -q ."; '
        "fi"
    )
    assert verification._DISCOVER == verification.discovery_script(False) == original
    enabled = verification.discovery_script(True)
    assert enabled.index("go.mod") < enabled.index("package.json") < enabled.index("pyproject.toml")


@pytest.mark.skipif(not HAS_NODE, reason="Node is required for JS discovery")
def test_js_discovery_is_opt_in_and_precedes_python(repo):
    assert "package.json" in verification._build(repo, None, js_build=True)
    repo.write_file("package.json", "{}")
    assert verification.discover_build(repo) == ""
    assert "package.json" not in verification._build(repo, None)
    js_command = verification.discover_build(repo, js_build=True)
    assert "node --check" in js_command
    assert "node_modules/.bin/tsc --noEmit -p ." in js_command

    repo.write_file("pyproject.toml", "")
    assert verification.discover_build(repo) == "python3 -m compileall -q ."
    assert verification.discover_build(repo, js_build=True) == js_command


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("compile.sh", "bash ./compile.sh"),
        ("Makefile", "make"),
        ("Cargo.toml", "cargo build --release --offline"),
        ("go.mod", "go build ./..."),
        ("main.go", "go build ./..."),
    ],
)
def test_higher_priority_builds_win_in_both_modes(repo, filename, expected):
    if filename == "Makefile" and shutil.which("make") is None:
        pytest.skip("Make is required to discover a Makefile")
    repo.write_file(filename, "")
    repo.write_file("package.json", "{}")
    for enabled in (False, True):
        assert verification.discover_build(repo, js_build=enabled) == expected


class RecordingEnv:
    """Keep container-local files isolated when executing against a local repo."""

    def __init__(self, repo, tmp_path):
        self.repo = repo
        self.commands = []
        self.rc = tmp_path / ".v_js_base_rc"
        self.log = tmp_path / "v_build.log"

    def execute(self, command, *, timeout=60):
        self.commands.append((command, timeout))
        if not command.startswith(verification._DISCOVER_START):
            command = command.replace("/tmp/.v_js_base_rc", shlex.quote(str(self.rc)))
            command = command.replace("/tmp/v_build.log", shlex.quote(str(self.log)))
        return self.repo.execute(command, timeout=timeout)


@pytest.mark.skipif(not HAS_NODE, reason="Node is required to run the JS syntax check")
def test_checks_every_tracked_source_and_ignores_vendored_and_untracked_files(repo, tmp_path):
    env = RecordingEnv(repo, tmp_path)
    repo.write_file("package.json", '{"scripts":{"check":"touch ran-package-script"}}')
    repo.write_file("a.js", "const a = 1;")
    repo.write_file("z ' second file.mjs", "export const ;")
    repo.write_file("node_modules/vendor.cjs", "const ;")
    repo.write_file("dist/bundle.js", "const ;")
    assert repo.execute("git add .").exit_code == 0
    repo.write_file("untracked.js", "const ;")

    error = verification._build(env, None, js_build=True)
    assert error.startswith("Build failed")
    assert "second file.mjs" in error
    repo.write_file("z ' second file.mjs", "export const b = 2;")
    assert verification._build(env, None, js_build=True) is None
    assert not (Path(repo.repo_path) / "ran-package-script").exists()


@pytest.mark.skipif(not HAS_NODE, reason="Node is required to select the JS build")
@pytest.mark.parametrize("base_exit_code", [0, 2])
def test_typecheck_baseline_is_cached_and_failures_fall_back_to_syntax(repo, tmp_path, base_exit_code):
    env = RecordingEnv(repo, tmp_path)
    repo.write_file("package.json", "{}")
    repo.write_file("tsconfig.json", "{}")
    repo.write_file("valid.cjs", "const value = 1;")
    repo.write_file("node_modules/.bin/tsc", '#!/bin/sh\nprintf "%s\\n" "$*" >> typecheck.log\nexit "$(cat typecheck-rc)"\n')
    repo.write_file("typecheck-rc", str(base_exit_code))
    assert repo.execute("chmod +x node_modules/.bin/tsc && git add valid.cjs").exit_code == 0
    verification.prepare_js_build(env)
    verification.prepare_js_build(env)
    assert repo.read_file("typecheck.log") == "--noEmit -p .\n"
    assert any("printf 1 >/tmp/.v_js_base_rc" in command and timeout == 600 for command, timeout in env.commands)

    repo.write_file("typecheck-rc", "2")
    error = verification.validate(env, js_build=True)
    if base_exit_code == 0:
        assert error.startswith("Build failed")
        assert repo.read_file("typecheck.log") == "--noEmit -p .\n" * 2
    else:
        assert error is None
        assert repo.read_file("typecheck.log") == "--noEmit -p .\n"
        repo.write_file("valid.cjs", "const ;")
        assert verification.validate(env, js_build=True).startswith("Build failed")
