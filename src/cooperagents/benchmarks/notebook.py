"""Shared-notebook substrate for cooperative state benchmarks.

Replaces oracle best-of-N selection with a single, jointly-authored solution.
Every agent on one task instance shares one :class:`SharedNotebook`: a free-text
scratchpad to pool findings (notebook 1) and a solution slot whose final content
IS the graded deliverable (notebook 2), executed or judged by the benchmark's
native evaluator.

There is no selection over N candidate episodes, so there is no oracle: whatever
the team leaves in the solution slot is scored, whether that is better or worse
than one agent acting alone. The team arm therefore measures the real impact of
the cooperation setup, positive or negative.
"""

from __future__ import annotations

import threading
from collections import defaultdict

from cooperagents.env.artifact import Artifact


class SharedNotebook:
    """Thread-safe blackboard shared by all agents on one instance.

    ``scratch`` is an append-only coordination log; ``solution`` is the single
    graded deliverable (last write wins — convergence is the team's job, not an
    oracle's). ``edits`` records per-agent write counts so a non-participating
    teammate is visible in the data rather than hidden.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.scratch: list[str] = []
        self.solution: str = ""
        self.edits: dict[str, dict[str, int]] = defaultdict(lambda: {"scratch": 0, "solution": 0})

    def write_scratch(self, agent_id: str, text: str) -> None:
        with self._lock:
            self.scratch.append(f"[{agent_id}] {text}")
            self.edits[agent_id]["scratch"] += 1

    def read_scratch(self) -> str:
        with self._lock:
            return "\n".join(self.scratch)

    def write_solution(self, agent_id: str, text: str) -> None:
        with self._lock:
            self.solution = text
            self.edits[agent_id]["solution"] += 1

    def read_solution(self) -> str:
        with self._lock:
            return self.solution

    def participation(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self.edits.items()}


class NotebookArtifact(Artifact):
    """Contribution whose graded text is the shared solution slot, read live.

    All agents on an instance harvest a NotebookArtifact over the SAME notebook,
    so the integrated contribution is the team's final agreed solution no matter
    which agent's artifact the (trivial) reducer returns, and no matter the order
    agents finished in — the solution is read at scoring time.

    ``trajectory`` is this agent's OWN action log (gym experiments, note_write,
    solution_commit), so per-agent capture records what each agent actually did
    and whether it wrote to the notebooks at all.
    """

    def __init__(self, notebook: SharedNotebook, trajectory: str = "") -> None:
        self.notebook = notebook
        self._trajectory = trajectory

    @property
    def trajectory(self) -> str:
        return self._trajectory

    def is_empty(self) -> bool:
        return not self.notebook.read_solution().strip()

    def text(self) -> str:
        return self.notebook.read_solution()

    @property
    def empty_hint(self) -> str:
        return (
            "The shared solution notebook is empty. Converge on a solution with your "
            "teammates and write it with solution_commit before finishing."
        )


__all__ = ["SharedNotebook", "NotebookArtifact"]
