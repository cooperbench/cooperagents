"""Artifact abstraction + git-optional Environment contribution."""

from __future__ import annotations

from cooperagents.env.artifact import Artifact, DiffArtifact, StateArtifact
from cooperagents.env.base import Environment, ExecResult
from cooperagents.env.local import LocalEnv


def test_diff_artifact_empty_and_payload():
    assert DiffArtifact("").is_empty()
    assert DiffArtifact("   \n ").is_empty()
    a = DiffArtifact("diff --git a b\n+x\n")
    assert not a.is_empty()
    assert a.as_patch() == a.text() == "diff --git a b\n+x\n"
    assert "git diff" in a.empty_hint


def test_state_artifact_semantics():
    assert StateArtifact().is_empty()
    assert not StateArtifact(answer="42").is_empty()
    assert not StateArtifact(state="{...}").is_empty()
    assert not StateArtifact(trajectory="Move(...)").is_empty()
    # A state contribution submits no patch.
    assert StateArtifact(answer="42").as_patch() == ""
    # text() prefers answer, then state, then trajectory.
    assert StateArtifact(answer="a", state="s").text() == "a"
    assert StateArtifact(state="s", trajectory="t").text() == "s"


def test_git_env_contribution_is_diff_artifact():
    """The default contribution wraps git_diff() byte-for-byte."""
    env = LocalEnv.fresh()
    try:
        env.write_file("f.txt", "hello\n")
        contribution = env.contribution()
        assert isinstance(contribution, DiffArtifact)
        assert contribution.as_patch() == env.git_diff()
        assert not contribution.is_empty()
    finally:
        env.cleanup()


def test_non_git_env_can_override_with_state_artifact():
    class _StateEnv(Environment):
        repo_path = "/nowhere"

        def __init__(self) -> None:
            self.answer = ""

        def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
            return ExecResult(stdout="", exit_code=0)

        def read_file(self, path: str) -> str:
            return ""

        def write_file(self, path: str, content: str) -> None:
            pass

        def contribution(self) -> Artifact:
            return StateArtifact(answer=self.answer)

    env = _StateEnv()
    assert env.contribution().is_empty()
    assert env.git_diff() == ""  # non-git env: no diff, no crash
    env.answer = "the capital is Paris"
    contribution = env.contribution()
    assert isinstance(contribution, StateArtifact)
    assert not contribution.is_empty()
    assert contribution.as_patch() == ""
    assert contribution.text() == "the capital is Paris"
