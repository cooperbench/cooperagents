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
