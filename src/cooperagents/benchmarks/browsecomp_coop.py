"""BrowseComp-Plus under the cooperative-notebook protocol (auto-capture).

NOTE (non-principled experiment): this shared-notebook variant uses a single
mutable "solution" slot (last-write-wins), departing from cooperagents' isolated
execution + reconcile principle. The principled path is the plain ``browsecomp``
benchmark run as a cooperagents team (own search session per agent, bus
coordination, reducer). Kept for comparison only.

Agents share one :class:`SharedNotebook` per query: a scratchpad to pool search
findings, and a solution slot that holds the team's answer. There is no commit
ritual — ``submit_answer`` writes directly to the shared solution (auto-capture),
because submitting the answer is the task's native deliverable action. The single
shared answer is graded by the same injected judge (no oracle, no best-of-N).

Solo (team_size 1) and team use the identical protocol; solo is simply the sole
author of the shared answer.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from cooperagents.benchmarks.browsecomp import (
    BM25Retriever,
    BrowseCompBenchmark,
    Judge,
    LLMJudge,
    Retriever,
    _UnconfiguredJudge,
    _UnconfiguredRetriever,
    load_browsecomp,
)
from cooperagents.benchmarks.notebook import NotebookArtifact, SharedNotebook
from cooperagents.env.artifact import Artifact
from cooperagents.env.base import Environment
from cooperagents.env.state import StateEnv
from cooperagents.llm import Action, LLMClient
from cooperagents.reducers import Reducer, best_of_first_nonempty
from cooperagents.tools import ToolSet


class BrowseCompCoopToolSet(ToolSet):
    """Per-agent search/open plus shared-notebook coordination and an
    auto-capturing submit_answer that writes the team's shared answer."""

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    def specs(self) -> list[dict[str, str]]:
        return [
            {"name": "search", "description": "Search the corpus. args: {query, k?}"},
            {"name": "open", "description": "Read a document's full text. args: {doc_id}"},
            {"name": "note_write", "description": "Append a finding to the SHARED scratchpad teammates can read. args: {text}"},
            {"name": "note_read", "description": "Read the SHARED scratchpad of teammate findings. args: {}"},
            {"name": "solution_read", "description": "Read the team's current shared answer. args: {}"},
            {"name": "submit_answer", "description": "Write/replace the team's shared ANSWER (graded; last write wins). args: {answer}"},
        ]

    def dispatch(self, env: Environment, action: Action) -> tuple[str, bool] | None:
        assert isinstance(env, StateEnv)
        aid = str(getattr(env, "agent_id", "?"))
        nb: SharedNotebook = env.sandbox["notebook"]
        if action.tool == "search":
            k = int(action.args.get("k", 5) or 5)
            query = str(action.args.get("query", ""))
            hits = self._retriever.search(query, k)
            env.store.setdefault("visited", [])
            for doc_id, _snippet in hits:
                if doc_id not in env.store["visited"]:
                    env.store["visited"].append(doc_id)
            env.record(f"search({query!r}, k={k}) -> {[d for d, _ in hits]}")
            return "\n".join(f"[{d}] {s}" for d, s in hits) or "(no results)", False
        if action.tool == "open":
            doc_id = str(action.args.get("doc_id", ""))
            env.store.setdefault("visited", [])
            if doc_id not in env.store["visited"]:
                env.store["visited"].append(doc_id)
            text = self._retriever.open(doc_id) or "(unknown document)"
            env.record(f"open({doc_id!r}) -> {len(text)} chars")
            return text, False
        if action.tool == "note_write":
            text = str(action.args.get("text", ""))
            nb.write_scratch(aid, text)
            env.record(f"note_write: {text[:120]}")
            return "noted to shared scratchpad", False
        if action.tool == "note_read":
            return nb.read_scratch() or "(scratchpad empty)", False
        if action.tool == "solution_read":
            cur = nb.read_solution()
            return (f"current team answer: {cur}" if cur else "(no answer yet)"), False
        if action.tool == "submit_answer":
            answer = str(action.args.get("answer", ""))
            nb.write_solution(aid, answer)  # auto-capture: submitting IS the shared deliverable
            env.record(f"submit_answer -> {answer[:200]!r}")
            return "team answer recorded", True
        return None


class BrowseCompCoopBenchmark(BrowseCompBenchmark):
    """BrowseComp with a shared notebook and one auto-captured team answer."""

    name = "browsecomp-coop"

    def __init__(
        self,
        *,
        retriever: Retriever | None = None,
        judge: Judge | None = None,
        instances_data: Any = None,
    ) -> None:
        super().__init__(retriever=retriever, judge=judge, instances_data=instances_data)
        self._nbs: dict[int, SharedNotebook] = {}
        self._nb_lock = threading.Lock()

    def _notebook(self, instance: Any) -> SharedNotebook:
        with self._nb_lock:
            return self._nbs.setdefault(id(instance), SharedNotebook())

    def task_prompt(self, instance: Any) -> str:
        return (
            "You are on a TEAM answering a research question together. Use search and open to find "
            "evidence in the corpus. Pool what you find with your teammates via note_write, and read "
            "their findings with note_read. The team's ANSWER is a single shared value: writing it "
            "with submit_answer replaces the current team answer (last write wins), and that shared "
            "answer is what gets graded — so coordinate and converge rather than overwrite a better "
            "answer. Read the current team answer any time with solution_read.\n\n"
            "Question: " + instance.query
        )

    def make_env(self, instance: Any) -> StateEnv:
        nb = self._notebook(instance)

        def harvest(_e: StateEnv) -> Artifact:
            return NotebookArtifact(nb, trajectory="\n".join(_e.log))

        env = StateEnv(harvest=harvest)
        env.sandbox = {"notebook": nb}
        return env

    def toolset(self) -> ToolSet:
        return BrowseCompCoopToolSet(self._retriever)

    def team_reducer(self, llm: LLMClient) -> Reducer:
        # One shared answer; every candidate artifact resolves to it. No oracle.
        return best_of_first_nonempty


def browsecomp_coop_from_env() -> BrowseCompCoopBenchmark:
    """Build a cooperative BrowseComp benchmark from the same environment config
    as :func:`~cooperagents.benchmarks.browsecomp.browsecomp_from_env`."""
    corpus = os.getenv("BROWSECOMP_CORPUS")
    queries = os.getenv("BROWSECOMP_QUERIES")
    judge_model = os.getenv("BROWSECOMP_JUDGE_MODEL")
    retriever = BM25Retriever.from_jsonl(corpus) if corpus and os.path.exists(corpus) else _UnconfiguredRetriever()
    judge = LLMJudge(judge_model) if judge_model else _UnconfiguredJudge()
    data = load_browsecomp(queries) if queries and os.path.exists(queries) else None
    return BrowseCompCoopBenchmark(retriever=retriever, judge=judge, instances_data=data)


__all__ = ["BrowseCompCoopBenchmark", "BrowseCompCoopToolSet", "browsecomp_coop_from_env"]
