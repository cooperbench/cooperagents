"""Finance-Agent benchmark on the state substrate.

The Finance Agent benchmark answers expert finance questions by fetching sources
via four tools and emitting a final answer + sources, graded by a rubric judge.
Two external services are needed — a **tool backend** (the four data tools, live
network) and a **rubric grader** (Vals-gated for the full set). Both are injected
behind protocols, so:

  * with FAKE implementations (``FakeFinanceBackend`` + ``KeywordGrader``) the
    adapter runs offline with NO api keys — the smoke-test path;
  * with REAL implementations (live Google/EDGAR/HTTP clients + the Vals rubric
    grader, or a local rubric-LLM approximation over the 50 open questions) it
    runs the actual benchmark.

See docs/SCALING_BENCHMARKS.md for wiring the real services and the gating notes.
"""

from __future__ import annotations

import json
import os
import re
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


class ToolBackend(Protocol):
    """The four Finance-Agent data tools."""

    def google_search(self, query: str) -> str: ...
    def edgar_search(self, query: str, form_type: str = "", cik: str = "") -> str: ...
    def parse_html(self, url: str) -> str: ...
    def retrieve_information(self, query: str) -> str: ...


class RubricGrader(Protocol):
    """Grade a final answer against a per-question rubric + reference answer."""

    def grade(self, question: str, answer: str, rubric: str, reference: str) -> float: ...


@dataclass
class FinanceInstance:
    instance_id: str
    question: str
    reference: str
    rubric: str = ""
    domains: list[str] = field(default_factory=list)


# --- fake services (offline smoke-test path; no api keys) --------------------


class FakeFinanceBackend:
    """Canned responses keyed by substring; a scratch store for parse/retrieve."""

    def __init__(
        self, *, web: dict[str, str] | None = None, pages: dict[str, str] | None = None, edgar: dict[str, str] | None = None
    ) -> None:
        self.web = web or {}
        self.pages = pages or {}
        self.edgar = edgar or {}
        self._store: dict[str, str] = {}

    def _lookup(self, table: dict[str, str], key: str) -> str:
        for k, v in table.items():
            if k.lower() in key.lower():
                return v
        return ""

    def google_search(self, query: str) -> str:
        return self._lookup(self.web, query) or "(no results)"

    def edgar_search(self, query: str, form_type: str = "", cik: str = "") -> str:
        return self._lookup(self.edgar, query) or "(no filings)"

    def parse_html(self, url: str) -> str:
        content = self.pages.get(url, "")
        if content:
            self._store[url] = content
        return content or "(page not found)"

    def retrieve_information(self, query: str) -> str:
        return self._lookup(self._store, query) or "(nothing stored)"


class KeywordGrader:
    """Deterministic fake grader: the reference answer appears in the response."""

    def grade(self, question: str, answer: str, rubric: str, reference: str) -> float:
        norm = " ".join(answer.lower().split())
        return 1.0 if reference.strip() and " ".join(reference.lower().split()) in norm else 0.0


# --- real service shells (require keys / Vals access; see docs) --------------

_NEED_BACKEND = (
    "Finance real tool backend not configured: provide API keys (LLM, Tavily/SerpAPI, SEC EDGAR) "
    "and inject a ToolBackend wrapping github.com/vals-ai/finance-agent tools. See docs/SCALING_BENCHMARKS.md."
)
_NEED_GRADER = (
    "Finance real grader not configured: inject a RubricGrader backed by the Vals grader (gated; "
    "50/537 open) or a local rubric-LLM approximation. See docs/SCALING_BENCHMARKS.md."
)


class _UnconfiguredBackend:
    def google_search(self, query: str) -> str:
        raise NotImplementedError(_NEED_BACKEND)

    def edgar_search(self, query: str, form_type: str = "", cik: str = "") -> str:
        raise NotImplementedError(_NEED_BACKEND)

    def parse_html(self, url: str) -> str:
        raise NotImplementedError(_NEED_BACKEND)

    def retrieve_information(self, query: str) -> str:
        raise NotImplementedError(_NEED_BACKEND)


class _UnconfiguredGrader:
    def grade(self, question: str, answer: str, rubric: str, reference: str) -> float:
        raise NotImplementedError(_NEED_GRADER)


# --- adapter -----------------------------------------------------------------


def _harvest(env: StateEnv) -> Artifact:
    return StateArtifact(answer=env.answer, state=json.dumps(env.store.get("sources", [])))


class FinanceToolSet(ToolSet):
    def __init__(self, backend: ToolBackend) -> None:
        self._b = backend

    def specs(self) -> list[dict[str, str]]:
        return [
            {"name": "google_search", "description": "Web search. args: {query}"},
            {"name": "edgar_search", "description": "Search SEC EDGAR filings. args: {query, form_type?, cik?}"},
            {"name": "parse_html", "description": "Fetch and store a page's content. args: {url}"},
            {"name": "retrieve_information", "description": "Retrieve stored document content. args: {query}"},
            {"name": "submit_answer", "description": "Submit the final answer with sources. args: {answer, sources?}"},
        ]

    def dispatch(self, env: Environment, action: Action) -> tuple[str, bool] | None:
        assert isinstance(env, StateEnv)
        a = action.args
        if action.tool == "google_search":
            return self._b.google_search(str(a.get("query", ""))), False
        if action.tool == "edgar_search":
            return self._b.edgar_search(str(a.get("query", "")), str(a.get("form_type", "")), str(a.get("cik", ""))), False
        if action.tool == "parse_html":
            return self._b.parse_html(str(a.get("url", ""))), False
        if action.tool == "retrieve_information":
            return self._b.retrieve_information(str(a.get("query", ""))), False
        if action.tool == "submit_answer":
            env.answer = str(a.get("answer", ""))
            sources = a.get("sources", [])
            env.store["sources"] = sources if isinstance(sources, list) else [str(sources)]
            return "answer recorded", True
        return None


class FinanceScorer(Scorer):
    name = "finance"

    def __init__(self, grader: RubricGrader) -> None:
        self._grader = grader

    def score(self, instance: Any, submission: str) -> TaskScore:
        raw = self._grader.grade(
            getattr(instance, "question", ""),
            submission,
            getattr(instance, "rubric", ""),
            getattr(instance, "reference", ""),
        )
        return TaskScore(instance_id=str(getattr(instance, "instance_id", "")), success=float(raw))


class FinanceAgentBenchmark(StateBenchmark):
    name = "finance"

    def __init__(
        self,
        *,
        backend: ToolBackend | None = None,
        grader: RubricGrader | None = None,
        instances_data: list[FinanceInstance] | None = None,
    ) -> None:
        self._backend: ToolBackend = backend or _UnconfiguredBackend()
        self._grader: RubricGrader = grader or _UnconfiguredGrader()
        self._data = instances_data

    def instances(self, split: str = "validation") -> list[FinanceInstance]:
        if self._data is not None:
            return list(self._data)
        raise NotImplementedError(
            "Finance real dataset not loaded: pass instances_data (fakes) or load the open 50-question "
            "split from HuggingFace vals-ai/finance_agent_benchmark. See docs/SCALING_BENCHMARKS.md."
        )

    def task_prompt(self, instance: Any) -> str:
        return (
            "Answer the finance question by fetching sources with the tools "
            "(google_search / edgar_search / parse_html / retrieve_information), then call "
            "submit_answer with your answer and its sources.\n\nQuestion: " + instance.question
        )

    def make_env(self, instance: Any) -> StateEnv:
        return StateEnv(harvest=_harvest)

    def toolset(self) -> ToolSet:
        return FinanceToolSet(self._backend)

    def scorer(self) -> Scorer:
        return FinanceScorer(self._grader)


# --- real services (require keys / a grader model; testable via seams) -------


class LiveFinanceBackend:
    """Real tool backend over live HTTP. The network seams (``web_search_fn`` for
    search, ``http_get`` for EDGAR/pages) are injectable so parse/retrieve and the
    dispatch wiring are testable offline; defaults use Tavily + urllib."""

    def __init__(
        self,
        *,
        web_search_fn: Callable[[str], str] | None = None,
        http_get: Callable[[str], str] | None = None,
        tavily_key: str | None = None,
    ) -> None:
        self._web_search_fn = web_search_fn
        self._http_get = http_get
        self._tavily_key = tavily_key or os.getenv("TAVILY_API_KEY")
        self._store: dict[str, str] = {}

    def _get(self, url: str) -> str:
        if self._http_get is not None:
            return self._http_get(url)
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "cooperagents/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - explicit https URLs
            return r.read().decode("utf-8", "replace")

    def google_search(self, query: str) -> str:
        if self._web_search_fn is not None:
            return self._web_search_fn(query)
        if not self._tavily_key:
            raise NotImplementedError("Set TAVILY_API_KEY or inject web_search_fn for Finance google_search.")
        import urllib.request

        body = json.dumps({"api_key": self._tavily_key, "query": query, "max_results": 5}).encode()
        req = urllib.request.Request("https://api.tavily.com/search", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - explicit https URL
            return r.read().decode("utf-8", "replace")

    def edgar_search(self, query: str, form_type: str = "", cik: str = "") -> str:
        import urllib.parse

        url = "https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(query)
        if form_type:
            url += "&forms=" + urllib.parse.quote(form_type)
        return self._get(url)

    def parse_html(self, url: str) -> str:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", self._get(url))).strip()
        self._store[url] = text
        return text

    def retrieve_information(self, query: str) -> str:
        q = query.lower()
        for key, val in self._store.items():
            if q in key.lower() or q in val.lower():
                return val[:2000]
        return "(nothing stored)"


class LLMRubricGrader:
    """Rubric grader via an LLM. Uses litellm by default; a ``complete`` seam is
    injectable for offline tests. For the gated full set, substitute the Vals grader."""

    def __init__(self, model: str, *, complete: Callable[[str], str] | None = None) -> None:
        self.model = model
        self._complete = complete

    def _default_complete(self, prompt: str) -> str:
        import litellm

        resp = litellm.completion(model=self.model, messages=[{"role": "user", "content": prompt}], temperature=0.0)
        return resp["choices"][0]["message"]["content"] or ""

    def grade(self, question: str, answer: str, rubric: str, reference: str) -> float:
        prompt = (
            f"Question: {question}\nRubric: {rubric}\nReference answer: {reference}\nCandidate answer: {answer}\n\n"
            "Is the candidate answer correct per the rubric and reference? Reply with only CORRECT or INCORRECT."
        )
        out = (self._complete or self._default_complete)(prompt).strip().upper()
        return 1.0 if out.startswith("CORRECT") else 0.0


def load_finance(path: str | Path) -> list[FinanceInstance]:
    """Load the finance question CSV (flexible column names) into instances."""
    import pandas as pd

    df = pd.read_csv(path, dtype=str).fillna("")

    def pick(row: Any, *names: str) -> str:
        for n in names:
            if n in row and str(row[n]).strip():
                return str(row[n])
        return ""

    out: list[FinanceInstance] = []
    for i, row in df.iterrows():
        out.append(
            FinanceInstance(
                instance_id=str(row["id"]) if "id" in row and str(row["id"]).strip() else str(i),
                question=pick(row, "question", "Question", "task"),
                reference=pick(row, "answer", "Answer", "reference", "outcome"),
                rubric=pick(row, "rubric", "Rubric"),
            )
        )
    return out


def finance_from_env() -> FinanceAgentBenchmark:
    """Build a Finance benchmark from environment configuration.

    Reads FINANCE_DATA (csv), FINANCE_GRADER_MODEL, and TAVILY_API_KEY. Any
    missing piece leaves that service as an unconfigured shell.
    """
    data_path = os.getenv("FINANCE_DATA")
    grader_model = os.getenv("FINANCE_GRADER_MODEL")
    backend = LiveFinanceBackend() if os.getenv("TAVILY_API_KEY") else None
    grader = LLMRubricGrader(grader_model) if grader_model else None
    data = load_finance(data_path) if data_path else None
    return FinanceAgentBenchmark(backend=backend, grader=grader, instances_data=data)


__all__ = [
    "FinanceAgentBenchmark",
    "FinanceInstance",
    "FinanceToolSet",
    "FinanceScorer",
    "ToolBackend",
    "RubricGrader",
    "FakeFinanceBackend",
    "KeywordGrader",
    "LiveFinanceBackend",
    "LLMRubricGrader",
    "load_finance",
    "finance_from_env",
]
