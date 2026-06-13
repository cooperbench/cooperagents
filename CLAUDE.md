# Self-Evolving Harness for a Team of Agents

## North Star

Create a harness which calls a few LLMs in a team, which can spawn separate execution environment, real-time communication via Redis, Git server, shared disk, task lists, etc. This harness should achieve higher performance than a single agent baseline, in terms of both:

- Task success rate: the chance of passing all tasks
- Execution efficiency: the time spent in executing the tasks

## Guiding principle — co-optimize the agent AND team layers together (READ FIRST)

The point of this project is **co-design**: optimize the *team orchestration*
and the *agent loop* **as one system**, across the seam between them. Not the
team alone, not the agent alone. Hold this line:

1. **Keep the agent loop editable. Use the vendored mini-swe-agent
   (`src/cooperagents/vendor/mini_swe/`) as the worker — never a black-box
   agent (e.g. codex).** A closed agent forecloses co-optimization: you can't
   reach into its prompts/tools/control flow, so the seam disappears. mini-swe
   is small and modifiable on purpose.
2. **Hold the agent constant; improve the *seam*.** Every gain must be
   *attributable* to the harness / the team×agent co-design — measured agent-held-
   constant (see `docs/SEAM_BACKLOG.md`). A change to the team alone or the agent
   alone in isolation is not the goal.
3. **Don't game the metric.** CooperBench's published codex number (62%) is
   *context, not a target*. Do not chase pass-rate by swapping in a stronger
   black-box agent — that measures the agent, not our harness, and defeats the
   objective (a genuinely better co-designed system).
4. **Optimize both axes of the North Star** — success rate *and* execution
   efficiency — via the seam.

If a proposed change can't be attributed to the harness/seam with the agent held
constant, it's out of scope.

### Hard constraints (do not violate)

- **Every agent runs in its OWN container/environment.** Agents must never share
  a single live workspace. Coordination happens via the bus and by seeding an
  agent's fresh container with teammates' work (e.g. `git apply` of their diff),
  never by co-editing one running container. (The team path `_run_isolated`
  enforces this: one `env_factory(agent_id)` per agent, including the integrator.)
- **Never edit `CooperBench/`** — task source + evaluator only.
- **Agent held constant = mini-swe**; never swap in a black-box agent (codex).

## Current State

In CooperBench, an evaluation platform for agent cooperation, we have implementation of both [team harness](https://github.com/cooperbench/CooperBench/tree/main/src/cooperbench/team_harness), and [agent harnesses](https://github.com/cooperbench/CooperBench/tree/main/src/cooperbench/agents), e.g. mini-swe-agent, openhands-sdk, etc. However, the two harnesses are separate and hierarchical. This is good for generalization across different agent harnesses, however, it misses the opportunity to co-design team and agent harnesses.

**This repo (`cooperagents`) is a standalone, from-scratch implementation of the unified harness.** It does **not** modify CooperBench — CooperBench is a sibling checkout used **only** as a task source (the `flash`/`lite`/`core` subsets) and as the evaluator. See `README.md` for architecture and usage.

## Plan

### Stage 1

Unify harnesses (team + mini swe). Instead of using two different levels of harnesses, unify the team and agent harness. 

The unified harness should support:

1. One task for the whole team;
2. or N tasks for N agents, while the harness can spawn more agents as helpers. 

#### Validation

Please validate Stage 1 with CooperBench flash set with azure gpt-5.5.

#### Status — implemented

Stage 1 is implemented as the `cooperagents` package (`src/cooperagents/`):

- **One unified harness, not two levels.** `UnifiedHarness` (orchestrator) and every `Agent` share a single `TeamBus` (task list + messaging + spawn queue). There is no separate team layer wrapping an opaque agent — coordination is just more tools on the agent (`send_message`, `task_*`, `spawn_helper`) alongside `bash`/file tools.
- **Both task shapes supported.** `--mode features` = N tasks for N agents (one seed per feature); `--mode shared` = one objective for a lead + members. In both, the team can **spawn helper agents at runtime** (`spawn_helper` → host supervisor launches a helper, capped by `--max-agents`).
- **Pluggable backends behind interfaces:** bus (`InMemoryBus`/`RedisBus`), environment (`LocalEnv`/`DockerEnv`), LLM (`ScriptedLLM`/`CallbackLLM`/`LiteLLMClient`/`DemoPolicy`).
- **CooperBench is eval-only.** Outputs are written in CooperBench's expected layout (`logs/<run>/team/<repo>/<task>/<f1>_<f2>/` with per-feature `agent{fid}.patch`); `cooperbench eval` scores them unmodified. Verified: CooperBench's own `discover_runs` finds our output.

Validation: full pytest suite + offline flash runs (`LocalEnv`+`DemoPolicy`), **and graded live runs on flash with Azure GPT-5.5** (Docker, CooperBench-scored). **mini-swe is now integrated as the agent worker** (vendored at `src/cooperagents/vendor/mini_swe/`, run on the shared bus/tree via `TeamSpec.worker="mini_swe"`) so the team and agent are optimized *together*. Current numbers (10 flash pairs, mini-swe, GPT-5.5): solo 30% / team 50% — team > solo reproduced (CooperBench published codex numbers: 48% / 62%).

### Plan 2 — New features & self-evolving

#### How to continue — the self-improvement loop (READ FIRST)

The work is run as a repeatable, resumable cycle. Always start here:

- **`docs/SELF_IMPROVEMENT_LOOP.md`** — the loop: resume → pick → build → gate → measure → decide → log → reflect.
- **`docs/SEAM_BACKLOG.md`** — the prioritized queue of team×agent "seam" co-optimizations (S1–S7), the fixed 10-pair baseline, and a measured-delta Done log.
- **`scripts/measure.sh <label>`** — one command: solo + team-shared on the fixed 10 pairs (same agent/model/eval), prints pass-rate.

Invariants: **never edit `CooperBench/`** (task source + evaluator only); keep the agent held constant as **mini-swe — never swap in a black-box agent**; keep the benchmark fixed so deltas compare. Editing the agent loop (prompts/tools/control flow) *is* in scope when it's part of team×agent co-optimization — see the Guiding principle above.

The remaining feature ideas below feed that backlog:

1. **Self-evolving prompts/policies.** After each run, mine the bus audit logs + eval result and update a per-role "playbook" (what worked: decomposition, when to spawn, how to integrate). Persist playbooks and inject them into future runs. A/B new vs. old playbook on a flash slice; keep the winner.
2. **Learned spawn policy.** Replace the fixed `--max-agents` cap with a controller that decides whether/when to spawn based on observed marginal value (does a granted helper raise pass-rate per dollar?). Feed `spawn_metrics` + eval back into it.
3. **Git server + shared disk backends.** Add a real Git remote and a shared volume to the bus/env so members exchange branches and the lead merges (beyond the scratchpad-patch convention). Wire `DockerEnv` to CooperBench task images on a shared docker network.
4. **Richer coordination.** Typed request/response, plan-approval, and blocking long-poll inbox (so agents wait on each other server-side rather than busy-looping).
5. **Auto-decomposition for `--mode shared`.** A planner agent that turns one objective into a dependency-aware task DAG, then assigns/spawns against it.
6. **Self-eval loop.** Run flash → read eval → propose a harness change → re-run → compare, fully autonomously, with guardrails (cost cap, regression gate vs. single-agent baseline).
