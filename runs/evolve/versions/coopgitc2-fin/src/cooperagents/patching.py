"""Patch post-processing shared by the harness and the eval writer."""

from __future__ import annotations

import re

# Files the grader supplies itself.  An agent-authored copy collides with the
# grader's test patch ("already exists") and frequently won't compile, so these
# sections are stripped from any submitted diff — across all settings, so solo
# and team are treated identically.
_TEST_PATH = re.compile(r"(_test\.[A-Za-z0-9]+$)|((^|/)test_[^/]*$)|((^|/)tests?/)|((^|/)conftest\.py$)")


# Scratch dir agents use to publish per-feature regression checks to teammates
# (the "invariant" coordination protocol). These transfer between agents via the
# seeded tree but must NEVER reach the grader, so they're stripped at submission.
_SCRATCH = re.compile(r"(^|/)\.cb_checks/")


def _strip(patch: str, pat: re.Pattern[str]) -> str:
    if not patch:
        return patch
    out: list[str] = []
    skip = False
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)", line.rstrip("\n"))
            path = m.group(2) if m else ""
            skip = bool(pat.search(path))
        if not skip:
            out.append(line)
    return "".join(out)


def strip_test_sections(patch: str) -> str:
    """Drop whole-file sections that touch test files from a unified diff."""
    return _strip(patch, _TEST_PATH)


def strip_for_submission(patch: str) -> str:
    """Final cleanup at the eval boundary: drop test files AND scratch checks
    (.cb_checks/), which agents share between themselves but the grader must not see."""
    return _strip(_strip(patch, _TEST_PATH), _SCRATCH)


__all__ = ["strip_test_sections", "strip_for_submission"]
