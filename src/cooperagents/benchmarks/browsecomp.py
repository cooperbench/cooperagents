"""BrowseComp-Plus benchmark on the state substrate.

BrowseComp-Plus is deep research: a query answered by an agentic search loop
over a frozen retriever corpus, graded by an LLM judge. Two external services
are needed — a **retriever** (search + open over the corpus) and a **judge**
(answer vs gold). Both are injected behind small protocols, so:

  * with FAKE implementations (``InMemoryRetriever`` + ``SubstringJudge``) the
    whole adapter runs offline with NO api keys — this is the smoke-test path;
  * with REAL implementations (a served index + an LLM judge endpoint) it runs
    the actual benchmark.

The cooperagents plumbing (search/open/submit_answer tools, answer + visited-doc
harvest, judge-based scorer) is identical either way; only the two injected
services change. See docs/SCALING_BENCHMARKS.md for wiring the real services.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cooperagents.benchmarks.base import StateBenchmark
from cooperagents.env.artifact import Artifact, StateArtifact
from cooperagents.env.base import Environment
from cooperagents.env.state import StateEnv
from cooperagents.eval.scoring import Scorer, TaskScore
from cooperagents.llm import Action
from cooperagents.tools import ToolSet


class Retriever(Protocol):
    """Search + open over the frozen corpus."""

    def search(self, query: str, k: int = 5) -> list[tuple[str, str]]:
        """Return up to ``k`` (doc_id, snippet) pairs for ``query``."""

    def open(self, doc_id: str) -> str:
        """Return the full text of ``doc_id`` (empty string if unknown)."""


class Judge(Protocol):
    """Grade a final answer against the gold answer."""

    def judge(self, question: str, answer: str, gold: str) -> bool: ...


@dataclass
class BrowseCompInstance:
    instance_id: str
    query: str
    answer: str
    gold_docs: list[str] = field(default_factory=list)


# --- fake services (offline smoke-test path; no api keys) --------------------


class InMemoryRetriever:
    """Keyword-overlap search over an in-memory ``{doc_id: text}`` corpus."""

    def __init__(self, docs: dict[str, str]) -> None:
        self.docs = docs

    def search(self, query: str, k: int = 5) -> list[tuple[str, str]]:
        terms = set(query.lower().split())
        scored = []
        for doc_id, text in self.docs.items():
            overlap = len(terms & set(text.lower().split()))
            if overlap:
                scored.append((overlap, doc_id, text[:160]))
        scored.sort(reverse=True)
        return [(doc_id, snippet) for _score, doc_id, snippet in scored[:k]]

    def open(self, doc_id: str) -> str:
        return self.docs.get(doc_id, "")


class SubstringJudge:
    """Deterministic fake judge: gold answer appears in the response (normalized)."""

    def judge(self, question: str, answer: str, gold: str) -> bool:
        norm = " ".join(answer.lower().split())
        return bool(gold.strip()) and " ".join(gold.lower().split()) in norm


# --- real service shells (require infra; see docs) ---------------------------

_NEED_RETRIEVER = (
    "BrowseComp real retriever not configured: stand up the frozen index (BM25 or "
    "Qwen3-Embedding-8B) from github.com/texttron/BrowseComp-Plus and inject a Retriever. "
    "See docs/SCALING_BENCHMARKS.md."
)
_NEED_JUDGE = "BrowseComp real judge not configured: inject a Judge backed by the Qwen3-32B endpoint. See docs/SCALING_BENCHMARKS.md."


class _UnconfiguredRetriever:
    def search(self, query: str, k: int = 5) -> list[tuple[str, str]]:
        raise NotImplementedError(_NEED_RETRIEVER)

    def open(self, doc_id: str) -> str:
        raise NotImplementedError(_NEED_RETRIEVER)


class _UnconfiguredJudge:
    def judge(self, question: str, answer: str, gold: str) -> bool:
        raise NotImplementedError(_NEED_JUDGE)


# --- adapter -----------------------------------------------------------------


def _harvest(env: StateEnv) -> Artifact:
    visited = env.store.get("visited", [])
    return StateArtifact(answer=env.answer, trajectory="visited: " + ", ".join(visited))


class BrowseCompToolSet(ToolSet):
    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    def specs(self) -> list[dict[str, str]]:
        return [
            {"name": "search", "description": "Search the corpus. args: {query, k?}"},
            {"name": "open", "description": "Read a document's full text. args: {doc_id}"},
            {"name": "submit_answer", "description": "Submit the final short answer. args: {answer}"},
        ]

    def dispatch(self, env: Environment, action: Action) -> tuple[str, bool] | None:
        assert isinstance(env, StateEnv)
        if action.tool == "search":
            k = int(action.args.get("k", 5) or 5)
            hits = self._retriever.search(str(action.args.get("query", "")), k)
            env.store.setdefault("visited", [])
            for doc_id, _snippet in hits:
                if doc_id not in env.store["visited"]:
                    env.store["visited"].append(doc_id)
            return "\n".join(f"[{d}] {s}" for d, s in hits) or "(no results)", False
        if action.tool == "open":
            doc_id = str(action.args.get("doc_id", ""))
            env.store.setdefault("visited", [])
            if doc_id not in env.store["visited"]:
                env.store["visited"].append(doc_id)
            return self._retriever.open(doc_id) or "(unknown document)", False
        if action.tool == "submit_answer":
            env.answer = str(action.args.get("answer", ""))
            return "answer recorded", True
        return None


class BrowseCompScorer(Scorer):
    name = "browsecomp"

    def __init__(self, judge: Judge) -> None:
        self._judge = judge

    def score(self, instance: Any, submission: str) -> TaskScore:
        ok = self._judge.judge(getattr(instance, "query", ""), submission, getattr(instance, "answer", ""))
        return TaskScore(instance_id=str(getattr(instance, "instance_id", "")), success=1.0 if ok else 0.0)


class BrowseCompBenchmark(StateBenchmark):
    name = "browsecomp"

    def __init__(
        self,
        *,
        retriever: Retriever | None = None,
        judge: Judge | None = None,
        instances_data: list[BrowseCompInstance] | None = None,
    ) -> None:
        self._retriever: Retriever = retriever or _UnconfiguredRetriever()
        self._judge: Judge = judge or _UnconfiguredJudge()
        self._data = instances_data

    def instances(self, split: str = "test") -> list[BrowseCompInstance]:
        if self._data is not None:
            return list(self._data)
        raise NotImplementedError(
            "BrowseComp real dataset not loaded: pass instances_data (fakes) or wire the "
            "830-query loader from github.com/texttron/BrowseComp-Plus. See docs/SCALING_BENCHMARKS.md."
        )

    def task_prompt(self, instance: Any) -> str:
        return (
            "Answer the question by searching the corpus (search), reading documents (open), "
            "then submitting a short factual answer (submit_answer).\n\nQuestion: " + instance.query
        )

    def make_env(self, instance: Any) -> StateEnv:
        return StateEnv(harvest=_harvest)

    def toolset(self) -> ToolSet:
        return BrowseCompToolSet(self._retriever)

    def scorer(self) -> Scorer:
        return BrowseCompScorer(self._judge)


# --- real services (require a corpus + a judge model; testable via seams) ----


class BM25Retriever:
    """Real BM25 ranking over an in-memory ``{doc_id: text}`` corpus.

    Pure-Python (no external index dependency), so it runs the actual retrieval
    algorithm over a local corpus — testable over a fixture, and usable for real
    runs by loading the BrowseComp-Plus corpus via :meth:`from_jsonl`.
    """

    def __init__(self, corpus: dict[str, str], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.docs = corpus
        self.ids = list(corpus)
        self._tok = {i: corpus[i].lower().split() for i in self.ids}
        self._dl = {i: len(self._tok[i]) for i in self.ids}
        self._avgdl = (sum(self._dl.values()) / len(self.ids)) if self.ids else 0.0
        df: dict[str, int] = {}
        for i in self.ids:
            for t in set(self._tok[i]):
                df[t] = df.get(t, 0) + 1
        n = len(self.ids)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.k1, self.b = k1, b

    def _score(self, q_terms: list[str], doc_id: str) -> float:
        if not self._avgdl:
            return 0.0
        tf: dict[str, int] = {}
        for t in self._tok[doc_id]:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in q_terms:
            idf = self._idf.get(t)
            if idf is None:
                continue
            f = tf.get(t, 0)
            s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * self._dl[doc_id] / self._avgdl))
        return s

    def search(self, query: str, k: int = 5) -> list[tuple[str, str]]:
        q_terms = query.lower().split()
        scored = sorted(((self._score(q_terms, i), i) for i in self.ids), reverse=True)
        return [(i, self.docs[i][:160]) for s, i in scored[:k] if s > 0]

    def open(self, doc_id: str) -> str:
        return self.docs.get(doc_id, "")

    @classmethod
    def from_jsonl(cls, path: str | Path, *, id_field: str = "docid", text_field: str = "text") -> BM25Retriever:
        corpus: dict[str, str] = {}
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            corpus[str(row[id_field])] = str(row.get(text_field, ""))
        return cls(corpus)


class LLMJudge:
    """LLM judge (answer vs gold). Uses litellm by default; a ``complete`` seam
    (prompt -> reply) is injectable for offline tests."""

    def __init__(self, model: str, *, complete: Callable[[str], str] | None = None) -> None:
        self.model = model
        self._complete = complete

    def _default_complete(self, prompt: str) -> str:
        import litellm

        resp = litellm.completion(model=self.model, messages=[{"role": "user", "content": prompt}], temperature=0.0)
        return resp["choices"][0]["message"]["content"] or ""

    def judge(self, question: str, answer: str, gold: str) -> bool:
        prompt = (
            f"Question: {question}\nReference answer: {gold}\nCandidate answer: {answer}\n\n"
            "Does the candidate answer match the reference answer? Reply with only YES or NO."
        )
        out = (self._complete or self._default_complete)(prompt).strip().upper()
        return out.startswith("YES")


def load_browsecomp(path: str | Path) -> list[BrowseCompInstance]:
    """Load queries from a JSONL file of {id/query_id, query, answer, gold_docs?}."""
    out: list[BrowseCompInstance] = []
    for idx, line in enumerate(Path(path).read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        out.append(
            BrowseCompInstance(
                instance_id=str(row.get("id", row.get("query_id", idx))),
                query=str(row["query"]),
                answer=str(row.get("answer", "")),
                gold_docs=list(row.get("gold_docs", [])),
            )
        )
    return out


def browsecomp_from_env() -> BrowseCompBenchmark:
    """Build a BrowseComp benchmark from environment configuration.

    Reads BROWSECOMP_CORPUS (jsonl), BROWSECOMP_QUERIES (jsonl), and
    BROWSECOMP_JUDGE_MODEL. Any missing piece leaves that service as an
    unconfigured shell (raises with guidance on use).
    """
    corpus = os.getenv("BROWSECOMP_CORPUS")
    queries = os.getenv("BROWSECOMP_QUERIES")
    judge_model = os.getenv("BROWSECOMP_JUDGE_MODEL")
    retriever = BM25Retriever.from_jsonl(corpus) if corpus else None
    judge = LLMJudge(judge_model) if judge_model else None
    data = load_browsecomp(queries) if queries else None
    return BrowseCompBenchmark(retriever=retriever, judge=judge, instances_data=data)


__all__ = [
    "BrowseCompBenchmark",
    "BrowseCompInstance",
    "BrowseCompToolSet",
    "BrowseCompScorer",
    "Retriever",
    "Judge",
    "InMemoryRetriever",
    "SubstringJudge",
    "BM25Retriever",
    "LLMJudge",
    "load_browsecomp",
    "browsecomp_from_env",
]
