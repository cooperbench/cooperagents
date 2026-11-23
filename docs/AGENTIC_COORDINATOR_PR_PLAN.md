# Method 2.1 PR Plan: Agent-Mode Coordinator

Status: Revision 4 for user review (user decisions Q1–Q8 of 2026-11-22; System-1 gate on
the OpenRouter Decisions API with `liquid/d1.2-ultra`; PR count reduced from 8 to 5).
Implementation (Codex) starts only after the user approves
`docs/AGENTIC_COORDINATOR_SPEC.md` and this plan.
Base: `origin/cooperagents-coordinator-training` at
`829a1702518a9a5864c5452d243c4464d52c4d4f`.
PR target branch for PR 1 to PR 5: `cameron-chen/goal-agentic-coordinator`. Each PR branch
starts from the current head of that branch.
Upstream merge: after the experiments on `cameron-chen/goal-agentic-coordinator` complete,
one PR merges that branch into `cooperagents-coordinator-training` (Section 6). That merge
PR adds no changes and is not counted in the 5 PRs.
Total PRs: 5 (PR 1 through PR 5).

## 1. PR list

All PRs in this table target `cameron-chen/goal-agentic-coordinator`.

| PR | Title (Conventional Commit) | Branch | Wave | Depends on | Touches `src/` | Qualification refresh |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `feat: add agent coordination spec and contracts` | `cameron-chen/agent-coord-1-contract` | 1 | none | yes | yes |
| 2 | `feat: add structural signals and system-1 gate` | `cameron-chen/agent-coord-2-signals-gate` | 2 | 1 | yes | yes |
| 3 | `feat: add agent-mode coordinator core` | `cameron-chen/agent-coord-3-coordinator` | 2 | 1 | yes | yes |
| 4 | `feat: wire agent coordinator, percentiles, and bench flags` | `cameron-chen/agent-coord-4-wiring` | 3 | 2, 3 | yes | yes |
| 5 | `test: add protected agent coordinator macro suite` | `cameron-chen/agent-coord-5-protected` | 4 | 4 | no | no |

Mapping from revision 3 (8 PRs): PR 1 = old PR 0 + PR 1; PR 2 = old PR 2 + PR 3;
PR 3 = old PR 4; PR 4 = old PR 5 + PR 6; PR 5 = old PR 7.

Reasons for each merge:

- Old PR 0 (docs) and old PR 1 (contracts) are reviewed together: the contract types are a
  direct transcription of the spec, so one approval covers both.
- Old PR 2 (signals) and old PR 3 (gate) are both pure-function modules with no thread or
  environment code. The gate consumes `Signal` records directly, so one PR tests the
  `Signal` → `GateState` → `GateDecision` path end to end.
- Old PR 5 (percentiles) and old PR 6 (wiring) both edit `scripts/bench_compare.py` and
  regenerate the qualification files. One PR removes that double edit and one rebase.
- The protected suite stays a separate PR: `scripts/check_protected_tests.py` rejects a
  change set that mixes `tests/protected/` with any other path
  (`"Protected test allowance requires a test-only change"`).

## 2. Dependency graph

```text
PR1 (spec, plan, contracts, prompt constants; merged first, then frozen)
  ├─> PR2 (Tier 1 signals + System-1 gate)   ┐ wave 2, parallel,
  └─> PR3 (coordinator core)                 ┘ no shared source file
            └─> PR4 (harness + bench_compare wiring, percentiles, flash smoke)
                      └─> PR5 (protected macro suite, family "agent")
```

Parallel groups:

- Wave 2: PR 2 and PR 3 branch from PR 1's merge commit and develop in parallel. PR 3
  depends on PR 2 only through the PR 1 types: `AgentCoordinator` takes a `SignalTracker`
  and a gate object as constructor arguments, and its unit tests use stubs with the PR 1
  signatures. The integration test with the real PR 2 classes is part of PR 4.
- Waves 3 and 4 are serial.

Merge order: 1 → {2, 3 in any order} → 4 → 5.

## 3. Contract freeze (PR 1)

After PR 1 merges, the following are frozen for waves 2–3. A change to any of them needs a
separate contract PR merged before dependent PRs rebase.

- `src/cooperagents/agent_coordination_types.py`: `SignalKind`, `Severity`, `DiffProbe`,
  `StepRecord`, `Signal`, `GateState`, `Decision`, `ReasonCode`, `DecisionSource`,
  `GateDecision` (with `candidate_kind`, `probabilities`, `threshold`, `model`,
  `input_tokens`, `completion_tokens`, `error`; spec Section 4.2), `DecisionQuestion`
  (with `criteria`), `DecisionRequest`, `DecisionResult` (with `cost_usd`, `provider`,
  `response_id`), the `DecisionComplete` and `AgentComplete` type aliases, `WorkerPlan`,
  `OpeningPlan`, `SpecFinding`, `SpecVerdict`, `SignalConfig`, `GateConfig`
  (decision-model fields and `validate()`, spec Section 4.3.3), `AgentCoordinatorConfig`,
  schema version constants (`gate-state/1`, `gate-decision/1`, `opening-plan/1`,
  `agent-coordinator-metrics/1`), `to_json`/`from_json` helpers using
  `dataclasses.asdict` and strict key checks.
- `src/cooperagents/agent_coordinator_prompts.py`: every prompt constant of spec
  Section 4.6 (gate instructions, `decision` question instructions, `WAKE`/`SKIP`
  criteria, opening-plan, wake and spec-check prompts) and the `prompt_id` helper. PR 2
  and PR 3 import this module and do not edit it, so the two wave-2 PRs share no source
  file. Prompt text changes found necessary in the PR 4 flash smoke are made in PR 4,
  which is serial; each change produces a new `prompt_id`.
- `TeamSpec.agent_coordinator` and the three-family check in `TeamSpec.__post_init__`.
- Golden JSON fixtures under `tests/fixtures/agent_coordination/contract/`.
- Tool schemas (Section 5.2 of the spec) as constants in the types module, so PR 3 and
  polar-ext read one definition.

## 4. Per-PR scope and acceptance

Common verification for every code PR (run from the repository root). `829a1702` is the
merge base of `cameron-chen/goal-agentic-coordinator` with
`origin/cooperagents-coordinator-training`; the checker default base
(`origin/cameron-chen/coord-baselines`) is not used.

```sh
uv run pytest -q
uv run pytest tests/protected -q
python3 scripts/check_protected_tests.py --base 829a1702
git diff --name-only 829a1702...HEAD -- tests/protected scripts/check_protected_tests.py CooperBench src/cooperagents/vendor src/cooperagents/workers   # must print nothing (PR 5 excepted for tests/protected)
```

For PRs that touch `src/cooperagents/**/*.py`, also:

```sh
python3 scripts/qualify_direct_capture.py   # only when a file in producer_identity changed
python3 scripts/qualify_published_reader.py
git diff --exit-code src/cooperagents/reader_qualification.json src/cooperagents/direct_capture_qualification.json  # clean after commit
uv run pytest -q tests/test_published_profiles.py tests/test_direct_capture.py
```

### PR 1: specification and contracts

- Files: `docs/AGENTIC_COORDINATOR_SPEC.md`, `docs/AGENTIC_COORDINATOR_PR_PLAN.md`,
  `src/cooperagents/agent_coordination_types.py` (new),
  `src/cooperagents/agent_coordinator_prompts.py` (new; all prompt constants and the
  `prompt_id` helper), `src/cooperagents/types.py` (`TeamSpec.agent_coordinator`,
  `__post_init__`), `src/cooperagents/harness.py` (only: in `UnifiedHarness.run`,
  `if spec.agent_coordinator: raise NotImplementedError("agent coordinator is not wired")`
  placed before environment creation), `tests/test_agent_coordination_contract.py` (new),
  `tests/fixtures/agent_coordination/contract/*.json` (new), both qualification JSON files
  (`harness.py` and `types.py` are in `producer_identity`).
- Acceptance:
  1. User approval of both documents (revision 4). Q1–Q8 are resolved (spec Section 12.1);
     spec Section 12.2 lists no open questions, so no PR is blocked on a decision.
  2. Every contract type round-trips through `to_json`/`from_json` and equals its golden
     file.
  3. Unknown keys and wrong schema versions raise `ValueError`.
  4. With the required `TeamSpec` fields set, `TeamSpec(coordinator=True, agent_coordinator=True)`
     and `TeamSpec(deterministic_coordinator=True, agent_coordinator=True)` raise
     `ValueError("Coordinator families are mutually exclusive")`.
  5. `UnifiedHarness().run(TeamSpec(..., agent_coordinator=True), env_factory=unexpected)`
     raises `NotImplementedError` without calling `env_factory`.
  6. `GateDecision` golden files cover the sources `model` (with `probabilities`,
     `threshold`, `model`, `completion_tokens=0`), `fallback` (with `error`),
     `rule`, `random` and `budget` (with `probabilities`, `threshold` and `model` null).
     A `GateDecision` with `source="model"` and `probabilities=null` raises `ValueError`.
  7. `GateConfig()` with `policy="model"` resolves to `model="liquid/d1.2-ultra"`,
     `base_url="https://openrouter.ai/api/alpha"`, `api_key_env="OPENROUTER_API_KEY"`,
     `wake_threshold=0.5`, `timeout_s=3.0`; with `policy` `rule` or `random` these fields
     are `None`. `GateConfig.validate()`: `policy="model"` with an empty `model`, a
     `base_url` that fails spec Section 4.3.3, an empty `api_key_env` variable,
     `wake_threshold` outside `[0, 1]`, or `timeout_s` outside `(0, 10]` raises
     `ValueError`; `policy` `rule` or `random` with any decision-model field set raises
     `ValueError`. No default reads the worker model.
  8. `prompt_id` has the form `<name>-v<n>:<sha256(text)[:12]>` for every prompt constant;
     the gate `prompt_id` changes when any of the gate instructions, the `decision`
     question instructions or one `WAKE`/`SKIP` criterion changes (spec Section 4.3.1).
  9. Protected suites pass unchanged.
- Verification: common commands; `uv run pytest -q tests/test_agent_coordination_contract.py`.

### PR 2: Tier 1 structural signals and System-1 gate

- Files: `src/cooperagents/structural_signals.py` (new), `src/cooperagents/system1_gate.py`
  (new), `tests/test_structural_signals.py` (new), `tests/test_system1_gate.py` (new),
  `tests/fixtures/agent_coordination/signals/*` (new),
  `tests/fixtures/agent_coordination/gate/*` (new), qualification JSON files.
- Scope, signals: `parse_steps`, `parse_probe`, `DiffProbe.command()` (the shell text of
  spec Section 3.1.2), `DiffProbe.run(env)`, `SignalTracker`, `overlap_signals`,
  test-command classifier, debounce. Reuses `git_snapshots._regions` and
  `_overlapping_regions` by import.
- Scope, gate: `WakeBudget`, `RuleGate`, `DecisionGate` (injected `DecisionComplete`,
  result validation and renormalization, threshold, timeout, fallback; no cache),
  `HttpDecisionClient` implementing the OpenRouter `POST /api/alpha/decisions` schema
  exactly (spec Section 4.3.2, standard-library `urllib.request`), `reason_code`
  derivation (spec Section 4.2.1), `RandomGate` (matched schedule of spec Section 6.2),
  `build_gate_state`. Prompt texts are imported from `agent_coordinator_prompts.py`
  (PR 1).
- Acceptance, signals:
  1. Each `SignalKind` in spec Section 3.2 has positive, negative and boundary
     (threshold − 1, threshold, threshold + 1) golden cases.
  2. Debounce and re-arm rules of spec Section 3.3 have one case each.
  3. `DiffProbe.run` against a `LocalEnv` repository: fingerprint changes on a tracked edit,
     on a new untracked file, and stays equal after edit-and-revert; `.cb_*` and `patch.txt`
     changes do not change it; truncation yields `fingerprint=None`.
  4. Fixture derived from a recorded `det_main` trajectory produces a documented signal
     count (golden file).
  5. No import of `harness.py` from `structural_signals.py` or `system1_gate.py`.
- Acceptance, gate:
  6. Rule table of spec Section 4.3 and every constraint of Section 4.4 have one case each;
     a budget-blocked candidate makes no `DecisionComplete` call.
  7. `DecisionGate` sends `signal`, `peer_message` and `heartbeat` candidates to the
     decision model (Option A); `RuleGate` is not applied before the call.
  8. Threshold: with `wake_threshold=0.5`, `p_wake` 0.49 yields `SKIP`, 0.50 yields `WAKE`;
     with `wake_threshold=0.0` every passing candidate yields `WAKE`.
  9. A `DecisionComplete` that sleeps past `timeout_s`, raises, or returns an invalid result
     (missing option, extra option, value outside `[0, 1]`, sum 1.2, NaN) yields the
     `RuleGate` result with `source="fallback"` and the matching `error` code; a sum within
     `1e-3` of 1 is accepted and renormalized.
  10. `reason_code` for single-signal, multi-signal (severity then map order),
      `peer_message` and `heartbeat` candidates equals spec Section 4.2.1, for every source.
  11. `HttpDecisionClient` against a local `http.server` stub serving
      `/api/alpha/decisions` (base URL `http://127.0.0.1:<port>/api/alpha`):
      a. the request path, the `Authorization` and `Content-Type` headers and the JSON body
         (`model`, `state`, `questions.decision` with `type="choice"`, `instructions`,
         `criteria`) equal spec Section 4.3.2;
      b. the spec Section 4.3.2 example response maps to `probabilities`, `chosen`,
         `input_tokens=734`, `completion_tokens=0`, `cost_usd=0.000030828`,
         `provider="Liquid"`, `response_id`;
      c. each row of the spec Section 4.3.2 error table yields its `error` code: delayed
         response past `timeout_s`, closed port, HTTP 429 and 500, non-JSON body, missing
         `answers`, missing `answers.decision`, `type="refusal"`, `choice` outside the
         criteria keys;
      d. the API key does not appear in the recorded request row.
  12. `RandomGate` with fixed `(random_wakes, random_step_horizon, random_seed, pair key)`
      yields the same wake indices across runs (golden list); suppressed wakes move to the
      next eligible step; leftover count equals `random_unused`.
  13. `GateState` JSON built by `build_gate_state` from the signals fixture of item 4 for a
      2-worker team is ≤ 3 KB and contains no raw tool output.
- Verification: common commands;
  `uv run pytest -q tests/test_structural_signals.py tests/test_system1_gate.py`. No test
  makes a network call outside `localhost`.

### PR 3: agent-mode coordinator core

- Files: `src/cooperagents/agent_coordinator.py` (new), `tests/test_agent_coordinator.py`
  (new), `tests/fixtures/agent_coordination/coordinator/*` (new), qualification JSON files.
  Prompt texts are imported from `agent_coordinator_prompts.py` (PR 1). No file of PR 2 is
  imported; `SignalTracker` and the gate are constructor arguments typed by PR 1.
- Scope: read-only tool layer, opening plan phase, gate and wake threads, bus delivery,
  conversation state, `spec_check`, `AgentCoordinatorPoller`, `composed_completion_gate`,
  `metrics()` producing the spec Section 8.1 block, trajectory events of Section 8.2.
- Acceptance:
  1. Read-only tools: each template runs in a `LocalEnv`; path and revision validation
     rejects the cases in spec Section 5.2 without executing a command (asserted with a
     recording fake environment).
  2. Opening plan: scripted completion produces a valid `OpeningPlan`; budget exhaustion and
     invalid JSON produce `outcome="failed"` with no exception.
  3. Wake: a scripted `WAKE` produces at most `wake_max_tool_calls` calls and at most 2 bus
     messages; the wake observation contains no trigger reason field (key-set assertion).
  4. Spec check: empty diff and conflict-marker cases reject with no model call; scripted
     `REJECT` then `ACCEPT`; raising completion returns `None` with `verdict="ERROR"`;
     the per-worker rejection cap holds.
  5. `AgentCoordinatorPoller.poll()` returns the composed `_TeammatePoller.poll()` text
     unchanged and never blocks on the gate (asserted with a gate stub that sleeps).
  6. Thread shutdown: `finish()` joins both threads within 10 s after `stop()`.
  7. The `gate_decision` trajectory event for a `source="model"` decision (from a gate stub
     returning a PR 1 `GateDecision`) carries the full `GateState` and the
     `probabilities`, `threshold` and `model` fields; the `coordinator_gate` request rows
     have `tool_calls=[]`; the metrics block reports `model_calls`, `fallbacks` and
     `p_wake` (spec Section 8.1).
- Verification: common commands; `uv run pytest -q tests/test_agent_coordinator.py`.

### PR 4: harness and bench wiring, completion-time percentiles

- Files: `src/cooperagents/harness.py`, `src/cooperagents/metrics.py`
  (`completion_time_summary`), `scripts/bench_compare.py`,
  `tests/test_agent_coordinator_harness.py` (new), `tests/test_metrics.py` (new) or an
  extension of `tests/test_bench_budget.py`, `docs/CONFIGS.md` (flag table),
  qualification JSON files (both, since `harness.py` is in `producer_identity`).
  `src/cooperagents/agent_coordinator_prompts.py` only if the flash smoke requires a prompt
  text change (Section 3).
- Scope, wiring:
  1. `UnifiedHarness.__init__`: new keyword arguments of spec Section 5.1.
  2. `UnifiedHarness.run`: remove the PR 1 `NotImplementedError`; add the agent-family
     validation; add `and not spec.agent_coordinator` to the guard at `harness.py:3408-3421`;
     reject `coordinator_complete`/`coordinator_completion` with `spec.agent_coordinator`.
  3. `_run_isolated`: create `env_factory("coordinator")` with the worker environments;
     start `GitShareSync(observe=True)`; run the opening plan before worker threads; start
     coordinator threads; in `run_coop`, pass `AgentCoordinatorPoller` and the composed
     completion gate; merge `metrics["agent_coordinator"]`; register cleanup through the
     existing `register_cleanup`.
  4. `bench_compare.py` arguments: flags of spec Section 5.1 in the `coordinator_family`
     group, including the decision-model flags of spec Section 4.3.3;
     `GateConfig.validate()` at argument parsing; validation mirroring the deterministic
     checks at `bench_compare.py:570-571`; `--agent-random-match` resolution per pair;
     `pair_start` trajectory fields.
- Scope, percentiles: `completion_time_summary` (spec Section 8.3); `bench_compare.py`
  summary lines after the existing pass-rate and feature lines (`bench_compare.py:741-743`)
  and `logs/<run-name>/summary.json`.
- Acceptance, wiring:
  1. Non-protected integration test with `LocalEnv`, two scripted workers, scripted
     coordinator and gate, and the real PR 2 `SignalTracker` and gate classes: one opening
     plan, one wake, one spec rejection, final submission, metrics block valid against the
     PR 1 contract.
  2. `bench_compare.py` argument errors: `--agent-gate model` with `OPENROUTER_API_KEY`
     unset, or with `--agent-gate-base-url http://example.com`;
     `--agent-gate rule` or `random` with `--agent-gate-model`; `--agent-gate-threshold 1.5`.
     Each exits nonzero before any container starts.
  3. Existing families: `uv run pytest tests/protected -q` and
     `uv run pytest -q tests/test_coordinator.py tests/test_implementation_coordinator.py tests/test_harness.py`
     pass with no test edits.
  4. `git diff 829a1702 -- src/cooperagents/harness.py` shows no changed lines inside
     `class _Coordinator`, `class _DeterministicCoordinator`, `class _TeammatePoller`
     (reviewer checks hunk ranges against those class spans).
- Acceptance, percentiles:
  5. `completion_time_summary` matches `statistics.quantiles(n=100, method="inclusive")` at
     indices 49, 89, 94, 98 for n ≥ 2; n = 1 and n = 0 cases per spec Section 8.3.
  6. Existing printed summary lines are byte-identical for a fixture run directory of an
     existing arm (`det_main` or `plan_main`); new lines follow them.
  7. `summary.json` contains `n`, pass counts, and the percentile fields per arm, and the
     agent-mode totals of spec Section 8.3 for agent-mode runs.
- Acceptance, live smoke:
  8. Live smoke on CooperBench flash, Azure GPT-5.5, Docker, 10 fixed pairs
     (`scripts/measure.sh` pair list), arms: `det_main` flags;
     `agent_main` (`--agent-coordinator --agent-gate model`, gate defaults
     `liquid/d1.2-ultra` at `https://openrouter.ai/api/alpha`, key in `OPENROUTER_API_KEY`);
     `agent_rule_main` (`--agent-coordinator --agent-gate rule`);
     `agent_rand_main` (`--agent-coordinator --agent-gate random --agent-random-match <agent_main run>`).
     Report pass rate, completion time p50/p90/p95/p99, coordinator tokens, gate input
     tokens, completion tokens (0 expected) and summed `usage.cost` against
     $0.042 per 1M input tokens, gate latency p50/p95 (OpenRouter median 0.22 s),
     fallback count by `error`, `p_wake` distribution, probe latency p50/p95, wakes per pair.
     The wake budget (8 per run, 4 per worker) and `wake_threshold` are revisited from
     these numbers. Acceptance is completion without infrastructure errors and probe
     p95 ≤ 5% of median worker step time. Pass-rate deltas are reported and have no
     acceptance threshold.
- Verification: common commands, plus:

  ```sh
  uv run pytest -q tests/test_agent_coordinator_harness.py tests/test_metrics.py
  uv run python scripts/bench_compare.py --help   # lists every existing flag and the new agent flags
  uv run python scripts/bench_compare.py --team-only --no-seed --coop-tools --git-share \
    --agent-coordinator --agent-gate rule --completion-gate --presub-merge \
    --limit 1 --skip-eval --record-trajectory --team-name agent-smoke
  ```

### PR 5: protected macro suite, family `agent`

- Files: `tests/protected/test_agent_coordinator_macro.py` only.
- Scope: scenarios A1–A12 of spec Section 7.3. A12 covers the decision-model threshold,
  Option A routing of heartbeat candidates, fallback and `tool_calls=[]` rows, using a
  scripted `DecisionComplete` (no network).
- Acceptance:
  1. `python3 scripts/check_protected_tests.py --base <PR 4 merge commit> --allow-protected agent`
     exits 0.
  2. `python3 scripts/check_protected_tests.py --base <PR 4 merge commit>` (no allowance)
     reports `Protected change: tests/protected/test_agent_coordinator_macro.py (family: agent)`,
     confirming the family name.
  3. Suite runtime under 60 s; no `skip`/`skipif`/`xfail` markers.
  4. Explicit user approval before merge (CLAUDE.md protected-test rule).
- Verification: `uv run pytest -q tests/protected/test_agent_coordinator_macro.py`; common
  commands with the allowance.

## 5. Conflict hotspots

| File | PRs | Type | Mitigation |
| --- | --- | --- | --- |
| `src/cooperagents/reader_qualification.json` | 1–4 | Generated hash map; every source PR changes it, including the parallel PR 2 and PR 3 | Merge serially; the second of PR 2 and PR 3 rebases and reruns `qualify_direct_capture.py` (when needed) then `qualify_published_reader.py`; never hand-merge |
| `src/cooperagents/direct_capture_qualification.json` | 1, 4 (PRs changing a file in `producer_identity`) | Generated | Same as above; `qualify_direct_capture.py` preserves `admitted` |
| `src/cooperagents/agent_coordinator_prompts.py` | 1 (4 only for smoke-driven text changes) | Prompt constants | Created complete in PR 1; PR 2 and PR 3 only import it |
| `scripts/bench_compare.py` | 4 | Argument parsing, `run_team` and summary block | Single PR |
| `src/cooperagents/harness.py` | 1, 4 | PR 1 adds one guard line in `run`; PR 4 removes it and adds the wiring | PR 4 starts after PR 1 is merged |
| `src/cooperagents/types.py` | 1 | `TeamSpec` field and `__post_init__` | Frozen after PR 1 |
| `tests/fixtures/agent_coordination/` | 1–3 | Separate subdirectories per PR (`contract/` in PR 1; `signals/` and `gate/` in PR 2; `coordinator/` in PR 3) | No shared files |

Apart from the generated qualification files, PR 2 and PR 3 share no file.

## 6. Codex hand-off notes

- Each Codex task receives: the spec, this plan, its PR section, the frozen PR 1 types and
  prompts files, and the hard constraints of spec Section 2.
- Codex must stop and report when an acceptance item requires editing a protected file,
  `src/cooperagents/vendor/`, `src/cooperagents/workers/mini_swe_worker.py`, the class
  bodies listed in spec Section 7.1, or a PR 1 frozen file outside PR 4's prompt-text
  exception.
- polar-ext changes (spec Section 10) are a separate PR in the polar-ext repository, started
  after PR 4 merges and its new cooperagents pin is fixed. It is not counted in the 5 PRs.
  It adds `GATE_MODEL = "liquid/d1.2-ultra"`, `GATE_BASE_URL = "https://openrouter.ai/api/alpha"`,
  `GATE_THRESHOLD = 0.5`, the gate price entry ($0.042 per 1M input, $0.00 per 1M output),
  per-actor model identity and the `coordinator_gate` audit rules.
- Codex tests never call a real decision-model endpoint. `OPENROUTER_API_KEY` (or the
  variable named by `--agent-gate-api-key-env`) is read only in the live smoke of PR 4.
- Upstream merge: after the study on `cameron-chen/goal-agentic-coordinator` completes, open
  one PR from that branch into `cooperagents-coordinator-training`, titled
  `feat: merge agent-mode coordinator`. Before opening it, rerun the common verification
  with `--base origin/cooperagents-coordinator-training` and regenerate the qualification
  files on the merged tree if `cooperagents-coordinator-training` moved.
