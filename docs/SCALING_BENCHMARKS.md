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

## v3 additions: code benchmarks (git substrate)

Version 3 of the paper expands the evaluation from four to six benchmarks
(N=260 configurations). The two additions are software-engineering benchmarks:
SWE-bench Verified (resolve a GitHub issue in a repository) and Terminal-Bench
(terminal tasks).

Both run on cooperagents' original git substrate (the `BenchmarkAdapter`
contract: `image` / `task_for` / `submit` / `evaluate`), which is separate from
the state substrate used by the four non-code benchmarks. They run through the
git-substrate runner (`scripts/bench_programbench.py`, selected by the
`COOPER_BENCHMARK` environment variable) with the mini-swe worker, and are scored
by each benchmark's official evaluator.

Terminal-Bench has two adapters for two dataset formats:

- `src/cooperagents/adapters/terminalbench.py` — Terminal-Bench 3.0 / Harbor tasks
  (`task.toml` + `environment/Dockerfile` + `instruction.md`, artifact/reward grading);
  dataset root via `TB3_ROOT`, benchmark name `terminalbench`.
- `src/cooperagents/adapters/terminalbench_core.py` — the classic
  `terminal-bench-core-0.1.1` flat format (`task.yaml` + root `Dockerfile` +
  `docker-compose.yaml` + `run-tests.sh` + pytest `tests/`), benchmark name
  `terminalbench-core`. Dataset root defaults to
  `data/terminal-bench-core-0.1.1` (override with `TBC_ROOT`). The agent works in
  `/app`; the submission is the git diff, and `evaluate()` applies it to a fresh
  task container and runs the task's own pytest suite (resolved iff all tests
  pass). Scope: single-container tasks only (one compose service — 77/80 of the
  core set); multi-container tasks are excluded by `instances()`. Verified
  end-to-end on `hello-world` (agent creates the file, `evaluate()` scores
  reward 1.0). Run it through the same git-substrate runner:

```
COOPER_BENCHMARK=terminalbench-core AZURE_OPENAI_DEPLOYMENT=sap/gemini-2.5-flash uv run python scripts/bench_programbench.py --instance hello-world --arm solo --rep r1 --step-limit 20 --env-brief
```

SWE-bench Verified uses `src/cooperagents/adapters/swebench.py`, configured by
environment variables:

- `SWEBENCH_DATASET` — HuggingFace dataset id (default `SWE-bench/SWE-bench_Verified`). swebench >=4 grades against instances that carry an `image` field; the classic `princeton-nlp/SWE-bench_Verified` lacks it and makes the grader raise `KeyError('image')`. For an arm64 grade, build a one-instance local JSONL from this dataset with `image` rewritten to the `sweb.eval.arm64.*` tag and pass that path as `--dataset_name`.
- `SWEBENCH_DATA` — path to a local JSONL to load instances offline instead of HuggingFace.
- `SWEBENCH_IMAGE_TEMPLATE` — per-instance image name. Default `swebench/sweb.eval.x86_64.{key}:latest`, where `{key}` is the instance id with `__` rewritten to `_1776_` (the published Docker Hub tag convention, e.g. `pylint-dev__pylint-4551` → `swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-4551:latest`). `{id}` (raw id) is also available. On Apple Silicon, point it at the native arm64 convenience builds — `SWEBENCH_IMAGE_TEMPLATE='swebench/sweb.eval.arm64.{key}:latest'` — to avoid x86_64 emulation; set a `ghcr.io/epoch-research/...` or locally built name as needed.
- `SWEBENCH_MODEL_NAME` — the `model_name_or_path` written into predictions (default `cooperagents`).
- `SWEBENCH_MAX_WORKERS` — evaluator worker count (default `4`).

The agent edits the repository at `/testbed` inside the per-instance image and the
submission is its git diff. `evaluate()` writes a `predictions.jsonl` entry and
runs the official harness (`python -m swebench.harness.run_evaluation`), which
applies the patch to a fresh image and runs the task's FAIL_TO_PASS / PASS_TO_PASS
tests. A real run requires the `swebench` package and the per-instance Docker
images; the adapter wiring (image name, task text, predictions format, instance
loading) is unit-tested offline, and the live scoring path requires that
infrastructure.

### Team arms (FACTORY23-style) for the code benchmarks

SWE-bench and Terminal-Bench run the same team arm config as the FACTORY23 series
(`docs/FACTORY23_REPRODUCTION.md`), via `bench_programbench.py`. Example (3-agent
team on SAP AI Core Gemini):

```
set -a; source .env; set +a
AZURE_OPENAI_DEPLOYMENT=sap/gemini-2.5-flash COOPER_BENCHMARK=swebench SWEBENCH_DATA=data/swebench_verified.jsonl uv run python scripts/bench_programbench.py --instance <id> --arm coopgitc2 --rep r1 --team-size 3 --repair --completion-gate --env-brief --presub-merge --step-limit 1000 --agent-time-limit 14400
```

- The worker model is set by `AZURE_OPENAI_DEPLOYMENT` (despite the name). Use a
  native litellm provider id — `sap/<deployment>` for AI Core (auth via `AICORE_*`
  from `.env`), or an `openai/`-compatible endpoint via `OPENAI_BASE_URL`/`OPENAI_API_KEY`.
  `sap/`/`gemini/`/`vertex_ai/`/`bedrock/` are passed through to litellm as-is; a
  bare HF-style name (`Qwen/Qwen3.5-9B`) is treated as `openai/`-compatible.
- `--arm coopgitc2` = `coop_tools` + `git_share` + the coordinator (loop/stall/collision nudges).
- `--repair` / `--completion-gate` / `--presub-merge` / `--env-brief` / `--team-size` as in FACTORY23.

Honest degradations (SWE-bench/Terminal-Bench declare no `build_artifact` /
`reference_binary`, so `verification` falls back):

| Flag | Behavior here |
| --- | --- |
| `coopgitc2`, `--team-size` | full — git substrate; SWE-bench `/testbed` is a git repo, TB git-inits the workdir in `setup_env` |
| `--env-brief` | now faithful — adapters implement `brief()` (env orientation text) |
| `--completion-gate` | gate = "teammate merge clean AND repo compiles (`compileall`/make)", NOT FAIL_TO_PASS / the TB verifier |
| `--presub-merge` | merge is real; integration SELECTS by patch size (the `score` fallback), not pass-rate |
| `--repair` | repairs compile breakage only, never test failures |

The honest success signal is the separate `evaluate()` pass (the SWE-bench harness
/ the TB verifier), which `bench_programbench.py` does not invoke inline. Infra
prerequisites: a running Docker daemon; SWE-bench per-instance images (or
`SWEBENCH_DATA` for offline instance loading) plus the `swebench` package for
scoring; Terminal-Bench `TB3_ROOT` task tree (single-container tasks only).

#### Verified live smoke (2026-10-19)

A single-instance live run confirmed the SWE-bench path end-to-end on SAP AI
Core: pull the per-instance image, run the agent in the container, authenticate
via `AICORE_*`, and emit a git-diff submission.

```
AZURE_OPENAI_DEPLOYMENT=sap/gemini-2.5-flash COOPER_BENCHMARK=swebench SWEBENCH_DATA=data/swebench_verified.jsonl SWEBENCH_IMAGE_TEMPLATE='swebench/sweb.eval.arm64.{key}:latest' uv run python scripts/bench_programbench.py --instance pylint-dev__pylint-4551 --arm solo --rep smoke --step-limit 40 --agent-time-limit 1800 --env-brief --runs-dir runs/swebench_smoke
```

Result: 33 steps, a 1809-byte patch editing `pylint/checkers/base.py`, submission
written. Four real defects were fixed in the process:

- **Image name.** The published Docker Hub tags rewrite `__` to `_1776_`; the old
  default template used the raw id and never resolved. Fixed via the `{key}`
  placeholder (see `SWEBENCH_IMAGE_TEMPLATE` above).
- **litellm SAP + Gemini thinking.** With thinking enabled, `sap/gemini-2.5-flash`
  returns `reasoning_content` as a *list* of thinking blocks, but litellm 1.102.1's
  `GenAIHubOrchestrationConfig.transform_response` validates it as a string and
  raises `ValidationError` on nearly every call, wedging the agent in a retry
  loop. `cooperagents/_litellm_patches.py` installs an idempotent patch that
  flattens list `reasoning_content` to a string before validation; it is applied
  on both the state path (`LiteLLMClient`) and the git path (vendored
  `litellm_model`). No behaviour is lost — the answer lives in `message.content`.
- **Grading dataset.** swebench >=4 grades against instances carrying an `image`
  field; the classic `princeton-nlp/SWE-bench_Verified` lacks it (`KeyError('image')`).
  Default dataset is now `SWE-bench/SWE-bench_Verified` (see `SWEBENCH_DATASET`).
- **Issue text never reached the agent (the big one).** `bench_programbench.py`
  passes a bare instance-id *string* to `task_for`, but the adapter only read
  `problem_statement` from a `SWEBenchInstance` object — so the `## Issue` section
  silently degraded to just the id. Agents were solving an issue they could not see
  and guessed the wrong module. `SWEBenchAdapter._resolve()` now looks the id up in
  the dataset so the full issue text is delivered. Verified: with the fix, the solo
  agent edits `pylint/pyreverse/diagrams.py` (the correct module) instead of the
  unrelated `checkers/base.py` it guessed before.

#### Team arm (coopgitc2) — pipeline verified, 2026-10-20

A 3-agent `coopgitc2` run (`--repair --completion-gate --env-brief --presub-merge
--team-size 3`, 1398 s) confirmed every FACTORY23 component fires on the git
substrate: the **coordinator** delivered 9 nudges (7 LOOP, 2 COLLISION) that
measurably changed agent behaviour; **git_share** fetched teammate branches;
**presub-merge** integrated 3 candidates; the **completion-gate** rejected
submissions (compile failures, teammate merge conflicts) with the rejection
countdown + forced-accept; **repair** flipped a gate-fail attempt into a gate-pass
one; **env-brief** was present in every prompt. The task board stayed unused
(`tasks_total=0`) — agents never spontaneously post subtasks, consistent with the
state benchmarks.

Honest outcome: both solo and team graded **unresolved** (`no_tests_collected`).
Remaining (agent-behaviour, not plumbing) blockers, in priority order: (1) agents
write scratch files (`patch.txt`, `base_content.py`, helper `*.py`) into the repo
root that the git-diff submission captures — a stray top-level module breaks pytest
collection and the 105 KB team patch was 97% one such dump; (2) `presub-merge`
select-by-size favours the *most* bloated candidate when verification scores are
degenerate — it should prefer the minimal valid diff; (3) the compile gate
(`compileall .`) false-positives on pre-existing Python-2/3 files under
`tests/functional`. These are documented in `runs/swebench_smoke_team/` and are the
next things to fix for a solve, separate from the now-verified harness.


The **state** benchmarks (PlanCraft/WorkBench/BrowseComp/Finance) take the
*portable* half of this config via `scaling_box.py`: the `team` arm defaults to the
coordinator (loop/stall nudges over the bus) + a task-board protocol + any
benchmark `brief()`; the git-only reconciliation (`git_share`, `--presub-merge`,
the repair integrator) has no analogue because state benchmarks reconcile via a
reducer, not a merge. Disable with `--no-coordinator` / `--no-task-board`.


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

## Running via SAP AI Core

cooperagents reaches models through litellm, which supports the SAP Generative AI
Hub (SAP AI Core) via the `sap/` provider prefix. No code change is required:
`LiteLLMClient` passes the model string to litellm, which performs the OAuth2
token exchange, resolves the deployment from the resource group, and routes the
request. (Verified with litellm 1.102.1, provider `SAP_GENERATIVE_AI_HUB`.)

Credentials are read from the environment and must never be committed. Use either
the service-key JSON or the individual values.

```
export AICORE_SERVICE_KEY="$(cat service_key.json)"
export AICORE_RESOURCE_GROUP=default
```

```
export AICORE_CLIENT_ID=...
export AICORE_CLIENT_SECRET=...
export AICORE_AUTH_URL=...
export AICORE_BASE_URL=...
export AICORE_RESOURCE_GROUP=default
```

Model strings use the `sap/` prefix with a model id deployed in the resource
group, for example `sap/gemini-2.5-flash` or `sap/anthropic--claude-3.5-sonnet`.
The id must match a deployment; litellm lists deployments at
`AICORE_BASE_URL/lm/deployments`.

Confirm attachment with a single call before a full sweep:

```
uv run python scripts/aicore_smoke.py --model sap/gemini-2.5-flash
```

Then run the sweep with the `sap/` model string:

```
uv run python scripts/scaling_box.py --benchmark plancraft --models sap/gemini-2.5-flash --limit 20
```

Requests routed through SAP AI Core consume BTP credits rather than direct Gemini
billing; the token counts in the cost estimate still apply.

## Cost estimate (Gemini)

Estimated API cost for a k=3 run of both configurations (solo and team) across
all four benchmarks on gemini-2.5-flash (the scaling_box.py default). Pricing as
of 2026-09-29: gemini-2.5-flash is $0.30 per 1M input tokens and $2.50 per 1M
output tokens; gemini-2.5-pro is $1.25 / $10.00 input/output.

Cost model: one LLM call per agent step. Per-instance call counts are
solo = S and team = 3*S + R, where S is the average steps per agent and R is the
reducer LLM-call count. Both S and R differ per benchmark because the team setup
differs:

| Benchmark | Team setup | Reducer calls R | Avg steps S | Input tokens/call |
| --- | --- | --- | --- | --- |
| PlanCraft | N independent simulators + mechanical success-selection | 0 | 15 | ~2500 |
| WorkBench | N concurrent thread-local sandboxes + synthesis | 1 | 5 | ~2000 |
| BrowseComp-Plus | search fan-out + lead-synthesis | 1 | 8 | ~4000 |
| Finance-Agent | decompose (planner + workers + synthesis) | 2 | 10 | ~4000 |

Output is estimated at ~300 tokens per call (the action JSON). Per-instance cost
for both configurations at k=3 on gemini-2.5-flash:

| Benchmark | Calls/instance (x k=3) | Cost/instance |
| --- | --- | --- |
| PlanCraft | 180 | $0.27 |
| WorkBench | 63 | $0.085 |
| BrowseComp-Plus | 99 | $0.19 |
| Finance-Agent | 126 | $0.25 |

Scenario totals (both configurations, k=3, gemini-2.5-flash):

| Scenario | Instances per benchmark | Total |
| --- | --- | --- |
| Pilot | 20 each | ~$16 |
| Paper-scale | PlanCraft 110, WorkBench 690, BrowseComp 100, Finance 50 | ~$120 |

Main cost driver: Gemini 2.5 thinking tokens. Gemini 2.5 Flash bills thinking
tokens as output and enables thinking by default; LiteLLMClient does not set a
thinking budget. With ~1000-2000 thinking tokens per call, the output component
increases by a factor of about 4 to 7, raising the paper-scale estimate to about
$300 to $500. Set a thinking budget in LiteLLMClient to hold the lower estimate.
gemini-2.5-pro raises all figures by a factor of about 4 to 5.

Caveats: S (average steps) and the input-token sizes are estimates; the largest
uncertainties are the thinking-token setting and S. The estimate excludes an
iso-compute solo configuration; adding one (solo granted N times the budget for
compute-matching) increases the solo-configuration cost proportionally.

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
