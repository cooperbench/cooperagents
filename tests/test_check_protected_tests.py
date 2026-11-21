"""Run the protected-test guard against temporary Git histories."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_protected_tests.py"
GUARD = "scripts/check_protected_tests.py"
SUITE = "tests/protected/test_tool_call_coordinator_macro.py"
BASE = "refs/remotes/origin/cameron-chen/coord-baselines"


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write(path, text):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def commit():
        git("add", "--all")
        git("-c", "commit.gpgsign=false", "commit", "-qm", "test snapshot")

    def guard(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.name", "Guard test")
    git("config", "user.email", "guard@example.test")
    write(SUITE, "def test_example():\n    assert True\n")
    write(GUARD, "# baseline guard\n")
    commit()
    git("update-ref", BASE, "HEAD")
    return tmp_path, git, write, commit, guard


def test_clean_tree_and_ordinary_implementation_change_pass(repo):
    _, _, write, commit, guard = repo
    assert guard().returncode == 0
    write("src/example.py", "value = 1\n")
    commit()
    assert guard().returncode == 0


def test_unstaged_staged_and_untracked_protected_changes_fail(repo):
    _, git, write, _, guard = repo
    write(SUITE, "def test_example():\n    assert 1 == 1\n")
    write("tests/protected/tool_call/test_extra.py", "# untracked\n")
    git("add", SUITE)
    git("restore", "--source=HEAD", "--worktree", SUITE)
    write(GUARD, "# updated guard\n")
    result = guard()
    assert result.returncode == 1
    assert f"Protected change: {SUITE} (family: tool_call)" in result.stderr
    assert f"Protected change: {GUARD} (family: guard)" in result.stderr
    assert "Protected change: tests/protected/tool_call/test_extra.py" in result.stderr


@pytest.mark.parametrize("change", ["delete", "rename-out", "rename-within"])
def test_committed_deletion_and_rename_cannot_hide_protected_path(repo, change):
    _, git, _, commit, guard = repo
    if change == "delete":
        git("rm", SUITE)
    else:
        destination = "tests/test_moved.py" if change == "rename-out" else "tests/protected/test_moved.py"
        git("mv", SUITE, destination)
    commit()
    result = guard("--base", "HEAD~1")
    assert result.returncode == 1
    assert f"Protected change: {SUITE}" in result.stderr
    if change == "rename-within":
        assert f"Protected change: {destination}" in result.stderr


def test_single_family_allowance_cannot_include_implementation_or_peer_family(repo):
    _, _, write, _, guard = repo
    write(SUITE, "def test_example():\n    assert 2 == 2\n")
    assert guard("--allow-protected", "tool_call").returncode == 0

    write("tests/protected/other/test_example.py", "# different family\n")
    write("src/example.py", "value = 1\n")
    result = guard("--allow-protected", "tool_call")
    assert result.returncode == 1
    assert "family: other" in result.stderr
    assert "test-only change: src/example.py" in result.stderr
    result = guard("--allow-protected", "tool_call", "--allow-protected", "other")
    assert result.returncode == 2
    assert "permits a single test family" in result.stderr


def test_guard_allowance_covers_guard_tests_and_repository_rules(repo):
    _, _, write, _, guard = repo
    write(SUITE, "# new version\n")
    write(GUARD, "# updated guard\n")
    write("tests/test_check_protected_tests.py", "# guard coverage\n")
    write("CLAUDE.md", "Protected suite rules\n")
    assert guard("--allow-protected", "tool_call", "--allow-protected", "guard").returncode == 0
    result = guard("--allow-protected", "tool_call")
    assert result.returncode == 1
    assert f"Protected change: {GUARD}" in result.stderr


@pytest.mark.parametrize(
    ("imports", "mark"),
    [
        ("import pytest", "pytest.mark.skip"),
        ("import pytest as pt", "pt.mark.xfail"),
        ("from pytest import mark", "mark.skipif"),
        ("from pytest import mark as marks", "marks.xfail"),
    ],
)
def test_disabled_marks_fail_even_with_family_allowance(repo, imports, mark):
    _, _, write, _, guard = repo
    file = "tests/protected/tool_call/conftest.py"
    write(file, f"{imports}\n\n@{mark}\ndef test_example():\n    pass\n")
    result = guard("--allow-protected", "tool_call")
    assert result.returncode == 1
    assert f"Disabled protected test marker: {file}:3" in result.stderr


def test_marker_text_in_strings_and_comments_is_allowed(repo):
    _, _, write, _, guard = repo
    write(SUITE, '"""Example: pytest.mark.xfail."""\n# pytest.mark.skip\n')
    assert guard("--allow-protected", "tool_call").returncode == 0


def test_missing_base_fails_closed(repo):
    *_, guard = repo
    result = guard("--base", "missing-branch")
    assert result.returncode == 1
    assert "Protected test guard failed" in result.stderr
