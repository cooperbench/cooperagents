"""Test-file stripping from submitted diffs."""

from __future__ import annotations

import shlex

import pytest

from cooperagents.patching import strip_for_submission, strip_test_sections

PATCH = """diff --git a/metrics.go b/metrics.go
new file mode 100644
--- /dev/null
+++ b/metrics.go
@@ -0,0 +1 @@
+package chi
diff --git a/path_value_test.go b/path_value_test.go
new file mode 100644
--- /dev/null
+++ b/path_value_test.go
@@ -0,0 +1 @@
+package chi // test
diff --git a/mux.go b/mux.go
--- a/mux.go
+++ b/mux.go
@@ -1 +1,2 @@
 package chi
+// edit
"""


def test_strips_go_test_file():
    out = strip_test_sections(PATCH)
    assert "metrics.go" in out
    assert "mux.go" in out
    assert "path_value_test.go" not in out
    assert "// test" not in out


def test_strips_python_test_paths():
    p = (
        "diff --git a/src/foo.py b/src/foo.py\n+x\n"
        "diff --git a/tests/test_foo.py b/tests/test_foo.py\n+y\n"
        "diff --git a/test_bar.py b/test_bar.py\n+z\n"
        "diff --git a/conftest.py b/conftest.py\n+c\n"
    )
    out = strip_test_sections(p)
    assert "src/foo.py" in out
    assert "tests/test_foo.py" not in out
    assert "test_bar.py" not in out
    assert "conftest.py" not in out


def test_empty_patch():
    assert strip_test_sections("") == ""


def test_strips_root_submission_artifact_from_merge_and_final_patch():
    artifact = "diff --git a/patch.txt b/patch.txt\n+worker output\n"
    implementation = "diff --git a/data/patch.txt b/data/patch.txt\n+implementation data\n"
    patch = artifact + implementation
    assert strip_test_sections(patch) == implementation
    assert strip_for_submission(patch) == implementation


@pytest.mark.parametrize("source_conflict", [False, True])
def test_presub_merge_keeps_own_submission_artifact(tmp_path, source_conflict):
    from cooperagents.env.local import LocalEnv
    from cooperagents.verification import _MERGE

    env = LocalEnv.fresh(workdir=str(tmp_path))

    def git(command):
        result = env.execute(command)
        assert result.exit_code == 0, result.stdout
        return result.stdout

    try:
        bare = shlex.quote(str(tmp_path / "share.git"))
        git(f"git init -q --bare {bare} && git remote add shared {bare}")
        git("git switch -qc teammate && printf theirs > patch.txt && printf teammate > feature2.txt")
        git("git add . && git commit -qm teammate && git push -q shared HEAD:refs/heads/agent2")
        git("git switch -qc worker HEAD^ && printf ours > patch.txt && printf worker > feature1.txt")
        if source_conflict:
            git("printf conflicting > feature2.txt")
        agent_file = tmp_path / "agent_id"
        agent_file.write_text("agent1\n")
        merge = _MERGE.replace("/tmp/.agent_id", shlex.quote(str(agent_file))).replace("/tmp/.mg", shlex.quote(str(tmp_path / "merge.log")))
        result = env.execute(merge)
        assert env.read_file("patch.txt") == "ours"
        unmerged = git("git diff --name-only --diff-filter=U").splitlines()
        if source_conflict:
            assert "TEAMMATE_MERGE_CONFLICT" in result.stdout
            assert unmerged == ["feature2.txt"]
        else:
            assert "MERGED_OK" in result.stdout
            assert not unmerged
            assert env.read_file("feature2.txt") == "teammate"
    finally:
        env.cleanup()
