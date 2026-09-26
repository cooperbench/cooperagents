"""BrowseComp-Plus benchmark — STUB (not yet runnable).

BrowseComp-Plus is a deep-research benchmark: a single query answered by an
agentic search loop over a FROZEN ~100K-document retriever corpus, graded by an
LLM judge (Qwen3-32B) plus IR recall/nDCG over the visited documents.

Wiring it needs live infrastructure that isn't set up here, so this adapter is
a documented placeholder that raises with instructions. To complete it (see
docs/SCALING_BENCHMARKS.md):

1. Stand up the frozen retriever + prebuilt index (BM25 or Qwen3-Embedding-8B)
   from github.com/texttron/BrowseComp-Plus as a local service.
2. Implement a ``ToolSet`` exposing ``search(query)`` / ``open(doc_id)`` over
   that service and ``submit_answer(answer)`` writing to ``StateEnv.answer``.
3. Implement a ``Scorer`` that calls the official Qwen3-32B judge endpoint on
   the final answer (and optionally recall/nDCG over visited doc ids).
4. ``make_env`` returns a read-only ``StateEnv`` whose harvest is the answer +
   visited-doc set; ``instances`` loads the 830 obfuscated queries.

Topology fit: search fan-out (Independent/Centralized) + a synthesis reducer.
"""

from __future__ import annotations

from typing import Any

from cooperagents.benchmarks.base import StateBenchmark
from cooperagents.env.state import StateEnv
from cooperagents.eval.scoring import Scorer
from cooperagents.tools import ToolSet

_NEED = (
    "BrowseComp-Plus is not wired yet: it needs a served frozen retriever index and a "
    "Qwen3-32B judge endpoint. See docs/SCALING_BENCHMARKS.md ('Completing BrowseComp-Plus')."
)


class BrowseCompBenchmark(StateBenchmark):
    name = "browsecomp"

    def instances(self, split: str = "test") -> list[Any]:
        raise NotImplementedError(_NEED)

    def task_prompt(self, instance: Any) -> str:
        raise NotImplementedError(_NEED)

    def make_env(self, instance: Any) -> StateEnv:
        raise NotImplementedError(_NEED)

    def toolset(self) -> ToolSet:
        raise NotImplementedError(_NEED)

    def scorer(self) -> Scorer:
        raise NotImplementedError(_NEED)


__all__ = ["BrowseCompBenchmark"]
