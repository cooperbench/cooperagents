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
| BrowseComp-Plus | Stub | Needs served retriever index + Qwen3-32B judge |
| Finance-Agent | Stub | Needs live API keys + Vals-gated grader (50/537 open) |

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

## Completing BrowseComp-Plus

The adapter is a stub in `benchmarks/browsecomp.py`. To make it runnable:

1. Stand up the frozen retriever and prebuilt index (BM25 or Qwen3-Embedding-8B)
   from github.com/texttron/BrowseComp-Plus as a local service.
2. Implement a `ToolSet` exposing `search(query)` and `open(doc_id)` over that
   service and `submit_answer(answer)` writing to `StateEnv.answer`.
3. Implement a `Scorer` calling the official Qwen3-32B judge endpoint on the
   final answer (optionally recall/nDCG over visited document ids).
4. `make_env` returns a read-only `StateEnv` whose harvest is the answer plus the
   visited-document set; `instances` loads the 830 obfuscated queries.

Topology fit: search fan-out (Independent or Centralized) with a synthesis reducer.

## Completing Finance-Agent

The adapter is a stub in `benchmarks/finance.py`. To make it runnable:

1. Provide API keys via the environment (LLM provider, Tavily or SerpAPI, SEC
   EDGAR) and, for the full set, Vals platform access. Never commit keys.
2. Implement a `ToolSet` wrapping the four official tools (GoogleSearch,
   EdgarSearch, ParseHTML, RetrieveInformation) plus `submit_answer`.
3. Implement a `Scorer`: call the Vals rubric grader where available, else a
   local rubric-judge approximation over the 50 open questions (documented as an
   approximation, not leaderboard-comparable).
4. `instances` loads the open 50-question validation split.

Topology fit: Centralized (a planner splits into sub-questions, workers fetch, a
synthesizer composes) — the paper's approximately +80% regime. Coordination
raises cost per query, which the benchmark penalizes.
