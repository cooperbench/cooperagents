"""Test-file stripping from submitted diffs."""

from __future__ import annotations

from cooperagents.patching import strip_test_sections

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
