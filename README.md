# CooperAgents

A **self-evolving, unified harness for a team of LLM agents.**

CooperAgents collapses what used to be two stacked layers — a *team harness*
wrapping an opaque *agent harness* — into **one** orchestrator whose agents and
supervisor share a single coordination bus. Because coordination is just more
tools in the agent's hands, a team can **reshape itself at runtime**: any agent
can call `spawn_helper` and the harness launches a new agent on that sub-task.

[CooperBench](https://github.com/cooperbench/CooperBench) is used **only** as a
task source and evaluator — CooperAgents never modifies it.

## Why one harness instead of two

In CooperBench the team layer and the agent layer are separate and hierarchical:
the team decides the whole roster up front (`N agents == N features`) and each
agent is a black box run to completion. That cleanly generalizes across agent
frameworks but forecloses *co-design* — the team can't react to what an agent
discovers mid-run.

Here, the orchestrator and every agent hold the **same `TeamBus`** (task list +
messaging + spawn queue). There is no second level. The headline capability that
unlocks: **dynamic helper spawning** — the team grows on demand, capped by
`--max-agents`.

## Stage 1 — what's implemented

The unified harness supports both shapes from the project plan:

1. **N tasks for N agents** — one seed agent per feature (the generalization of
   coop/team), `--mode features` (default).
2. **One task for the whole team** — a single objective handed to a lead +
   members who decompose it via the shared task list, `--mode shared`.

…and in both, **the harness can spawn more agents as helpers** at runtime.

## Architecture

```
TeamSpec ─▶ UnifiedHarness.run ─┬─ seed agents (threads, own envs)
                                │     each: Agent loop over [bash, files,
                                │       send_message, task_*, spawn_helper]
                                ├─ supervisor: drains spawn queue ─▶ helper agents
                                └─ harvest: patches + coordination/spawn metrics
        shared TeamBus (task list · messaging · spawn queue)
        Environment per agent: LocalEnv (git checkout) | DockerEnv (CB image)
        LLMClient: ScriptedLLM | CallbackLLM | LiteLLMClient | DemoPolicy
```

Module map (`src/cooperagents/`):

| Module | Role |
| --- | --- |
| `harness.py` | the orchestrator + supervisor (dynamic spawning) |
| `agent.py` | the unified tool-calling agent loop |
| `bus/` | `TeamBus` ABC + `InMemoryBus` / `RedisBus` |
| `env/` | `Environment` ABC + `LocalEnv` / `DockerEnv` |
| `llm.py` | LLM client interface + scripted/callback/litellm policies |
| `policies.py` | `DemoPolicy` — deterministic offline policy |
| `metrics.py` | coordination + spawn metrics from the bus logs |
| `eval/` | CooperBench task loading + result writing + `cooperbench eval` |
| `cli.py` | `validate` / `run` / `eval` |

## Install

```bash
uv venv && uv pip install -e ".[dev]"      # core + tests
uv pip install -e ".[all]"                  # + redis + litellm for live runs
```

CooperAgents finds CooperBench via `--cooperbench-dir`, `$COOPERBENCH_DIR`, or a
`CooperBench/` directory beside the cwd.

## Usage

**Offline validation** (no API key, no Docker) — runs the real flash specs
through the full orchestrator with `LocalEnv` + `DemoPolicy`, writes
CooperBench-compatible outputs, and demonstrates helper spawning:

```bash
cooperagents validate --subset flash --limit 5 --max-agents 3
```

**Live run** (Docker task images + a real model via litellm):

```bash
cooperagents run --subset flash --model claude-sonnet-4-6 --max-agents 3 --eval
```

**Score a prior run** with CooperBench:

```bash
cooperagents eval -n cooperagents-run --backend docker
cooperagents eval -n cooperagents-run --dry-run   # just print the command
```

## Evaluation integration

We meet CooperBench where it already looks. Its `discover_runs` scans
`logs/<run>/<setting>/<repo>/<task>/<f1>_<f2>/` for `solo|coop|team` and scores
`agent{fid}.patch` per feature. CooperAgents writes exactly that layout under
`setting="team"`, so `cooperbench eval` scores our runs unmodified. Per-feature
seed patches are scored; helper/member work reaches the score through the seed
agent that integrates it.

## Coordinator notebook experiment

`scripts/bench_compare.py --coordinator --coop-tools --no-seed` enables a
coordinator for a fixed mini-SWE team. It makes one synchronous decision before
workers start, then checks progress and replies every 20 seconds. Workers first
inspect code and discuss proposed edit regions; agreement is a prompt convention,
not an execution barrier. Budget advice is chosen by the model.

Use `--coordination-variant auto-prompt` (the default, formerly `current`/B)
or `--coordination-variant human-prompt` (formerly `human_in_loop`/C) to select
the coordinator and worker prompt text and notice format. Both variants share
the same runtime. `human-prompt` requires the coordinator and notebook to be
enabled. The NLP submission CLI accepts the same values and records them in
the run metadata and variant. The former parameter values are no longer accepted.

The coordinator writes a run-specific Markdown artifact. Each worker mounts its
directory read-only at `/coordination`, outside the code checkout. A short
version/path notice prompts workers to read `/coordination/notebook.md` with their
existing shell tool. Updates replace the file atomically; the poller never pushes
its full text. After context compaction, the next poll repeats the read reminder.
Reading consumes normal worker budget and is not guaranteed by the harness.

Add `--no-coordinator-notebook` for the same coordinator with messages only.
ProgramBench's `coopgitc2` arm accepts the same flag. The implementation and
[k=3 comparison plan](thoughts/shared/plans/2026-09-27-coordinator-notebook.md)
are experimental; feature-score benefit has not yet been measured.

Direct `UnifiedHarness` callers must supply a new `coordinator_notebook_path`
named `notebook.md` and mount its parent directory in each worker factory, e.g.
`task_environment(image, coordinator_dir=path.parent)`. The harness creates the
initial file before calling the factories and verifies readability before workers
start. Set `TeamSpec.coordinator_notebook=False` to omit the file and mounts.

The optional `coordinator_complete(prompt)` callback returns OpenAI-style tool
calls as `[{"name": ..., "arguments": "<JSON object>"}]`. For example:

```python
[
    {"name": "update_notebook", "arguments": '{"content":"# Coordination\\n\\nProposed ownership: pending replies."}'},
    {"name": "send_message", "arguments": '{"recipient":"agent1","content":"Report proposed files and interfaces."}'},
]
```

`[]` is a valid no-op. The default client passes tool definitions to the model API
and reads `message.tool_calls`; the serving stack must parse the model's native
tool syntax into that field. The coordinator ignores `message.content`, including
raw tool markup; a service without a tool parser will therefore make no action.
For the tested Qwen3.5-9B SGLang service, enable `--tool-call-parser qwen3_coder`
on the server; this is a serving setting, not a harness model-format branch.
Each batch allows one full notebook replacement (8,000 characters) and one
message per worker (1,200 characters). Content above either limit is truncated
with a visible notice at the end, included in that limit; a truncated notebook
loses its tail. Unknown fields or recipients reject the entire batch. A failed
write preserves the old notebook and sends none of that batch's messages.
Callback errors or invalid tool calls fail the
run after cleanup; default model failures are logged and retried on a later tick.
Replies are retained until a valid decision succeeds. The first callback runs on
the caller thread, later callbacks serially on the monitor thread; injected
clients must support that usage and bound their own request duration.

## Testing

```bash
uv run pytest          # unit + offline flash integration
uv run ruff check src tests
uv run mypy
```

## Roadmap

Stage 2 (self-evolving) is sketched in `CLAUDE.md`. The work is run as a
repeatable, resumable cycle:

- **The loop** — [`docs/SELF_IMPROVEMENT_LOOP.md`](docs/SELF_IMPROVEMENT_LOOP.md):
  resume → pick → build → gate → measure → decide → log → reflect. Always return here.
- **The backlog** — [`docs/SEAM_BACKLOG.md`](docs/SEAM_BACKLOG.md): prioritized
  team×agent "seam" co-optimizations (S1–S7), the fixed baseline table, and a
  measured-delta Done log.
- **Measure** — `scripts/measure.sh <label>` runs solo + team-shared on the
  fixed 10-pair benchmark (same agent/model/eval) and prints pass-rate.

Work one item at a time: implement → re-measure → keep if it helps → record the delta.

### Optional Langfuse tracing

Install `uv sync --extra mini --extra langfuse`. Tracing is **off by default**,
even when credentials are present. Add `--langfuse` to `scripts/bench_compare.py`
(with `--coop-tools --no-seed`, optionally `--coordinator`) or
`scripts/bench_programbench.py` (a coop arm). Direct callers use
`UnifiedHarness(langfuse=True)` for a single mini-SWE coop-tools team.

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in the process environment
(or the launcher's dotenv file). `LANGFUSE_BASE_URL` selects the server; the
existing `LANGFUSE_OTEL_HOST` and `LANGFUSE_HOST` aliases are also accepted.
The default is `https://us.cloud.langfuse.com`. The SDK also accepts
`LANGFUSE_TRACING_ENVIRONMENT`.

Each `run_id` has a stable session containing separate worker and coordinator
agent traces. Reusing the same `run_id` and agent IDs after a restart appends new
observations to the same session and traces; it does not restore agent state.
Use a new `run_id` for every independent experiment. Launcher-generated run IDs
are fresh on each launch; their `--resume` option skips completed work rather
than restoring an earlier tracing session.

Traces include model inputs, raw responses (including tool calls and reasoning
returned by the provider), token counts, tool results, compaction
calls, and coordinator observations/decisions. Calls use explicit parent spans
so concurrent agents and completion threads remain separate. Cost calculation
uses Langfuse's model pricing when available; custom models may need pricing
configured in Langfuse. Transport credentials are omitted and known environment
secrets are redacted. Task content and tool output are uploaded when enabled.

`--record-trajectory` can run alongside Langfuse. Missing credentials or the
optional SDK fail before containers start; export failures are logged and do not
retry or fail agent work. Traces are flushed when the harness finishes, including
exception paths. A hard process kill cannot guarantee delivery. Custom
`coordinator_complete` callbacks retain ownership of their SDK recording; their
observations and decisions are captured, but raw model usage requires recording
inside that callback.

### Public group chat and opening planning

`--coordination-variant auto-prompt|human-prompt` selects the prompt strategy.
`--coordination-mode direct|group-chat|group-chat-planning` independently selects
communication and opening planning; `direct` remains the default.

| Mode | Communication | Opening behavior |
| --- | --- | --- |
| `direct` | Existing worker inboxes and coordinator notices | Existing prompt strategy |
| `group-chat` | Public conversation among both workers and coordinator | Existing prompt strategy |
| `group-chat-planning` | Same public conversation | Inspect/discuss, confirm a notebook version, then receive the final notebook |

Group modes require two mini-SWE workers, `--coop-tools --no-seed --coordinator`,
the notebook, and no wait protocol. `recipient="group"` addresses everyone;
a roster ID names a participant but the message remains public. Send each
message once. Canonical IDs preserve ordering; ordinary delivery provides complete
messages in pages of at most 8,000 characters. Compaction restores recent
public conversation. The full journal remains available for audit.
Coordinator group observations use `group_chat.unread/recent/backlog` instead of
the direct-mode `replies` field. Forced decisions use the same top-level fields,
with frozen tasks, notebook, budgets and chat. Recent frozen messages include each
worker's latest public report once, even if it falls outside the recent window.
The frozen recent window is bounded to 8,000 characters, with at most two reports
added from outside it, so the finalization chat can be slightly larger.
Later messages cannot change this input or advance the ordinary unread cursor.

Opening planning retains the complete bash tool. `[PLANNING MODE]` instructs
workers to inspect code and discuss it without editing project source, tests,
or configuration. This is a prompt instruction, not a write-permission barrier.
Early changes remain in the trace and raw patch; they are not rolled back or
automatically excluded from scoring.

Workers confirm the current delivered notebook with `confirm_plan(version)` as
their sole action. A revised notebook invalidates confirmations. After both
workers participate and confirm the same candidate, the coordinator calls
`finish_planning(version)`. Each worker receives the same complete final notebook
and `[IMPLEMENTATION MODE]` before either next implementation request starts.

`--planning-step-limit 200 --planning-time-limit 600` limit discussion within the
original worker step/time budget. Either limit causes one coordinator finalization
call, using frozen evidence and only `update_notebook`; this records `forced` and
continues implementation without requiring new worker agreement. Normal agreement
records `agreed`. Technical failures record `error`/`planning_failed`, preserve
actual raw patches, and bypass later generating/repair passes. Forced finalization,
in-flight calls and waiting still consume the original time budget.

The NLP launcher forwards the mode and both limits into run metadata, variant,
environment and actual arguments. Run IDs include the complete prompt/mode arm;
existing snapshot/run directories are refused. It records explicit request settings,
model/service revision fields, pair manifests, image hashes and evaluator patch hashes.
Set `COOPER_MODEL_REVISION` and `COOPER_SERVICE_REVISION` in the authorized runtime
profile before freezing a campaign.

Offline input review (no model or network calls):

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python scripts/export_coordinator_inputs.py \
  --output /path/to/ignored/artifacts/coordinator-inputs
```

This captures actual planner/client messages and tool schemas with an explicitly
synthetic transport and observations. It covers both prompts and all three modes,
including confirmation, forced decisions and the later implementation monitor.

`scripts/summarize_coordination_experiment.py campaign.json --output reports`
reads 18 explicit arm/round/run entries. The campaign supplies six arm definitions,
three rounds, the pair file/hash and frozen source, runtime, image and evaluator
settings. The summary validates canonical pair identities, verdict types and audit
receipts, preserves `agreed`, `forced` and `error` samples, and reports paired changes
and separate worker/coordinator costs. Missing scoring or infrastructure failures
produce coverage information and withheld rates. Zero or missing cost/usage does
not imply free service. The summarizer never calls an LLM judge or launches jobs.
