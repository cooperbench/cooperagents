# Terminal-Bench 3.0 evaluation plan (prepared 2026-08-20)

Goal: evaluate the frozen coopgitc2-fin-min harness (solo + teams) on
Terminal-Bench 3.0 (frontierbench.ai / Harbor framework), as a second
out-of-domain benchmark after ProgramBench.

## Integration shape (decided)

Harbor's runner confines an agent to ONE sandbox (`BaseEnvironment`), which
conflicts with the project hard constraint that every team agent runs in its
own container. Therefore Harbor is used the same way as CooperBench and
ProgramBench: **task source + evaluator only**. Our runner executes the team
on the fleet; the task's own verifier scores the result. The harness is not
modified.

## Task anatomy (from terminal-bench/terminal-bench@latest, 74 tasks)

    <task>/task.toml          timeouts, [verifier] environment_mode,
                              artifacts = [paths the verifier consumes]
    <task>/instruction.md     the agent-facing task statement
    <task>/environment/       Dockerfile (+data) for the AGENT environment
    <task>/tests/             Dockerfile + test.sh + pytest suite (VERIFIER,
                              runs separately; writes /logs/verifier/reward.json)
    <task>/solution/          oracle solution (never shown to agents)

62/74 tasks are single-container; 12 use docker-compose (excluded from the
pilot). Categories span science, GPU, databases, crypto, CAD, finance.

## Execution design

1. **Image**: build `environment/Dockerfile` per task on the fleet node,
   tagged `tb3/<task>:local` (cached across agents/attempts).
2. **Team substrate**: task workdir (usually /app) is git-initialized at
   seed time in each agent's container (same init commit via the shared bare
   repo), so the existing git-share/merge/selection machinery applies
   unchanged. This is the seam experiment: TB tasks are 1-task-for-the-team
   (mode `shared`), the second OOD axis after ProgramBench's 3-agent setting.
3. **Submission**: unlike ProgramBench, success = artifacts/state in the
   environment, not a repo diff. The integrator materializes the merged tree
   in a fresh container, re-runs any build steps, and the declared
   `artifacts` paths are extracted from THAT container.
   - Caveat (open): tasks whose state lives outside the git-tracked workdir
     (system packages, databases) will under-transfer through a git merge.
     The pilot measures how often this bites; candidate fix is an
     artifact-level selection (pick one agent's whole container) rather
     than a merge, using the existing `select_integration` hook.
4. **Verification**: build `tests/Dockerfile`, mount the extracted artifacts
   at their declared paths plus /logs, run `test.sh`, read
   `/logs/verifier/reward.json` (binary or graded reward). This mirrors
   Harbor's `environment_mode = "separate"` semantics.
5. **Budgets**: task.toml sets `[agent] timeout_sec` (typ. 18000s) — use it
   as the per-agent wall clock. Endpoint: the held B200 (135 tok/s/stream)
   with the full starvation-safe stack (preflight, heartbeats, guard,
   conn-close, sized timeouts).

## Pilot protocol

- 6 single-container tasks sampled across categories (no GPU tasks).
- Arms: solo and t3 (team-shared), 1 cell each = 24 agent-runs.
- Success metrics: reward from the task verifier; efficiency: duration,
  steps, tokens. Resource validation identical to ProgramBench cells.
- Decision gate: if the git-merge submission path loses >1 task to
  untracked state, switch teams to whole-container selection before the
  full 62-task run.

## Status

- [x] Harbor CLI installed (0.21.0); dataset downloaded to ~/terminalbench
- [x] Task anatomy inspected (single vs compose census: 62/12)
- [x] Adapter skeleton: src/cooperagents/adapters/terminalbench.py
- [x] Runner wiring (COOPER_BENCHMARK env in bench runner)
- [x] Verifier harness verified end-to-end (smoke: 60/103 tests, reward 0.0
      binary). Five integration bugs found+fixed by the smoke iterations:
      no git in task images (git layer), base-commit capture ordering,
      agents stashing their submission away (prompt + stash recovery),
      share-fallback collection (agents wiping trees), relative docker -v
      paths in evaluate. Rewards are ALL-OR-NOTHING per task (reward.txt
      from pytest rc); pass-fraction captured as diagnostic from ctrf.json.
- [x] Batch running: first 10 tasks x solo/t2/t3/t4 (runs/tb3_shepherd.sh)
