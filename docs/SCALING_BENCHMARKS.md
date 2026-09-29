# Scaling-Agent-Systems benchmarks (non-code / state substrate)

This document describes how cooperagents runs on the four benchmarks from
"Towards a Science of Scaling Agent Systems" (arXiv:2512.08296): BrowseComp-Plus,
Finance-Agent, PlanCraft, and WorkBench.

## Goal

Produce ONE additional system entry — cooperagents' in-place stack — on those
benchmarks, to compare against the paper's numbers. The comparison is the
**relative solo-to-team delta** (the paper's own primary framing, normalized to
the single-agent baseline), which is robust to scorer-version differences; the
absolute success rate is reported alongside for rough placement.

The paper reports mostly relative gains (WorkBench: Decentralized +5.7% over an
SAS baseline of 0.629); Figure 2's y-axis is absolute success rate with one box
per architecture, each point being one (model x architecture) configuration
averaged over instances. cooperagents adds a `solo` box and a `team` box, one
point per model.

## Design: composition, not rewriting

cooperagents has two planes. The **artifact plane** (git) is unchanged. The new
**state plane** is additive and opt-in, selected by `TeamSpec.artifact_backend`:

- `env/artifact.py` — `Artifact` / `DiffArtifact` / `StateArtifact`. A git
  environment's `contribution()` still returns `DiffArtifact(git_diff())`
  (byte-identical), so CooperBench and ProgramBench are untouched.
- `env/state.py` — `StateEnv`, a git-free sandbox harvested as a `StateArtifact`.
- `tools.py` — `ToolSet`, which replaces the code-editing tools
  (bash/read_file/write_file) with a benchmark's tools while keeping the
  coordination tools (send_message/task_*/finish/spawn). Inert when unset.
- `reducers.py` — the non-code aggregation step that replaces git merge
  (`best_of_first_nonempty`, `lead_synthesis`).
- `eval/scoring.py` — `Scorer` / `TaskScore`, the pluggable grader.
- `benchmarks/` — one `StateBenchmark` per benchmark (task prompt, per-agent
  env, tool set, official scorer) plus the model-sweep `runner`.

The benchmark repositories are used as libraries and are never modified — the
adapters import their official tools and scorers. The only runtime touch is
rewriting WorkBench's `_CSV_PATHS` to absolute paths in memory.

## Status

| Benchmark | Status | Scoring |
| --- | --- | --- |
| PlanCraft | Ready | Official gym-wrapper reward (offline, deterministic) |
| WorkBench | Ready | Official `is_correct` / `has_side_effects` (current scorer, v1 2024 tasks, offline) |
| BrowseComp-Plus | Ready (real services implemented); smoke-testable offline with fakes | `BM25Retriever` + `LLMJudge` (real), configured by env |
| Finance-Agent | Ready (real services implemented); smoke-testable offline with fakes | `LiveFinanceBackend` + `LLMRubricGrader` (real), configured by env |

Real services for the two network/judge benchmarks are implemented behind the
injected protocols (`benchmarks/browsecomp.py`, `benchmarks/finance.py`): a
pure-Python BM25 retriever, litellm-backed judges/graders, live-HTTP Finance
tools, and dataset loaders. Their network/LLM calls use an injectable seam, so
the logic is unit-tested offline; only live endpoints are exercised at real-run
time. `get_benchmark(name)` builds the real services from environment variables
when present and otherwise returns unconfigured shells that raise with guidance.

## Smoke testing without API keys

BrowseComp-Plus and Finance-Agent each depend on external services (a retriever
and a judge; four data tools and a rubric grader). Those services are injected
behind small protocols, so the adapters run fully offline with fake, deterministic
implementations — the cooperagents plumbing (tools, harvest, scorer) is identical
whether the injected services are fake or real.

- BrowseComp-Plus: `InMemoryRetriever` (keyword search over an in-memory corpus) +
  `SubstringJudge`.
- Finance-Agent: `FakeFinanceBackend` (canned tool responses) + `KeywordGrader`.

Construct a benchmark with fakes plus a small `instances_data` fixture and it runs
through the real agent loop, tool dispatch, and scorer with no keys:

```
uv run pytest tests/test_benchmark_browsecomp.py tests/test_benchmark_finance.py -q
```

`get_benchmark("browsecomp")` / `get_benchmark("finance")` return the benchmark
with unconfigured real-service shells that raise a clear "provide X" error until
you inject real services — so the registry is safe to import without keys, and the
smoke path is reached by constructing the benchmark directly with fakes.

## Setup

Clone the two offline benchmarks as siblings of cooperagents and install PlanCraft:

```
git clone https://github.com/gautierdag/plancraft.git ../Plancraft
git clone https://github.com/olly-styles/WorkBench.git ../WorkBench
uv pip install -e ../Plancraft
```

WorkBench is imported directly from its checkout (no install); set `WORKBENCH_DIR`
if it is not a sibling. The mypy target is Python 3.12 because PlanCraft pulls
numpy>=2.2 whose stubs require 3.12 syntax.

## Running the model-sweep

```
uv run python scripts/scaling_box.py --benchmark plancraft --split val.small --limit 20
uv run python scripts/scaling_box.py --benchmark workbench --split email
```

Models are config-driven via litellm. Pass a comma-separated list or set the
environment variable `COOPER_MODELS`:

```
COOPER_MODELS="gemini/gemini-2.5-flash,gemini/gemini-2.5-pro" uv run python scripts/scaling_box.py --benchmark plancraft
```

Each run prints, per model, the solo and team success rates and the relative
solo-to-team delta, and writes the per-configuration rows to a JSON file for
plotting.

## Comparability notes

- **Agent held constant.** Code benchmarks hold mini-swe constant. Non-code
  benchmarks run the builtin `Agent` loop (already think + communicate + tools),
  because mini-swe's prompts and tools are code-editing specific. The agent is
  held constant across solo and team within each non-code sweep.
- **Different agent than the paper.** The paper used a LangChain scaffold on
  frontier models; cooperagents uses its own loop. Absolute numbers are
  therefore system-to-system, so lead with the relative solo-to-team delta.
- **WorkBench version.** The adapter uses the current (LangChain-free) scorer on
  the v1 (2024, 690-task) data. The paper did not state which WorkBench version
  or scorer it used, so no scorer exactly reproduces its numbers; the current
  scorer is more lenient than the 2024 paper's original (GPT-4 48%/16% vs
  43%/26%).
- **PlanCraft team.** Each agent gets its own simulator copy (Independent MAS
  with selection). PlanCraft is strongly sequential; the paper found MAS hurts
  it (-39% to -70%), so a small or negative team delta is a valid result.
- **WorkBench team.** WorkBench is a negative control (tasks are 1-5 tool calls;
  the paper found +5.7% at best). The solo baseline is the primary number.

## Running BrowseComp-Plus (real)

The retriever, judge, and loader are implemented. A real run needs three
environment variables; any missing one leaves that service as a shell:

```
export BROWSECOMP_CORPUS=/path/to/corpus.jsonl
export BROWSECOMP_QUERIES=/path/to/queries.jsonl
export BROWSECOMP_JUDGE_MODEL=gemini/gemini-2.5-pro
uv run python scripts/scaling_box.py --benchmark browsecomp --split test --limit 20
```

`corpus.jsonl` is `{"docid": ..., "text": ...}` per line (from the
BrowseComp-Plus corpus); `queries.jsonl` is `{"id", "query", "answer", "gold_docs"}`
per line. The retriever is a pure-Python BM25 over that corpus; the judge calls
`BROWSECOMP_JUDGE_MODEL` via litellm. To use a served index or a different judge,
inject a custom `Retriever` / `Judge` into `BrowseCompBenchmark` instead.

## Running Finance-Agent (real)

The tool backend, grader, and loader are implemented. A real run needs API keys
and env configuration:

```
export TAVILY_API_KEY=...
export FINANCE_GRADER_MODEL=gemini/gemini-2.5-pro
export FINANCE_DATA=/path/to/finance_validation.csv
uv run python scripts/scaling_box.py --benchmark finance --split validation --limit 20
```

`LiveFinanceBackend` uses Tavily for web search, SEC EDGAR full-text search, and
stdlib HTTP for page parsing; `LLMRubricGrader` calls `FINANCE_GRADER_MODEL`. Only
the open 50-question split is freely available (`vals-ai/finance_agent_benchmark`
on HuggingFace); the full set and the official Vals grader are gated, so the local
rubric grader is an approximation — not leaderboard-comparable. To use the Vals
grader, inject a custom `RubricGrader`.

Topology fit: BrowseComp — search fan-out (Independent/Centralized) + synthesis;
Finance — Centralized (planner splits into sub-questions, workers fetch, a
synthesizer composes), the paper's approximately +80% regime. Note team
coordination raises cost per query, which Finance penalizes.
