"""Artifacts: the unit of an agent's contribution, decoupled from git.

The harness historically moved a single currency — a unified-diff string from
:meth:`Environment.git_diff` — as every agent's contribution.  That hard-wires
the whole pipeline to a git/code substrate.  An :class:`Artifact` generalises
the contribution so the same harness can run code benchmarks (a diff) OR
non-code benchmarks (a text answer / final state / action trajectory) selected
by the environment backend.

:class:`DiffArtifact` preserves the exact git behaviour (byte-identical): a
git-backed :meth:`Environment.contribution` returns ``DiffArtifact(git_diff())``
by default, so CooperBench / ProgramBench runs are unchanged.
:class:`StateArtifact` carries a task-native deliverable collected off the bus
or sandbox state instead of a patch.
"""

from __future__ import annotations

from dataclasses import dataclass

# Verbatim nudge the agent loop has always shown when a finish is vetoed for an
# empty diff. Kept identical so the git path stays byte-for-byte unchanged.
_EMPTY_DIFF_HINT = (
    "Your git diff is EMPTY — you have not written any code yet. Implement the "
    "feature now with write_file/bash edits (do NOT create or edit test files), "
    "then finish."
)


@dataclass
class Artifact:
    """One agent's contribution. Subclasses define the concrete payload."""

    def is_empty(self) -> bool:
        raise NotImplementedError

    def as_patch(self) -> str:
        """The unified diff this contribution submits, or ``""`` when it is not
        a diff (non-code benchmarks contribute no patch)."""
        return ""

    def text(self) -> str:
        """The graded text of the deliverable (a diff, an answer, a trace)."""
        return ""

    @property
    def empty_hint(self) -> str:
        """Nudge shown when a finish is vetoed for an empty contribution."""
        return "You have not produced any output yet. Produce your result, then finish."


@dataclass
class DiffArtifact(Artifact):
    """A code contribution: a git unified diff (the legacy currency)."""

    diff: str = ""

    def is_empty(self) -> bool:
        return not self.diff.strip()

    def as_patch(self) -> str:
        return self.diff

    def text(self) -> str:
        return self.diff

    @property
    def empty_hint(self) -> str:
        return _EMPTY_DIFF_HINT


@dataclass
class StateArtifact(Artifact):
    """A non-code contribution collected via messaging / sandbox state.

    Holds whichever task-native deliverable a benchmark grades: a short
    ``answer`` string (BrowseComp / Finance), a serialized final ``state``
    (WorkBench DB snapshot), and/or an action ``trajectory`` (PlanCraft).
    """

    answer: str = ""
    state: str = ""
    trajectory: str = ""

    def is_empty(self) -> bool:
        return not (self.answer.strip() or self.state.strip() or self.trajectory.strip())

    def text(self) -> str:
        return self.answer or self.state or self.trajectory

    @property
    def empty_hint(self) -> str:
        return "You have not submitted an answer yet. Produce your result, then finish."


__all__ = ["Artifact", "DiffArtifact", "StateArtifact"]
