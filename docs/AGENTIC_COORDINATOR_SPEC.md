# Method 2.1 Specification: Event-Driven Agent-Mode Coordinator

Status: Revision 3 for user review. Includes the user decisions of 2026-11-22 (Section 12),
the decision-model design of the System-1 gate (Section 4) and the OpenRouter Decisions
API contract for `liquid/d1.2-ultra` (Section 4.3.2). No implementation starts
before approval.
Base: `origin/cooperagents-coordinator-training` at `829a1702518a9a5864c5452d243c4464d52c4d4f`.
Implementation PR target: `cameron-chen/goal-agentic-coordinator`. After the experiments,
that branch is merged back to `cooperagents-coordinator-training`.
Companion document: `docs/AGENTIC_COORDINATOR_PR_PLAN.md` (PR split, merge order, verification).

## 0. Summary

Method 2.1 adds a third coordinator family, the agent-mode coordinator, next to the two
existing families (`spec.coordinator`, the tool-call coordinator `_Coordinator`; and
`spec.deterministic_coordinator`, `_DeterministicCoordinator`). It has four parts:

| Part | Function | Cost per decision |
| --- | --- | --- |
| Tier 1: structural signals | Derive progress, churn, overlap and submission events from exit codes, `git diff` fingerprints and Git snapshot hunks | $0 (one `env.execute` per worker step, no model call) |
| Tier 2: System-1 gate | Map compact execution state to `WAKE` or `SKIP`, with a structured reason. The default policy sends the state to an independent decision model, which returns a probability for each option in one call; `WAKE` iff `p_wake >= wake_threshold` | Input tokens of one decision-model call, zero completion tokens (model policy); $0 (rule and random policies) |
| Agent-mode coordinator (System 2) | Static opening plan from read-only inspection of the base checkout; bounded tool-using interventions on `WAKE`; frozen pre-submit spec check | One model call sequence per wake |
| Random-trigger ablation | Replace the System-1 gate with seeded random wake times at a matched wake count | Same as System 2 |

The worker loop stays the vendored mini-swe `DefaultAgent`, configured exactly as in the
deterministic-coordinator arm (`det_main`). All coordination reaches the worker through three
existing channels: bus messages (`BusComm`), the per-step `team_poller` hook, and the
`completion_gate` callable. `src/cooperagents/vendor/mini_swe/` and
`src/cooperagents/workers/mini_swe_worker.py` need no changes.

### 0.1 Terminology

- "Gate" is kept in two names because they already exist in the code or the method
  name: `completion_gate` (existing callable in `MiniSweEnvAdapter`) and "System-1 gate"
  (the method name in the request). Elsewhere this document uses "check" or "test".
- "Decision model": a model served behind an API that takes a state text and a fixed list
  of options per question and returns a probability distribution over those options in a
  single call, with no generated text, chain of thought or tool-call JSON. Examples are the
  models listed on the Artificial Analysis Decision Index
  (`https://artificialanalysis.ai/evaluations/artificial-analysis-decision-index`). The
  decision model is configured independently of the worker model and of the System-2
  coordinator model.
- "Wake": one invocation of the System-2 coordinator model for one intervention.
- "Candidate": one point at which the System-1 gate is asked for a decision.
- "Executed step": one worker bash action whose result has `exception_info` other than
  `"action was not executed"`, matching the existing filter in `_Coordinator._recent_actions`.

## 1. Motivation and evidence

### 1.1 3-group study (polar-ext, 2026-11-22)

Source: `polar-ext/goal-cooperbench-coordination/artifacts/2026-11-22-final-3group-summary.md`.
100 pairs × 2 rollouts, model `Inkling-2-Small`.

| Group | Merged (B) pass | Official A pass | Total cost | Mean generation time | Messages per pair |
| --- | --- | --- | --- | --- | --- |
| Solo | 67.0% | 67.0% | $23.86 | (not reported) | (not applicable) |
| Group 1 `baseline` | 23.5% | 23.5% | $28.48 | 434.7 s | 4.17 |
| Group 2 `det_main` | 63.5% | 52.5% | $54.16 | 969.7 s | 2.91 |
| Group 3 `plan_main` | 69.0% | 55.5% | $147.26 | 1522.8 s | 122.53 |

Observations used by this specification:

1. `plan_main − det_main` = +5.5 pp merged pass (pair CI `[+1.0, +10.5]`) at 2.72× cost
   and 1.57× mean generation time. The coordinator actor alone cost $39.88.
2. `det_main` interventions are few (2.91 messages per pair) and come from string-pattern
   triggers (`LOOP`, `STALL`, `COLLISION`) evaluated every 20 s.
3. Neither arm attributes outcome changes to intervention timing. The study has no arm that
   holds the intervention count fixed and varies only when interventions happen.

Method 2.1 targets the region between the two arms: a planning step with no worker-side
discussion cost, interventions selected by grounded signals, and an ablation that isolates
the contribution of the selection policy.

### 1.2 Limits of the existing triggers

| Trigger | Location | Definition | Issue |
| --- | --- | --- | --- |
| `LOOP` (tool-call) | `harness.py:1257` `_Coordinator._add_warnings` | ≥4 identical `(command, returncode, sha256(output))` among the last 6 actions | Misses loops with varying commands or outputs; ignores whether the tree changed |
| `STALL` (tool-call) | same | Last 4 outputs share the first 120 characters and the last one contains `"rror"` or has a nonzero exit | Output-prefix matching; progress is read from output text and the tree state is not checked |
| `LOOP` (deterministic) | `harness.py:1650` `_DeterministicCoordinator._behavior` | ≥4 of the last 6 commands share their first two whitespace tokens | Fires on normal repeated inspection (`sed -n`, `grep -n`) |
| `STALL` (deterministic) | same | 4 identical `(output[:120], error flag)` pairs | Same as tool-call `STALL` |
| `COLLISION` (deterministic) | `harness.py:1665` `_dirty_files` | Any shared dirty path between two workers | File-level only; fires on disjoint edits to one file |

Tier 1 replaces these triggers inside the new family only. The existing functions stay
byte-identical (Section 7).

## 2. Hard constraints and invariants

| ID | Constraint | Source | How Method 2.1 satisfies it |
| --- | --- | --- | --- |
| C1 | Every agent runs in its own container | `CLAUDE.md`, `harness.py` module docstring | The coordinator receives its own environment from `env_factory("coordinator")`. Workers keep one environment each. No component writes into another component's environment. |
| C2 | Worker agent held constant as mini-swe | `CLAUDE.md` | Worker construction is identical to the `det_main` path: `run_mini_swe_agent(..., monitor=None, deterministic_monitor=None)`. System prompt and tool list do not change. |
| C3 | Never edit `CooperBench/` | `CLAUDE.md` | Not touched. |
| C4 | Protected macro tests untouched | `CLAUDE.md`, `scripts/check_protected_tests.py` | `tests/protected/test_tool_call_coordinator_macro.py`, `tests/protected/test_deterministic_coordinator_macro.py` and `scripts/check_protected_tests.py` are not edited in any PR. New protected family `agent` is added in its own test-only PR (Section 7.3). |
| C5 | Existing coordinator families keep their behavior | Protected tests | `_Coordinator`, `_DeterministicCoordinator`, `_TeammatePoller`, `GitShareSync` behavior and `_add_warnings` labels stay unchanged. The tool-call macro test asserts the label set `{"LOOP", "STALL", "POTENTIAL_CONFLICT"}` (`test_tool_call_coordinator_macro.py:352`). |
| C6 | Attribution with the agent held constant | `CLAUDE.md` Guiding principle | The ablation arm (Section 6) uses the same worker, coordinator model, prompts, observation builder and wake count. Only the trigger policy differs. |
| C7 | Source identity qualification | `published_profiles.validate_reader`, `validate_direct_writer` | Every PR that changes `src/cooperagents/**/*.py` regenerates both qualification files (Section 9). |

## 3. Tier 1: structural signals

New module: `src/cooperagents/structural_signals.py`. Pure functions over records; no model
calls, no threads, no I/O except `DiffProbe.run(env)`.

### 3.1 Data collection

#### 3.1.1 Hook point

The vendored `DefaultAgent.step` calls `self.team_poller.poll()` at the start of every step,
after the previous step's tool results are in `agent.messages`
(`vendor/mini_swe/agents/default.py:249-253`). The poll runs on the worker's own thread, so
a command issued from `poll()` cannot interleave with the worker's own commands. The
existing `_DeterministicCoordinator._dirty_files` needs a message-marker check to avoid
reading a tree mid-command (`harness.py:1669-1680`); the poll hook removes that race.

The agent-mode family passes an `AgentCoordinatorPoller` as `poller=` to `run_on_shared`
(`harness.py:2200`). It composes the existing `_TeammatePoller` for Git branch notices
(`watch_gitshare`) and adds signal collection. Per call it:

1. Parses new worker messages since the last poll into `StepRecord`s.
2. Runs one `DiffProbe` in the worker's environment.
3. Feeds the records to the worker's `SignalTracker`; any new `Signal` becomes a candidate
   on the coordinator's queue (non-blocking `queue.put_nowait`).
4. Returns `_TeammatePoller.poll()` text unchanged. Coordinator messages travel through the
   bus (Section 5.4), so `poll()` adds no new text to the worker context.

#### 3.1.2 `DiffProbe`

One shell command executed in the worker's environment with `timeout=signals.probe_timeout_s`:

```sh
git -c core.quotepath=false diff --no-ext-diff --no-color --no-renames -U0 HEAD -- . \
  ':(exclude).cb_*' ':(exclude)patch.txt' | grep -E '^(diff --git|@@ )'
echo '--FP--'
{ git diff --no-ext-diff --no-color --binary HEAD -- . ':(exclude).cb_*' ':(exclude)patch.txt'
  git ls-files -o --exclude-standard -z -- . ':(exclude).cb_*' | xargs -0 -r git hash-object -- ; } \
  | sha256sum | cut -c1-64
echo '--NUM--'
git diff --no-ext-diff --numstat HEAD -- . ':(exclude).cb_*' ':(exclude)patch.txt' \
  | awk '{a+=$1; d+=$2} END {print a+0, d+0}'
```

Exclusions match the existing conventions (`.cb_` prefix and `patch.txt` in
`git_snapshots._raw_changes` and `_Coordinator._dirty_files`). Hunk headers are parsed with
the existing `git_snapshots._regions` (base-side `(start, count)`), reused by import.

The probe sees untracked files through `git hash-object` so a new-file-only worker still
changes the fingerprint. The tracked diff is bounded by the output limit of the environment;
if `"[output truncated after "` appears (the existing truncation marker checked in
`git_snapshots._query`), the probe records `error="truncated"` and `fingerprint=None`.

Latency: one container exec per worker step. Worker steps include a model call of several
seconds, so the target is probe p95 ≤ 5% of median worker step time. The probe records
`latency_ms`; the metrics block reports p50/p95 (Section 8). Acceptance test in PR 4 records
the measured overhead on the 10-pair flash set.

#### 3.1.3 `StepRecord` parsing

A new parser extracts executed bash actions with their exit codes. It follows the same
assistant/tool pairing as `_Coordinator._recent_actions` (`harness.py:1178`), with two
differences: it keeps the full window required by Tier 1 (up to `signals.history_steps`,
default 24) and it keeps `returncode` and `sha256(raw_output)` without clipping. The
existing classmethod is not reused because its six-record window and clipped output form
part of the protected tool-call observation.

Test-command classification (`is_test`): the command, after removing leading
`cd <dir> &&` segments and `VAR=value` assignments, starts with one of: `pytest`,
`python -m pytest`, `python3 -m pytest`, `python -m unittest`, `tox`, `nox`, `go test`,
`cargo test`, `npm test`, `npm run test`, `yarn test`, `pnpm test`, `npx jest`, `jest`,
`vitest`, `mvn test`, `gradle test`, `./gradlew test`, `make test`, `ctest`, `rspec`,
`bundle exec rspec`, `phpunit`, `dotnet test`, or runs a file under `.cb_checks/`.
The classifier reads only the command. The pass/fail outcome comes from the exit code.
The prefix list is a `SignalConfig` field so it can be extended without code changes.

### 3.2 Signal definitions

Notation: `R` is the worker's list of executed `StepRecord`s in order; `fp(r)` is the
fingerprint after record `r`; `fp_before(r)` is the fingerprint before `r` (the previous
record's `fp`, or the probe at worker start).

| Kind | Fires at record `r_t` when | Severity | Payload |
| --- | --- | --- | --- |
| `NO_PROGRESS_FAILING` | Let `W` be the last `no_progress_window` (5) records ending at `r_t`. `fp(r_t) == fp_before(W[0])` (no net tree change across `W`), at least `no_progress_min_failures` (3) records in `W` have `returncode != 0`, and `r_t.returncode != 0`. All fingerprints in `W` are non-null. | `hard` when ≥1 failing record in `W` has `is_test`, else `warn` | `window_steps`, `failures`, `failing_test_commands` (≤3, clipped 160 chars), `fingerprint` |
| `HUNK_CHURN` | For one path `p`, at least `churn_min_steps` (3) records within the last `churn_window` (10) change the hunk set of `p`, each changed hunk set overlaps one common base region (overlap per `git_snapshots._overlapping_regions`), and no record between the first and last of them is a test command with `returncode == 0`. | `warn` | `path`, `region` (label from `_overlapping_regions`), `steps`, `last_test_pass_step` |
| `TEST_REGRESSION` | `r_t.is_test`, `r_t.returncode != 0`, and an earlier record with the same normalized command had `returncode == 0`. | `hard` | `command` (clipped), `passed_at_step`, `failed_at_step` |
| `PEER_OVERLAP` | A new `(pair, path, overlap_label)` entry appears in `GitEvidence.comparisons[pair].overlaps`, or a new `(pair, path)` appears in `RegionComparison.unknown`. Edge-triggered; each key fires once per run. | `hard` for region overlap, `info` for unknown regions | `pair`, `path`, `regions` or `"unknown"`, `source="snapshot"` |
| `SUBMIT_REJECTED` | The existing `completion_gate` returned an error at a worker's submission (Section 5.6). | `warn` on first, `hard` from second | `rejections`, `error_head` (first 500 chars) |
| `SUBMIT_ATTEMPT` | A worker emitted the submit command. Recorded for telemetry and the frozen pre-submit check; not a wake candidate. | `info` | `step` |
| `PEER_MESSAGE` | A worker sent a bus message to `coordinator`. | `info` | `sender`, `bytes` |
| `WORKER_FINISHED` | `run_mini_swe_agent` returned for a worker. | `info` | `status` |
| `PROBE_ERROR` | `DiffProbe` failed or timed out. Not a wake candidate. | `info` | `error` |

Normalized command for `TEST_REGRESSION`: whitespace collapsed, trailing `| tail ...`,
`| head ...`, `2>&1` removed.

`PEER_OVERLAP` uses the snapshots that `GitShareSync` already publishes every 45 s
(`git_snapshots.py:277-279`) and compares with `observe=True`. The agent-mode family sets
`observe=True`, as the tool-call family does today (`harness.py:2428`). Signal latency is
bounded by the 45 s publication cycle.

### 3.3 Debounce

| Rule | Default |
| --- | --- |
| Per `(worker, kind)` cooldown after a signal fires | `cooldown_steps` = 6 executed steps |
| `NO_PROGRESS_FAILING` re-arm | Only after the fingerprint changes or a test command exits 0 |
| `HUNK_CHURN` re-arm | Per `(worker, path, region)`; after a test command exits 0 or 10 steps without a change to the region |
| `PEER_OVERLAP` | Once per `(pair, path, label)` per run |
| Candidates per poll | At most one per worker; highest severity first, then table order |

### 3.4 Error tradeoffs

| Kind | False-positive case | False-negative case | Threshold direction |
| --- | --- | --- | --- |
| `NO_PROGRESS_FAILING` | Worker reproducing a failure before editing (deliberate red phase with no edits yet) | Loop that edits and reverts a file each step (the fingerprint changes each step); covered partly by `HUNK_CHURN` | Raising the window reduces false positives and delays detection by one step per unit |
| `HUNK_CHURN` | Iterative refinement of one function that converges without running tests | Churn spread across several regions | Requiring ≥3 steps keeps normal edit/fix pairs below threshold |
| `TEST_REGRESSION` | Flaky test; test command that changed meaning between runs (same text, different working directory) | Regression in a test command the worker never ran green | Exit-code based; no threshold |
| `PEER_OVERLAP` | Overlapping hunks that merge cleanly (adjacent insertions) | Semantic conflicts in disjoint regions (renamed symbol used elsewhere) | Region-level reduces the file-level false positives of `COLLISION` |
| `SUBMIT_REJECTED` | Build failure caused by the environment (no network) | None; event is exact | Not applicable |

The cost of a false positive is bounded by the wake budget (Section 4.4). The cost of a
false negative is a missed intervention, which also occurs in the random arm and in
`det_main`; the ablation (Section 6) measures the net effect.

### 3.5 Unit testability

All detectors are pure functions:

```python
def parse_steps(worker: str, messages: list[dict], *, start: int, config: SignalConfig) -> list[StepRecord]: ...
def parse_probe(stdout: str, *, latency_ms: float) -> DiffProbe: ...
class SignalTracker:
    def __init__(self, worker: str, config: SignalConfig) -> None: ...
    def observe(self, records: list[StepRecord]) -> list[Signal]: ...
def overlap_signals(evidence: GitEvidence, seen: set[tuple[str, str, str, str]]) -> list[Signal]: ...
```

Fixtures (`tests/fixtures/agent_coordination/`):

- `messages_*.json`: worker message lists in the vendored format (`extra.actions`,
  `extra.raw_output`, `extra.returncode`), built with the helper pattern from
  `tests/test_git_snapshot_warnings.py` (`history`, `observed`).
- `probe_*.txt`: probe stdout samples, including truncation and error cases.
- `evidence_*.json`: serialized `GitEvidence` values.
- One fixture derived from a recorded `det_main` trajectory (`trajectory.jsonl.gz`,
  `messages` events) to check signal firing rates on real command/exit sequences.
  Diff-based signals cannot be replayed from these trajectories because they carry no
  probe data; their calibration uses the PR 4 flash runs.

Each kind has positive, negative and boundary cases (window − 1, window, window + 1), plus
debounce and re-arm cases. Expected outputs are golden JSON files compared with `==`.

## 4. Tier 2: System-1 gate

New module: `src/cooperagents/system1_gate.py`.

### 4.1 Input: `GateState`

Built by the coordinator thread from a candidate. Canonical JSON, target size ≤ 3 KB.

```json
{
  "schema": "gate-state/1",
  "target": "agent1",
  "candidate_kind": "signal",
  "signals": [{"signal_id": "agent1:NO_PROGRESS_FAILING:37", "kind": "NO_PROGRESS_FAILING", "severity": "hard", "payload": {}}],
  "recent_steps": [{"step": 37, "command": "python -m pytest tests/test_x.py -q", "returncode": 1, "is_test": true, "diff_added": 0, "diff_deleted": 0}],
  "diff": {"files": 3, "added": 120, "deleted": 14, "unchanged_for_steps": 5},
  "since_last_intervention": {"steps": 22, "seconds": 410.0},
  "budget": {"remaining": 5, "target_remaining": 3, "steps_remaining": 963, "seconds_remaining": 2870.0},
  "peers": [{"id": "agent2", "status": "running", "steps": 41, "active_signals": []}]
}
```

`recent_steps` holds the last 6 executed records; commands are clipped to 160 characters.
No raw tool output enters the state.

`candidate_kind` is `signal` (a Tier 1 signal), `peer_message`, or `heartbeat`. A heartbeat
candidate is created for a worker after `heartbeat_steps` (15) executed steps without any
candidate. Heartbeats let the model policy wake on patterns that Tier 1 does not encode.
The rule policy always returns `SKIP` for heartbeats.

### 4.2 Output: `GateDecision`

PR 1 has not merged, so the schema keeps the version `gate-decision/1` and the new fields
are part of the first frozen version.

```json
{
  "schema": "gate-decision/1",
  "candidate_id": "c-0042",
  "candidate_kind": "signal",
  "decision": "WAKE",
  "reason_code": "NO_PROGRESS",
  "target": "agent1",
  "signal_ids": ["agent1:NO_PROGRESS_FAILING:37"],
  "source": "model",
  "probabilities": {"WAKE": 0.78, "SKIP": 0.22},
  "threshold": 0.5,
  "model": "liquid/d1.2-ultra",
  "latency_ms": 412.4,
  "input_tokens": 734,
  "completion_tokens": 0,
  "prompt_id": "gate-v1:3f2a...",
  "budget_remaining_after": 4
}
```

| Field | Type | Value by source |
| --- | --- | --- |
| `decision` | `"WAKE"` or `"SKIP"` | All sources |
| `reason_code` | enum below | Derived from the candidate (Section 4.2.1) for every source; `RANDOM_SCHEDULE` for `random`; `NONE` for `budget` |
| `source` | `rule`, `model`, `fallback`, `random`, `budget` | `fallback`: the decision-model call failed and the `RuleGate` result is used; `budget`: forced `SKIP` because a constraint of Section 4.4 failed |
| `probabilities` | `dict[str, float]` or `null` | Decision-model result normalized over `["WAKE", "SKIP"]` for `model`; `null` for the other sources |
| `threshold` | `float` or `null` | `wake_threshold` for `model` and `fallback`; `null` otherwise |
| `model` | `str` or `null` | Configured decision-model ID for `model` and `fallback`; `null` otherwise |
| `input_tokens`, `completion_tokens` | `int` or `null` | Usage reported by the endpoint. Mapped from `usage.input_tokens` and `usage.output_tokens` (Section 4.3.2); `completion_tokens` is 0 for `liquid/d1.2-ultra`. `null` when no call was made |
| `latency_ms` | `float` | Wall time of the policy decision, including the call |
| `error` | `str` or absent | Present only for `fallback`: `timeout`, `transport`, `http_<status>` or `invalid_response` |

`reason_code` enum: `NO_PROGRESS`, `HUNK_CHURN`, `TEST_REGRESSION`, `PEER_OVERLAP`,
`SUBMIT_REJECTED`, `PEER_MESSAGE`, `HEARTBEAT_PATTERN`, `RANDOM_SCHEDULE`, `NONE`.
The `note` field of revision 1 is removed: no policy produces free text.

#### 4.2.1 Source of `reason_code`

`reason_code` is derived deterministically from the Tier 1 candidate. The decision model
answers one question, `decision` over `["WAKE", "SKIP"]`.

| Candidate | `reason_code` |
| --- | --- |
| `signal` with one signal | Fixed map: `NO_PROGRESS_FAILING`→`NO_PROGRESS`, `HUNK_CHURN`→`HUNK_CHURN`, `TEST_REGRESSION`→`TEST_REGRESSION`, `PEER_OVERLAP`→`PEER_OVERLAP`, `SUBMIT_REJECTED`→`SUBMIT_REJECTED` |
| `signal` with several signals | The signal with the highest severity; ties resolved by the order of the map row above. All signal IDs stay in `signal_ids` |
| `peer_message` | `PEER_MESSAGE` |
| `heartbeat` | `HEARTBEAT_PATTERN` |

Reasons for this choice:

1. `reason_code` is telemetry. The wake observation omits the trigger reason in every arm
   (Section 5.4, required by Section 6), so no component consumes a model-selected reason.
2. A second question would add a second probability output with no outcome label to
   calibrate it against, and the two outputs could disagree (for example `SKIP` with a
   reason other than `NONE`).
3. For `heartbeat` candidates, a model-selected reason would describe a pattern that Tier 1
   does not encode. That pattern is recoverable offline from the recorded `GateState`.

`DecisionRequest` accepts a list of questions (Section 4.3.1), so a second question can be
added later without a contract change to the client interface. A second question changes
`gate-decision/1` and needs a contract PR.

### 4.3 Policies

| Policy | Flag value | Arm | Decision procedure | Cost |
| --- | --- | --- | --- | --- |
| `DecisionGate` | `model` (default) | `agent_main` | Constraints of Section 4.4 first. Every candidate that passes them (`signal`, `peer_message`, `heartbeat`) is sent to the decision model in one call. `WAKE` iff `probabilities["WAKE"] >= wake_threshold`. On failure, the `RuleGate` result with `source="fallback"` | Input tokens of one call |
| `RuleGate` | `rule` | `agent_rule_main` | Constraints first; then `WAKE` if the candidate carries a signal of severity `hard`, or a `HUNK_CHURN`, or a `PEER_MESSAGE`; else `SKIP`. Heartbeats always yield `SKIP` | $0 |
| `RandomGate` | `random` | `agent_rand_main` | Section 6 | $0 |

All three implement `decide(state: GateState, now: float) -> GateDecision`. In
`agent_main`, `RuleGate` runs only as the fallback inside `DecisionGate`; it never filters
candidates before the decision model.

#### 4.3.1 Client interface

```python
@dataclass(frozen=True)
class DecisionQuestion:
    id: str                     # "decision"
    instructions: str           # question text from agent_coordinator_prompts.py
    criteria: dict[str, str]    # option -> criterion text, e.g. {"WAKE": "...", "SKIP": "..."}
    # The option list is tuple(criteria); the question type is always "choice".

@dataclass(frozen=True)
class DecisionRequest:
    state_text: str             # gate prompt instructions + canonical GateState JSON
    questions: tuple[DecisionQuestion, ...]
    prompt_id: str              # covers state instructions, question instructions and criteria

@dataclass(frozen=True)
class DecisionResult:
    probabilities: dict[str, dict[str, float]]  # question id -> option -> probability
    chosen: dict[str, str]                      # question id -> option reported by the endpoint
    latency_ms: float
    input_tokens: int | None                    # usage.input_tokens
    completion_tokens: int | None               # usage.output_tokens
    cost_usd: float | None                      # usage.cost, as reported by the endpoint
    raw_model: str | None                       # response "model"
    provider: str | None                        # response "provider"
    response_id: str | None                     # response "id"

DecisionComplete = Callable[[DecisionRequest], DecisionResult]
```

The gate instructions, the `decision` question instructions and the `WAKE`/`SKIP`
criteria are constants in `src/cooperagents/agent_coordinator_prompts.py`. One
`prompt_id` (Section 4.6) is computed over all three texts, so a change to any criterion
yields a new `prompt_id`.

`agent_gate_complete` (Section 5.1) has type `DecisionComplete`. Tests inject scripted
functions with this type. `DecisionGate` validates every result before use:

- each question in the request has an entry; option keys equal the keys of `criteria`;
- each probability is finite and in `[0, 1]`; the sum is within `1e-3` of 1;
- the result is renormalized to sum to 1 and recorded.

A result that fails validation is `invalid_response` and triggers the fallback.
`DecisionGate` applies `wake_threshold` itself; `chosen` is recorded and does not decide.
The endpoint's `confidence` field equals the probability of `choice` and is not stored.

#### 4.3.2 OpenRouter Decisions API client

The default `DecisionComplete` is `HttpDecisionClient` in `system1_gate.py`, implementing
the OpenRouter Decisions API (`docs/guides/features/decisions` on openrouter.ai). It uses
`urllib.request` from the standard library.

Request: `POST <base_url>/decisions` with headers `Authorization: Bearer <key>` and
`Content-Type: application/json`. No other headers are sent.

```json
{
  "model": "liquid/d1.2-ultra",
  "state": "<gate prompt instructions + canonical GateState JSON>",
  "questions": {
    "decision": {
      "type": "choice",
      "instructions": "<question text>",
      "criteria": {
        "WAKE": "<criterion for WAKE>",
        "SKIP": "<criterion for SKIP>"
      }
    }
  }
}
```

`state` is `DecisionRequest.state_text`. `questions` maps each `DecisionQuestion.id` to
`{"type": "choice", "instructions", "criteria"}`.

Response (HTTP 200):

```json
{
  "id": "gen-dec-...",
  "model": "liquid/d1.2-ultra",
  "provider": "Liquid",
  "answers": {
    "decision": {
      "type": "choice",
      "choice": "WAKE",
      "probabilities": {"WAKE": 0.78, "SKIP": 0.22},
      "confidence": 0.78
    }
  },
  "usage": {"input_tokens": 734, "output_tokens": 0, "cost": 0.000030828}
}
```

| Response field | `DecisionResult` field | Handling |
| --- | --- | --- |
| `answers[<id>].probabilities` | `probabilities[<id>]` | Validated as in Section 4.3.1 |
| `answers[<id>].choice` | `chosen[<id>]` | Must be one of the criteria keys |
| `usage.input_tokens` | `input_tokens` | |
| `usage.output_tokens` | `completion_tokens` | 0 expected; a nonzero value is recorded as reported |
| `usage.cost` | `cost_usd` | Recorded for comparison with the polar-ext price computation |
| `model`, `provider`, `id` | `raw_model`, `provider`, `response_id` | Recorded |

Error mapping (each yields the `RuleGate` fallback of Section 4.5):

| Condition | `error` |
| --- | --- |
| No response within `timeout_s` | `timeout` |
| Connection or TLS failure | `transport` |
| HTTP status other than 200 | `http_<status>` |
| Body is not JSON; `answers` missing; `answers["decision"]` missing; `answers["decision"]["type"]` is `"refusal"` or any value other than `"choice"`; `choice` or `probabilities` invalid | `invalid_response` |

The GateState target size (≤ 3 KB, Section 4.1) plus the prompt texts is far below the
model's context window of 65,536 tokens, so no truncation is applied.

#### 4.3.3 Configuration and CLI validation

`GateConfig` fields for the decision model (PR 1 contract):

| Field | CLI flag (`scripts/bench_compare.py`) | Default | Validation |
| --- | --- | --- | --- |
| `policy` | `--agent-gate {model,rule,random}` | `model` | |
| `model` | `--agent-gate-model` | `liquid/d1.2-ultra` | Non-empty when `policy == "model"`. The worker model is never used as a default |
| `base_url` | `--agent-gate-base-url` | `https://openrouter.ai/api/alpha` | When `policy == "model"`: starts with `https://`, or with `http://localhost` / `http://127.0.0.1` (test stubs); no trailing `/` |
| `api_key_env` | `--agent-gate-api-key-env` | `OPENROUTER_API_KEY` | When `policy == "model"`, the named environment variable must be set and non-empty. The key value is never written to logs, metrics or trajectories |
| `wake_threshold` | `--agent-gate-threshold` | `0.5` | Float in `[0, 1]` |
| `timeout_s` | `--agent-gate-timeout` | `3.0` | Float in `(0, 10]` |

The defaults name the decision model chosen by the user (Section 12.1, Q7), so the
`agent_main` launch needs no gate flags. `OPENROUTER_API_KEY` is the variable name used in
OpenRouter documentation; a run that uses another OpenRouter key passes
`--agent-gate-api-key-env`.

Validation runs in `bench_compare` argument checks and again in `UnifiedHarness.run`
before any environment is created. The decision-model flags have argparse default `None`
and receive the defaults above only when `policy == "model"`. With `policy` `rule` or
`random`, passing any of `--agent-gate-model`, `--agent-gate-base-url`,
`--agent-gate-api-key-env`, `--agent-gate-threshold` or `--agent-gate-timeout` is an
argument error, and `GateConfig` holds `None` for those fields, so a launch record never
lists a decision model that the run did not call. Credentials of the worker endpoint
(`AZURE_*`, `OPENAI_*`) are not read by the gate client.

### 4.4 Shared constraints (`WakeBudget`)

Applied before any policy. A failed constraint yields `SKIP` with `source="budget"` and no
decision-model call.

| Constraint | Default |
| --- | --- |
| Run wake budget | `wake_budget` = 8 |
| Per-target wake cap | `per_worker_wake_cap` = 4 |
| Minimum executed steps of the target since its last wake | `min_steps_between_wakes` = 4 |
| Minimum seconds since any wake | `min_seconds_between_wakes` = 30 |
| Target status | Must be `running` |

The user approved `wake_budget` = 8 and `per_worker_wake_cap` = 4; both are revisable after
the PR 4 flash smoke. `det_main` sends at most 3 nudges per worker
(`_DeterministicCoordinator.run`, `self._sent[aid] >= 3`), so the per-worker cap of 4 is of
the same order. Budgets are reported in the metrics block.

### 4.5 Latency and fail-safe behavior

- Workers never wait on the gate. `AgentCoordinatorPoller.poll()` only enqueues candidates;
  its latency is the probe latency.
- Two daemon threads in the coordinator: the gate thread consumes candidates; the wake
  thread runs System-2 wakes. A long wake (up to `wake_time_limit_s`) does not delay gate
  decisions.
- Candidate coalescing: while a candidate for a target is queued, a newer candidate for the
  same target replaces it (latest state wins). A `WAKE` for a target that already has a
  queued wake merges its signal ids into the queued wake.
- Staleness: a `WAKE` older than 60 s when the wake thread picks it up is re-checked with a
  fresh `GateState` against the constraints only (no second decision-model call).
- `DecisionGate` client timeout is `timeout_s` = 3 s with zero retries. The OpenRouter
  page reports a median latency of 0.22 s for `liquid/d1.2-ultra`; the PR 4 smoke
  measures p50 and p95, and 3 s bounds the tail of the gate thread. On timeout, transport error, non-2xx status or
  `invalid_response`, the decision is the `RuleGate` decision with `source="fallback"`.
  The default on failure is therefore `SKIP` unless a `hard` signal or a `PEER_MESSAGE` is
  present.
- No cache. Revision 1 keyed a cache on a coarse summary of the state. With a decision
  model, a cached probability would be recorded for a state that the model did not score,
  which corrupts the per-decision calibration records of Section 4.6. Exact-match keys
  over the full `GateState` include elapsed seconds and step counts, so they do not
  repeat. Each call costs input tokens only, so the cache has no cost benefit to offset
  these issues.

### 4.6 Prompt and telemetry boundaries for later optimization

Objective and reward design are out of scope. The specification only fixes the interfaces
that a later prompt optimizer needs:

1. All prompts (gate, opening plan, wake, spec check) live in
   `src/cooperagents/agent_coordinator_prompts.py` as module constants. Each has a
   `prompt_id = "<name>-v<n>:<sha256(text)[:12]>"`, recorded on every decision and wake.
2. `AgentCoordinatorConfig.prompts: dict[str, str] | None` overrides any prompt by name for
   offline experiments. The CLI exposes no override flag in this method.
3. Every `GateDecision` is emitted with its full `GateState` (trajectory event
   `gate_decision`, actor `coordinator_gate`). Rows join to outcomes by
   `(run_id, candidate_id)`, and to evaluation results by `run_id` and pair.
4. The gate sees no trigger reason from previous wakes and no evaluation result.
5. Every `gate_decision` event with `source="model"` records `probabilities`, `threshold`
   and `model`. These records support three offline analyses without new inference:
   the wake count at any threshold `τ` (count of rows with `p_wake >= τ`, before budget
   constraints are re-applied); Brier score and reliability curves of `p_wake` against a
   wake-level outcome label (Section 6.3); and comparison of gate prompts by `prompt_id`.
   Rows with `decision="WAKE"` have the outcome after an intervention and rows with
   `decision="SKIP"` have the outcome without one, so calibration analysis reports the two
   groups separately. The choice of outcome label is outside this method (Section 11).

## 5. Agent-mode coordinator

New module: `src/cooperagents/agent_coordinator.py`, class `AgentCoordinator`.

### 5.1 Activation and isolation

| Item | Value |
| --- | --- |
| `TeamSpec` field | `agent_coordinator: bool = False` |
| `TeamSpec.__post_init__` | Raises `ValueError("Coordinator families are mutually exclusive")` when more than one of `coordinator`, `deterministic_coordinator`, `agent_coordinator` is true. The existing message text is kept so `test_coordinator_families_are_mutually_exclusive` still matches. |
| `UnifiedHarness.__init__` new keyword arguments | `agent_coordinator_config: AgentCoordinatorConfig | None = None`, `agent_coordinator_complete: Callable | None = None`, `agent_gate_complete: DecisionComplete | None = None` (Section 4.3.1) |
| CLI flag (`scripts/bench_compare.py`) | `--agent-coordinator`, added to the existing mutually exclusive group `coordinator_family` (`bench_compare.py:480`) |
| Sub-flags | `--agent-gate {model,rule,random}` (default `model`); decision-model flags `--agent-gate-model`, `--agent-gate-base-url`, `--agent-gate-api-key-env`, `--agent-gate-threshold`, `--agent-gate-timeout` (Section 4.3.3); `--agent-wake-budget`, `--agent-opening {draft,none}` (default `draft`), `--no-agent-spec-check`, `--agent-random-match <team-run-name>`, `--agent-random-seed` |
| Name choice | `--coordination-mode` already selects `direct`/`group-chat`/`group-chat-planning` for the tool-call family. A `--coordinator-mode agent` flag would differ from it by one word, so the family uses its own flag. |

Required team configuration (validated in `UnifiedHarness.run` before any environment is
created): `shared_workspace`, `coop_tools`, `worker == "mini_swe"`, `seed_prior == False`,
`git_share`, two or more assignments with unique IDs that do not use `coordinator`; no
`team_roles`, `adaptive`, `decompose`, `best_of_n > 1`, `contract_first`,
`allow_spawn_tool`, `task_board`, `claim_mode`, `wait_protocol`. Compatible with
`completion_gate`, `presub_merge` (through `select_integration`), `repair_integrator`,
`focused_repair`, `repair_attempts`, `persist_worker_patches`, `record_trajectory`; these
are the flags of the polar-ext system arms (`runner.py:protocol_arguments`).

The existing guard at `harness.py:3408-3421` rejects any run where
`self.coordinator_complete is not None and not spec.deterministic_coordinator` unless the
tool-call configuration holds. The agent family uses its own injection arguments
(`agent_coordinator_complete`, `agent_gate_complete`) and the guard condition gains
`and not spec.agent_coordinator`. Passing `coordinator_complete` or
`coordinator_completion` together with `spec.agent_coordinator` raises `ValueError`.

### 5.2 Environment and read-only tools

The coordinator's environment comes from `env_factory("coordinator")` and is created with
the worker environments in `_run_isolated` (before `GitShareSync` starts). In
`bench_compare.run_team`, this environment receives the same volumes as workers
(`/cbshared` for Git sharing) and no `/coordination` mount, since `coordinator_dir` is only
passed for worker IDs (`bench_compare.py:424`). It is never diffed, merged or submitted.

Coordinator tools (OpenAI function-tool schemas). Each tool maps to a fixed command template;
arguments are validated and passed through `shlex.quote`. The model never supplies a shell
string.

| Tool | Arguments | Command template | Limits |
| --- | --- | --- | --- |
| `list_dir` | `path` | `git ls-tree --name-only <rev> -- <path>/` | ≤ 400 entries |
| `read_file` | `path`, `start` (≥1), `end` | `git show <rev>:<path> \| sed -n '<start>,<end>p'` | ≤ 400 lines, ≤ 16000 characters |
| `search_code` | `pattern` (fixed string), `path_glob` | `git grep -n -F -I --max-count=20 -e <pattern> <rev> -- <glob>` | ≤ 200 lines |
| `git_show` | `rev`, `path` (optional) | `git show --stat <rev>` or `git diff <base> <rev> -- <path>` | `rev` restricted (below) |
| `send_message` | `recipient` (worker ID or `group`), `content` | Bus send (Section 5.4) | ≤ 1500 characters; ≤ 2 per wake |
| `submit_plan` | `OpeningPlan` JSON | Opening phase only | One per run |
| `spec_verdict` | `SpecVerdict` JSON | Spec check only | One per check |
| `end_wake` | `note` | Ends the wake | Required to end early |

Read access goes through Git objects, so the coordinator reads committed content and cannot
observe or alter a working tree. `<rev>` is one of: the base SHA (`GitEvidence.base_sha`,
or `git rev-parse HEAD` in the coordinator environment at start), or a worker snapshot SHA
from `GitEvidence.workers[aid].sha`. Snapshot reads use
`git --git-dir=/cbshared/repo.git`, as `GitShareSync._query` does. Paths are rejected when
absolute, when they contain `..`, a NUL byte or a newline, or when they start with `.cb_`.
Any other tool name returns an error result to the model and counts toward the call budget.

### 5.3 Static opening plan

Runs synchronously in `_run_isolated` after environments exist and before any worker
thread starts, at the same position as `coordinator.decide(initial=True)`
(`harness.py:2448-2453`). No worker exists yet, so the inspection has no race with worker
edits.

Input: each assignment's `task` text (the feature description), the roster, the base SHA.
Budget: `opening_max_tool_calls` = 16, `opening_time_limit_s` = 180. The phase ends with
`submit_plan`.

```json
{
  "schema": "opening-plan/1",
  "version": 1,
  "workers": {
    "agent1": {"owns": ["src/pkg/a.py: parse_x()"], "avoid": ["src/pkg/b.py: render()"], "interfaces": ["parse_x(s: str) -> X"], "notes": "..."},
    "agent2": {"owns": ["src/pkg/b.py: render()"], "avoid": [], "interfaces": [], "notes": "..."}
  },
  "shared_interfaces": [{"name": "X", "owner": "agent1", "definition": "dataclass X(a: int)", "location": "src/pkg/a.py"}],
  "risks": [{"path": "src/pkg/__init__.py", "reason": "both features export a symbol"}],
  "evidence": [{"tool": "read_file", "path": "src/pkg/a.py", "lines": "1-80"}]
}
```

Delivery: one bus message from `coordinator` to each worker, sent before worker threads
start. `BusComm.receive` runs at the start of each step (`default.py:234-245`), so the plan
is in the worker context at step 1 as `[Message from coordinator]: ...`. The message text
labels the plan as a proposal and asks the worker to verify it against the code and send
corrections to the teammate and to `coordinator`.

Failure handling: budget exhaustion, transport error or invalid plan JSON produces
`opening.outcome="failed"` and workers start without a plan. The run continues.

#### 5.3.1 Relation to collaborative peer planning

| Property | `plan_main` (`group-chat-planning`) | Agent-mode opening plan |
| --- | --- | --- |
| Who inspects | Workers, read-only phase, before edits | Coordinator, read-only Git objects, before workers start |
| Blocking | Workers wait for consensus and `finish_planning` | Non-blocking; workers start with a proposal |
| Peer discussion | Group chat, 122.53 messages per pair on average | Peer `send_message` stays available; amendments go to the teammate and the coordinator |
| Reconciliation | Coordinator notebook updates | `PEER_MESSAGE` candidate; the decision model scores it in `agent_main`; `RuleGate` (in `agent_rule_main` and as fallback) always wakes on it |
| Cost structure | Worker steps spent in planning plus coordinator calls | One bounded coordinator sequence; no worker steps |

The two approaches coexist. `--agent-opening none` disables the plan and gives an arm that
measures the opening plan's contribution with the event-driven part held fixed. A
blocking confirmation variant is excluded from this method because `plan_main` already
measures blocking peer planning.

### 5.4 Event-driven execution

Wake thread loop:

1. Take the next `WAKE` (after the staleness check of Section 4.5).
2. Build the wake observation: roster, opening plan, per-worker status, last 6 executed
   steps per worker (Section 3.1.3 records, commands clipped to 160 characters, output
   omitted), active Tier 1 signals for all workers, `GitEvidence.metadata()`, unread
   messages addressed to `coordinator` (`bus.receive("coordinator")`), and the
   coordinator's own previous messages. The observation does not contain the trigger
   reason, the candidate kind or the policy name (required for Section 6).
3. Run a tool loop with `wake_max_tool_calls` = 6 and `wake_time_limit_s` = 90. The model may
   inspect snapshots with the read-only tools and send at most 2 messages.
4. Emit `wake_end` with tool counts, messages sent and usage.

Message delivery uses `bus.send(sender="coordinator", to=<worker>, content=...)`, the same
path as peer messages. `BusComm.receive` traces `message_delivered` for each delivery, so
polar-ext's communication extraction counts coordinator messages without changes.
`recipient="group"` sends one copy to each worker. Delivery latency is at most one worker
step.

Conversation state: the coordinator keeps one message history across wakes, with the
opening plan as the first user message. Before each wake, tool outputs from previous wakes
are replaced by one line each (`<tool> <args> -> <n> chars`) so that the prompt grows by the
coordinator's messages only.

### 5.5 Completion function interface

The two tiers use two separate interfaces.

| Tier | Interface | Endpoint and model | Trajectory actor |
| --- | --- | --- | --- |
| System 2 (opening plan, wakes, spec check) | `AgentComplete` below | Coordinator model and credentials resolved as in `planner._default_planner_complete` | `coordinator` |
| System 1 (`DecisionGate`) | `DecisionComplete` (Section 4.3.1) | `GateConfig.model`, `base_url`, `api_key_env`; independent of the worker and System-2 endpoints | `coordinator_gate` |

```python
AgentComplete = Callable[[list[dict], list[dict]], list[dict]]
# (messages, tools) -> [{"name": str, "arguments": str}, ...]
```

The default `AgentComplete` calls `openai.OpenAI(...).chat.completions.create`, records
each request through `trajectory.record_call` under actor `coordinator`, and records
`usage.prompt_tokens`, `usage.prompt_tokens_details.cached_tokens` and
`usage.completion_tokens`.

The default `DecisionComplete` is the OpenRouter client of Section 4.3.2. Each call is
recorded through `trajectory.record_call` under actor `coordinator_gate` with: `model`,
`base_url` (scheme, host and path; query string removed), `prompt_id`, the request
`questions`, the response `id`, `provider`, `answers`, `input_tokens`,
`completion_tokens` (from `usage.output_tokens`), `cost_usd` (from `usage.cost`),
`latency_ms` and `tool_calls=[]`. The request body and `state_text` are
recorded as for other actors. The API key is never recorded.

Tests inject scripted functions through `agent_coordinator_complete` (`AgentComplete`)
and `agent_gate_complete` (`DecisionComplete`).

### 5.6 Frozen pre-submit spec check

Purpose: detect a submission that is incomplete against its feature description, or that
drops a teammate's change after the pre-submit merge, while the worker still has budget to
fix it.

Hook: `_run_isolated` passes `completion_gate=spec.completion_gate` into `run_on_shared`
(`harness.py:2249`). In the agent family it passes a composed callable instead:

```python
def composed(env) -> str | None:
    error = base(env) if base is not None else None   # existing check, e.g. verification.validate(merged=True)
    tracker.submit_attempt(aid, error)                 # SUBMIT_ATTEMPT / SUBMIT_REJECTED signals
    if error is not None:
        return error
    return coordinator.spec_check(aid, env)            # None = accept
```

`MiniSweEnvAdapter._check_finished` calls the gate only on a successful submit command and
caps total rejections at `gate_max_rejections` = 3 (`mini_swe_worker.py:134-155`). The
composed callable shares that cap. `spec_check` rejects at most
`spec_gate_max_rejections` = 1 time per worker; later calls return `None`.

`spec_check(aid, env)` steps:

1. Freeze: run `git diff --binary <base>` plus untracked files in the worker's environment
   (same base resolution as `collect_raw_diff`, `harness.py:2478`), pass it through
   `patching.strip_test_sections`, and record `frozen_diff_sha256`.
2. Structural checks ($0, return a rejection without a model call):
   empty diff (`empty_diff`); conflict markers `^<<<<<<< |^=======$|^>>>>>>> ` in added
   lines (`conflict_marker`).
3. Spec check: one coordinator call with the feature text of `aid`, the frozen diff
   (clipped to 12000 characters with a head/tail split) and the opening plan. When
   `presub_merge` is active the diff contains the teammates' merged work, so the call also
   receives the teammates' feature text and checks for `lost_teammate_change`. The model
   answers with `spec_verdict`.
4. `REJECT` returns text to the worker through the existing completion-gate path:
   `SPEC_CHECK_REJECTED` followed by at most 3 findings, each with `kind`, `feature_id`,
   a quote of at most 300 characters from the feature text, and an optional path.
5. Transport error, timeout (60 s) or invalid verdict returns `None` (accept) with
   `verdict="ERROR"`. Infrastructure failures never block a submission.

Finding kinds: `missing_requirement`, `api_mismatch`, `unrelated_edit`,
`lost_teammate_change`, `empty_diff`, `conflict_marker`.

With `--presub-merge`, the integration step selects the longest member patch
(`bench_compare.py:356-358`), and each member patch already contains the merged team work.
The spec check at worker submission therefore also checks the integrated tree, and the
repair tail (`run_repair_tail`) needs no change.

## 6. Random-trigger ablation at equal wake budget

### 6.1 Design

| Arm | Gate policy | Everything else |
| --- | --- | --- |
| `agent_main` | `model`: `DecisionGate` with the configured decision model, `RuleGate` as fallback | Same worker, coordinator model, prompts, observation builder, opening plan, spec check, budgets |
| `agent_rule_main` | `rule`: `RuleGate` ($0) | Same |
| `agent_rand_main` | `random`, wake count matched to `agent_main` | Same |

All three arms run in the first study. Primary contrast: `agent_main − agent_rand_main` on
matched pairs. Secondary contrast: `agent_main − agent_rule_main`, which measures the
decision model against the $0 rule on the same Tier 1 candidates; wake counts are not
matched in this contrast and are reported per arm. The two arms differ only in
when wakes occur. Tier 1 signals are computed in both arms and appear in both wake
observations; the trigger reason appears in neither.

### 6.2 Matched schedule

For pair `p` and rollout index `i`, the random arm reads the matched `agent_main` result
(`logs/<match-run>/team/<repo>/<task>/<features>/result.json`) and takes:

- `N = metrics.agent_coordinator.gate.wake` (realized wakes),
- `S = metrics.agent_coordinator.team_worker_steps` (total executed worker steps).

It draws `N` distinct indices uniformly from `[1, S]` with `random.Random(seed)` where
`seed = sha256(f"{random_seed}:{repo}:{task_id}:{features}:{i}")`. A team step counter
increments once per executed worker step across all workers. When the counter reaches a
drawn index, the gate issues `WAKE` for the worker whose step reached it, subject to the
constraints of Section 4.4. A wake suppressed by a constraint moves to the next eligible
step. If the run ends first, the remaining count is reported as `random_unused`.

Missing matched result: the run fails before environment creation with
`ValueError("random gate requires a matched agent_main result")`.

`bench_compare` resolves the match from `--agent-random-match <team-run-name>` per pair.
`UnifiedHarness` receives `N`, `S` and the seed through `GateConfig.random_wakes`,
`GateConfig.random_step_horizon` and `GateConfig.random_seed`, so the harness itself reads
no files.

### 6.3 Analysis

- Report realized wakes per arm. Matching holds when the paired difference in realized
  wakes has median 0; `random_unused` > 0 on a pair is reported.
- Use the polar-ext pair-clustered bootstrap (`polar_ext.tasks.cooperagents.study.analysis.bootstrap`)
  for pass-rate differences.
- Secondary: wake-level outcome labels (whether the target's next 10 steps contain a
  fingerprint change, a test exit 0, or a resolved overlap) per arm, from trajectory events.

## 7. Protected-suite boundary

### 7.1 Files that no PR edits

- `tests/protected/test_tool_call_coordinator_macro.py`
- `tests/protected/test_deterministic_coordinator_macro.py`
- `scripts/check_protected_tests.py`
- In `src/cooperagents/harness.py`: `_Coordinator`, `_DeterministicCoordinator`,
  `_TeammatePoller`, `_add_warnings`, `_recent_actions`. These classes and functions keep
  byte-identical bodies. Additions in `harness.py` are a new branch in `_run_isolated`, new
  keyword arguments with defaults, and one extra clause in the validation at
  `harness.py:3408-3421`.

### 7.2 Registration behavior of the checker

`scripts/check_protected_tests.py` derives the family from the file name
(`protected_family`): `tests/protected/test_agent_coordinator_macro.py` →
stem `test_agent_coordinator_macro` → remove `test_` → remove `_coordinator_macro` →
family `agent`. A new file is detected through `git ls-files --others` and
`git diff --name-status`. No list of protected files exists in the checker, so adding a
family needs no checker edit.

Consequences:

1. Implementation PRs run `python3 scripts/check_protected_tests.py --base <base>` with no
   allowance; any file under `tests/protected/` fails them.
2. The protected test PR runs `--allow-protected agent`. With an allowance, any changed path
   outside the family fails (`"Protected test allowance requires a test-only change"`), so
   that PR contains only `tests/protected/test_agent_coordinator_macro.py`.
3. `DEFAULT_BASE` is `origin/cameron-chen/coord-baselines`. Base SHA `829a1702` is on
   `origin/cooperagents-coordinator-training` and is not on `origin/master` (`89135a56`).
   Every invocation passes `--base` explicitly (Section 12, Q1).
4. Disabled-marker scan (`skip`, `skipif`, `xfail`) applies to every file in
   `tests/protected/`, including the new one.
5. Repository has no `.github/workflows`; "CI" is the local command list in the PR plan.

### 7.3 New protected family `agent`

File: `tests/protected/test_agent_coordinator_macro.py`. Style follows the existing macro
suites: `LocalEnv.clone`, `InMemoryBus`, `Trajectory` + `open_events`, `litellm.completion`
monkeypatched for workers, scripted `agent_coordinator_complete` (`AgentComplete`) and `agent_gate_complete` (`DecisionComplete`),
an `unexpected` function that fails on any unscripted call. Scenarios:

| ID | Scenario | Assertions |
| --- | --- | --- |
| A1 | Opening plan before worker start | Coordinator tool requests precede the first worker `request` event; every worker's first model request contains `[Message from coordinator]` with the plan; the coordinator environment's `git status --porcelain` is empty at the end |
| A2 | Read-only tool rejection | `read_file` with `../x`, an absolute path, and a `.cb_` path returns an error result; no command runs in any environment for those calls |
| A3 | `NO_PROGRESS_FAILING` wake | Five failing steps without tree change produce one `signal`, one `gate_decision` `WAKE` (scripted `p_wake=0.9`), one `wake_start`; the target receives the coordinator message at its next step; no second wake within the cooldown |
| A4 | `HUNK_CHURN` and `TEST_REGRESSION` | Each fires at its threshold and not one step earlier |
| A5 | `PEER_OVERLAP` | Overlapping snapshot hunks produce one signal per `(pair, path, label)` |
| A6 | Budget | With `wake_budget=2`, the third qualifying candidate yields `SKIP` with `source="budget"` |
| A7 | Random arm matching | With `random_wakes=2`, `random_step_horizon=S`, fixed seed: wakes occur at the drawn team-step indices; the wake observation JSON is identical in structure to the model arm and contains no trigger field |
| A8 | Frozen spec check | First submission gets `SPEC_CHECK_REJECTED`; second submission is accepted; `frozen_diff_sha256` matches the patch collected for that worker; a raising spec completion accepts with `verdict="ERROR"` |
| A9 | Worker held constant | Worker system prompt and tool names equal those of a deterministic-family worker in the same fixture (`bash`, `send_message`) |
| A10 | Family exclusivity and configuration | `agent_coordinator` with `coordinator` or `deterministic_coordinator` raises `ValueError`; incompatible options raise before `env_factory` is called; `GateConfig(policy="model")` with an empty `model`, a non-HTTPS `base_url` outside localhost, or an unset `api_key_env` variable raises before `env_factory` is called |
| A11 | Metrics schema | `result.metrics["agent_coordinator"]` validates against the contract and its counts equal the trajectory event counts |
| A12 | Decision-model threshold and fallback | With `wake_threshold=0.5`, a scripted `DecisionComplete` returning `p_wake=0.49` yields `SKIP` and `0.50` yields `WAKE`, both with `source="model"`, `probabilities`, `threshold` and `model` recorded and `completion_tokens=0`; a heartbeat candidate reaches the decision model; a raising or invalid (`p` sum 1.2) result yields the `RuleGate` decision with `source="fallback"`; a budget-blocked candidate makes no `DecisionComplete` call; the `coordinator_gate` request rows have `tool_calls=[]` |

Timing: `AgentCoordinatorConfig.tick_s` and the absence of the 20 s tick used by
`_Coordinator.run` keep the suite under 60 s. The tool-call suite waits 65 s per episode
because of that tick (`WAIT_SECONDS = 65`).

## 8. Metrics and telemetry

### 8.1 Per-pair metrics block

`RunResult.metrics["agent_coordinator"]`, written to `result.json` by
`eval/cooperbench.write_run_outputs` (`"metrics": result.metrics`). Key name differs from
`coordinator_events` so existing consumers of the tool-call and deterministic families do
not parse it.

```json
{
  "schema": "agent-coordinator-metrics/1",
  "config": {"gate": {"policy": "model", "model": "liquid/d1.2-ultra",
                      "base_url": "https://openrouter.ai/api/alpha", "wake_threshold": 0.5, "timeout_s": 3.0,
                      "wake_budget": 8, "per_worker_wake_cap": 4},
             "opening": "draft", "spec_check": true, "prompt_ids": {}},
  "opening": {"outcome": "delivered", "tool_calls": 11, "seconds": 74.2, "plan_sha256": "..."},
  "signals": {"by_kind": {"NO_PROGRESS_FAILING": 2}, "by_worker": {"agent1": 3}},
  "gate": {"candidates": 19, "wake": 5, "skip": 14, "by_reason": {}, "by_source": {},
           "by_candidate_kind": {"signal": 12, "peer_message": 2, "heartbeat": 5},
           "model_calls": 15, "fallbacks": {"timeout": 0, "transport": 0, "invalid_response": 0},
           "p_wake": {"n": 15, "mean": 0.41, "p50": 0.37, "p90": 0.82},
           "latency_ms": {"p50": 420.0, "p95": 980.0},
           "budget": 8, "budget_remaining": 3, "random_planned": null, "random_unused": null},
  "wakes": {"total": 5, "tool_calls": 17, "messages_sent": 6, "seconds": {"p50": 21.0, "p95": 48.0}},
  "spec_check": {"checked": 2, "accepted": 1, "rejected": 1, "errors": 0},
  "probe": {"calls": 412, "errors": 0, "latency_ms": {"p50": 61.0, "p95": 140.0}},
  "usage": {
    "coordinator": {"calls": 30, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0},
    "coordinator_gate": {"calls": 15, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "reported_cost_usd": 0.0}
  },
  "team_worker_steps": 412,
  "timeline": {"opening_end_s": 74.2, "first_worker_edit_s": {"agent1": 130.5}, "worker_end_s": {"agent1": 900.1}}
}
```

Cost in dollars is not computed in cooperagents. polar-ext computes cost from token counts
and its price snapshot (`LedgerEntry`, `PriceSnapshot` in `study/contract.py`); the block
supplies token counts per actor for that computation. Worker cost stays in
`AgentResult.cost`. `usage.coordinator_gate` counts decision-model calls only, with
`completion_tokens` 0 for `liquid/d1.2-ultra`; `prompt_tokens` is the sum of
`usage.input_tokens` and `reported_cost_usd` the sum of `usage.cost`. The gate model ID is
in `config.gate.model`, so polar-ext prices that actor with the gate model's input price
($0.042 per 1M input tokens) and can compare the result with `reported_cost_usd`.
`p_wake` percentiles use the method of Section 8.3 over `source="model"` decisions.

### 8.2 Trajectory events

Emitted through the existing `Trajectory.emit(actor, event, **data)` when
`--record-trajectory` is set.

| Actor | Events |
| --- | --- |
| `coordinator` | `agent_coordinator_start`, `opening_plan`, `wake_start`, `wake_end`, `spec_check`, `request`/response rows from `record_call` |
| `coordinator_gate` | `signal`; `gate_decision` with the full `GateState` and the `GateDecision` fields of Section 4.2 (`probabilities`, `threshold`, `model`, token counts); `request`/response rows for `DecisionGate` calls (Section 5.5), with `tool_calls=[]` |
| worker IDs | Unchanged |

### 8.3 Completion-time percentiles

`src/cooperagents/metrics.py` gains:

```python
def completion_time_summary(durations: list[float]) -> dict[str, float | int | None]:
    """n, mean, p50, p90, p95, p99 using statistics.quantiles(n=100, method="inclusive")."""
```

For `n == 1`, all percentiles equal the single value; for `n == 0`, all are `None`. With 10
pairs, p95 and p99 interpolate between the two largest values; the summary prints `n` next
to the percentiles.

`scripts/bench_compare.py` summary output (after the existing pass-rate and feature lines,
`bench_compare.py:741-743`) adds per arm: completion time p50/p90/p95/p99 from
`RunResult.duration_seconds`, total worker cost, and for agent-mode runs the totals of
wakes, skips, coordinator calls and tokens. It also writes `logs/<run-name>/summary.json`
with the same fields. Existing printed lines are kept unchanged.

## 9. Source-identity qualification

At `829a1702`, `reader_qualification.json["sources"]` equals the hash map of every `.py`
and `.yaml` file under `src/cooperagents/` plus `direct_capture_qualification.json`
(checked during this review). `tests/test_published_profiles.py:101` and `:110` call
`validate_reader()` on the real package, so any added or modified source file fails the
default test suite until the file is regenerated. `validate_direct_writer()` fails when an
existing file in `producer_identity` changes (for example `harness.py`, `types.py`).

Rule for every PR that changes `src/cooperagents/**/*.py`:

1. `python3 scripts/qualify_direct_capture.py` (only when a file listed in
   `producer_identity` changed; it preserves `admitted`).
2. `python3 scripts/qualify_published_reader.py` (always; it hashes the direct
   qualification file, so it runs second).
3. Both JSON files are committed in the same PR. On rebase, conflicts in these files are
   resolved by rerunning the two commands on the rebased tree, never by hand-merging.

The capture sources (`harness.py`, `workers/mini_swe_worker.py`,
`vendor/mini_swe/models/litellm_model.py`) define direct-capture identity. Only
`harness.py` among them changes in this method, in PR 1 (guard) and PR 4 (wiring). The reader qualification note
states that deployment qualification requires independent evidence; regenerating the files
records source identity only.

## 10. polar-ext compatibility

polar-ext pins a cooperagents source and launches `scripts/bench_compare.py`
(`polar_ext/recipes/cooperbench_coordination/runner.py`). Changes listed here are in the
polar-ext repository and are outside the cooperagents PR plan.

| Area | Current behavior | Required change |
| --- | --- | --- |
| Variant names | `VariantName` literal (`runner.py:35-48`) | Add `agent_smoke`, `agent_main`, `agent_rule_smoke`, `agent_rule_main`, `agent_rand_smoke`, `agent_rand_main` (and `cd_` versions if needed) with `.toml` files under `variants/` |
| System-arm detection | `is_system_arm`: prefix `det_` or `plan_` (`runner.py:104-105`) | Add `agent_` |
| Coordinator flags | `protocol_arguments` selects `--deterministic-coordinator` for `det_`, otherwise the planning coordinator (`runner.py:293-297`) | Explicit `agent_` branch: `--agent-coordinator --agent-gate model --agent-gate-model <GATE_MODEL> --agent-gate-base-url <GATE_BASE_URL> --agent-gate-api-key-env <var> --agent-gate-threshold <τ>` (all passed explicitly, values from the study constants); `--agent-coordinator --agent-gate rule`; or `--agent-coordinator --agent-gate random --agent-random-match <run>`. Prefix `agent_rule_` and `agent_rand_` are tested before `agent_`. Without the branch, `agent_` variants would receive the planning flags |
| Launch flag audit | `audit.py:980-1010` hard-codes expected flags for `det_smoke`/`plan_smoke` | Add expected flag lists for agent variants |
| Actor rules | Worker-lifetime check skips `coordinator` and `integrator*` (`audit.py:419`) | Also skip `coordinator_gate` |
| Coordinator tool allowlist | `{"send_message", "update_notebook", "finish_planning"}` (`audit.py:730-737`) | Agent variants: `{"list_dir", "read_file", "search_code", "git_show", "send_message", "submit_plan", "spec_verdict", "end_wake"}` for `coordinator`. For `coordinator_gate`: the allowlist is the empty set; every response row must have `tool_calls == []` and a `probabilities` object whose keys equal the requested options (`WAKE`, `SKIP`) and whose values sum to 1 within `1e-3`; `completion_tokens == 0` |
| Request model identity | Every request must use the frozen `MODEL` (`audit.py:617`) | Per-actor model identity: actor `coordinator_gate` must use the frozen `GATE_MODEL` at the frozen `GATE_BASE_URL` (scheme, host, path); every other actor keeps the `MODEL` check. `GATE_MODEL = "liquid/d1.2-ultra"`, `GATE_BASE_URL = "https://openrouter.ai/api/alpha"` and `GATE_THRESHOLD = 0.5` are study constants next to `MODEL` and are checked against the launch flags and `metrics.agent_coordinator.config.gate`. `agent_rule_` and `agent_rand_` variants must have no `coordinator_gate` request rows |
| Pricing | `PriceSnapshot` prices `MODEL` (`study/contract.py`) | Add a `GATE_MODEL` entry: input $0.042 per 1M tokens, output $0.00 per 1M tokens (OpenRouter page for `liquid/d1.2-ultra`, 2026-11-22). `completion_tokens == 0` is asserted per row. `LedgerEntry` for actor `coordinator_gate` uses that entry; a difference above 5% between the computed cost and the summed `usage.cost` is reported |
| Credentials | The runner passes worker endpoint variables to `bench_compare` | Also pass the variable named by `--agent-gate-api-key-env`; the audit checks that no recorded row or launch record contains its value |
| Containers and timing | Team arm reserves 2 container slots (`Variant.slots`); `timing.json` worker container identities must match the journal (`audit.py:431-476`) | The coordinator container is an extra container, as the integrator already is; timing audit excludes `coordinator` like `integrator*`; slot accounting adds one container |
| Source pin and pooling | README: pooling requires unchanged `src/` and `scripts/bench_compare.py` | Agent arms run on a new pin. Run a `det_main` control on the same pin; do not pool with the `829a1702` or earlier groups |
| Matched random arm | Scheduler admits rollouts in draw, rollout-index and arm order | `agent_rand_main` rollout `(pair, i)` starts after `agent_main` `(pair, i)` is terminal; it passes the `agent_main` run name to `--agent-random-match` |
| Metrics consumption | `result.json` `metrics` | Read `metrics.agent_coordinator` (schema `agent-coordinator-metrics/1`); usage per actor feeds `LedgerEntry` |
| Artifacts | `ArtifactPaths` (`study/contract.py`) | No new required paths; trajectories stay in `trajectory.jsonl.gz` |

Each item has a defined change on the polar-ext side, and the constant values are fixed
by Q7 and Q8 (Section 12.1).

## 11. Excluded from this method

- Changes to the tool-call or deterministic families, including replacing their `LOOP`/`STALL`
  triggers.
- Edits to the vendored mini-swe agent or to `mini_swe_worker.py`.
- A shared notebook for the agent family.
- Reward or objective design for gate optimization; prompt override CLI flags.
- Learned thresholds; thresholds are configuration values set from PR 4 measurements.
- Training or fine-tuning a decision model; the decision model is an external endpoint.
- Outcome labels and objectives for calibrating `p_wake` (Section 4.6 records the inputs).
- A second decision-model question for `reason_code` (Section 4.2.1).
- A blocking peer-confirmation opening phase.
- `src/cooperagents/cli.py` flags (the CLI has no coordinator flags today; studies use
  `scripts/bench_compare.py`).

## 12. Decisions and open questions

### 12.1 Resolved decisions (user, 2026-11-22)

| ID | Decision | Sections |
| --- | --- | --- |
| Q1 | Base: `origin/cooperagents-coordinator-training` at `829a1702`. Implementation PRs target `cameron-chen/goal-agentic-coordinator`. After the experiments, that branch is merged back to `cooperagents-coordinator-training` | Header, 7.2, PR plan Sections 1 and 4 |
| Q2 | `--agent-gate` defaults to `model` (`agent_main`). Option A: `WakeBudget` constraints are checked first; the decision model then scores every qualifying candidate (`signal`, `peer_message`, `heartbeat`). `RuleGate` runs only as the timeout and error fallback in `agent_main` | 4.3, 4.4 |
| Q3 | `wake_budget` = 8 per run, `per_worker_wake_cap` = 4 per worker; revisable after the PR 4 flash smoke | 4.4 |
| Q4 | `spec_gate_max_rejections` = 1, sharing the `gate_max_rejections` = 3 cap of `MiniSweEnvAdapter` | 5.6 |
| Q5 | The System-1 gate uses an independent decision model, configured by `--agent-gate-model`, `--agent-gate-base-url`, `--agent-gate-api-key-env`, `--agent-gate-threshold` and `--agent-gate-timeout`; no default is taken from the worker model | 0.1, 4.2, 4.3, 5.5, 10 |
| Q6 | `agent_rule_main` (`--agent-gate rule`) is an explicit arm next to `agent_main` and `agent_rand_main` | 6.1, PR plan PR 4 |
| Q7 | Decision model `liquid/d1.2-ultra` through OpenRouter (`https://openrouter.ai/liquid/d1.2-ultra`), OpenRouter Decisions API `POST https://openrouter.ai/api/alpha/decisions`; default base URL `https://openrouter.ai/api/alpha`, default key variable `OPENROUTER_API_KEY` | 4.3.2, 4.3.3, 10 |
| Q8 | Price $0.042 per 1M input tokens (weighted average $0.0419), $0.00 per 1M output tokens (`output_tokens = 0`). Context window 65,536 tokens. Median latency 0.22 s (provider Liquid), which is below the 3 s timeout | 4.3.2, 4.5, 8.1, 10 |

### 12.2 Open questions

None. Q1 to Q8 are resolved.


