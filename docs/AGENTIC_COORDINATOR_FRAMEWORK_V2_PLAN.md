# Agent Coordinator Framework v2 Plan

Status: plan only. No code edits and no commits are part of this document's task.
Base: `cameron-chen/goal-agentic-coordinator` at `b9aaa3d` (PR #34).
Input: Phase B2 results at `b9aaa3d` (`b2-agent_main` 8/10, `b2-agent_rand_main` 7/10,
`b2-agent_rule_main` 6/10, `b2-det_main` 6/10) and the user decisions of 2026-11-24.

## 0. Summary

| Item | Change | Protected-test impact |
| --- | --- | --- |
| Per-turn budget notice | Every request after the first in `opening` and `wake` ends with one `[remaining_tool_calls: N]` user message. The first request states the budget through `tool_call_budget`, as today. | `test_a1` and `test_a2` read `messages[-1]` as the newest tool result. Both need a 2-line change (Section 4.2). |
| Opening reserve | `reserve=1` → `reserve=2`. A rejected `submit_plan` on call 15 can be retried on call 16. | None |
| `wake_end` outcome | `_run_tools` returns its exit reason. A wake that sent a message and then ran out of budget records `sent` instead of `exhausted`. | None (`metrics()["wakes"]` is unchanged) |
| System-2 retries | `AgentCoordinatorConfig.max_retries = 2`. A retry loop in `_call` handles transient transport errors and respects `deadline` and `_stop`. | None |
| System-2 reasoning effort | `AgentCoordinatorConfig.reasoning_effort = "medium"`, set with the bench flag `--agent-coordinator-reasoning-effort`. The worker effort stays `high` through `COOPER_REASONING_EFFORT`. | None |
| Out of scope | `RandomGate` unused-draw mechanics (`random_unused`), `Makefile` and efficiency items, `wake_time_limit_s` (stays 90 s) | None |

PR split (Section 5): PR-T, a protected test-only PR for family `agent`; PR-C1 (code,
no dependency on PR-T); PR-C2 (per-turn notices, merged after PR-T).

Baseline at `b9aaa3d`: `uv run pytest -o addopts="" -q tests/protected tests/test_agent_coordinator.py
tests/test_agent_coordination_contract.py tests/test_agent_coordinator_harness.py` gives
216 passed. `python3 scripts/check_protected_tests.py --base origin/cameron-chen/goal-agentic-coordinator`
exits 0.

## 1. Audit of the current code

### 1.1 Tool loop (`src/cooperagents/agent_coordinator.py:615-681`)

- Loop condition: `run.calls < max_calls and not self._stop.is_set() and time.monotonic() < deadline`.
- `remaining = max_calls - run.calls`; `final = remaining <= reserve`. In a final turn only
  the `finish` tools are accepted. Any other tool returns
  `Error: <name> is not allowed in the final turn; allowed: <names>`, and the call counts.
- A batch that reaches `max_calls - reserve` during a non-final turn rejects further reads
  with `_READ_EXHAUSTED[phase]`. Those rejected reads do not count.
- Budget notice (`:633-637`): sent only when `run.turns` is non-empty and
  `remaining <= reserve + 1`. The notice is not stored in `run.turns`, so it is part of
  the current request only.
- Exit paths, all of which return `None`: no tool calls (`:640-641`); stop or deadline
  after a response (`:642-643`); stop or deadline during a batch (`:648-651`); `done`
  from `execute` (`:680-681`); the loop condition fails (budget, stop or deadline).

### 1.2 Phases

| Phase | Call site | `reserve` | `max_calls` | Deadline | `finish` |
| --- | --- | --- | --- | --- | --- |
| `opening-plan` | `run_opening`, `:734-745` | 1 | `opening_max_tool_calls` = 16 | `opening_time_limit_s` = 180 s | `submit_plan` |
| `wake` | `_run_wake`, `:1063-1067` | 2 | `wake_max_tool_calls` = 6 | `wake_time_limit_s` = 90 s | `send_message`, `end_wake` |
| `spec-check` | `_check_diff`, `:1116-1119` | single `_call` | 1 | 150 s | `spec_verdict` |

### 1.3 Wake outcome (`:1019`, `:1027-1031`, `:1068-1079`)

`outcome` starts as `"exhausted"` and becomes `"ended"` only when `end_wake` runs. A
`TimeoutError` gives `"stopped"` or `"timeout"`, and other exceptions give `"error"`. Every
silent return from `_run_tools` therefore records `"exhausted"`, including these cases:

- `send_message` succeeded on the last budget call. B2 data: 15 of 43 `b2-agent_main`
  wakes sent 1–4 messages and recorded `exhausted`.
- The model returned no tool call.
- The deadline passed after a response was received.

### 1.4 System-2 transport (`:366-382`, `:566-600`)

- `OpenAI(..., max_retries=0)`. `_call` makes one attempt through `_bounded_call`, and
  any exception propagates. Opening: an exception gives `outcome="failed"` (`:748`). Wake:
  an exception gives `outcome="error"`. B2 case `dspy_task:8394` f3_f5: an
  `APIConnectionError` ended the phase.
- `_OpenAIComplete.request` reads `sampling_kwargs()` with no `worker` flag. The
  coordinator therefore uses the same `COOPER_REASONING_EFFORT` as the workers
  (`sampling.py:52-63`), which is `high` on AWS and in polar-ext.
- The injected `complete` path (tests, `agent_coordinator_complete`) goes through the
  same `_bounded_call`, and `record_call` traces each request.

### 1.5 Protected assertions that read coordinator request messages

| Test | Line | Assertion | Affected by a per-turn notice |
| --- | --- | --- | --- |
| `test_a1_opening_plan_precedes_worker_requests` | `:271-272` | `messages[-1]["role"] == "tool"` and `content == "base two\n"` on the second request | Yes: `messages[-1]` becomes the notice. The stub then never submits, and the opening fails. |
| `test_a2_rejected_read_file_paths_execute_no_commands` | `:310-311` | `messages[-1]` is the tool result `"Error: invalid repository path"` on requests 2–4 | Yes, the same way |
| `test_a7_random_wakes_match_drawn_team_steps_and_model_observations` | `:521` | `json.loads(messages(request)[-1]["content"])` is the wake observation. Each wake makes exactly one request (`zip(..., strict=True)`). | No, as long as the first request of a wake has no notice |
| `test_a8_frozen_diff_rejection_acceptance_and_model_error` | `:543`, `:577` | `messages[-1]` is the spec observation; exactly one `error` row for the injected `RuntimeError` | No: spec-check has no tool loop, and `RuntimeError` is not retried |
| `test_a11_metrics_schema_counts_match_trajectory` | `:663-721` | Exact key sets of `metrics()`, `metrics["config"]` and `metrics["wakes"]`; `usage.coordinator.calls == len(request rows)` | No, as long as `metrics()` gains no keys and each retry attempt emits one `request` row and counts one call |

## 2. Budget notice, opening reserve and wake outcome

### 2.1 Notice placement

- Position: the last message of each request, as `{"role": "user", "content": <text>}`.
  It is appended after `messages + run.turns`, as today, and it is never stored in
  `run.turns` or `self._history`. The request prefix stays identical between turns,
  so provider prompt caching is unchanged.
- Turns: every request of a phase where `run.turns` is non-empty, which is every request
  except the first. Condition change at `:634`: `if run.turns and remaining <= reserve + 1:`
  → `if run.turns:`.
- First request: no notice. The budget is already given in the user message as
  `tool_call_budget`: the second message of a wake, and the first user message of the
  opening. This keeps `test_a7` unchanged. Adding a notice to the first request of a
  wake would also require a change to `test_a7`.
- Spec-check: no notice, because it is a single call.

### 2.2 Notice text

`reads = remaining - reserve` (the number of read calls still accepted).

| Condition | Opening constant | Wake constant |
| --- | --- | --- |
| `reads >= 2` | `OPENING_BUDGET_STATUS` (new) | `WAKE_BUDGET_STATUS` (new) |
| `reads == 1` | `OPENING_BUDGET_WARNING` (text unchanged) | `WAKE_BUDGET_WARNING` (text unchanged) |
| `reads <= 0` (final turn) | `OPENING_FINAL_TURN` (text unchanged) | `WAKE_FINAL_TURN` (text unchanged) |

New constants in `src/cooperagents/agent_coordinator_prompts.py`:

```python
OPENING_BUDGET_STATUS = (
    "[remaining_tool_calls: {remaining}] {reads} read calls remain. "
    "When {reserve} or fewer calls remain, only submit_plan is accepted."
)
WAKE_BUDGET_STATUS = (
    "[remaining_tool_calls: {remaining}] {reads} read calls remain. "
    "When {reserve} or fewer calls remain, only send_message and end_wake are accepted."
)
```

Selection in `_run_tools`, which replaces `:633-637`:

```python
note = []
if run.turns:
    status, warning, final_turn = _BUDGET_NOTES[phase]
    reads = remaining - reserve
    text = final_turn if final else warning if reads == 1 else status
    note = [{"role": "user", "content": text.format(remaining=remaining, reads=reads, reserve=reserve)}]
```

`_BUDGET_NOTES` becomes a 3-tuple per phase. `str.format` ignores the unused `reads`
and `reserve` arguments for the existing texts. With `reserve = 0`, the last request
receives the one-read warning if it follows an earlier request.

`BUDGET_NOTES_PROMPT_ID` hashes the 6 constants in this order: `OPENING_BUDGET_STATUS`,
`OPENING_BUDGET_WARNING`, `OPENING_FINAL_TURN`, `WAKE_BUDGET_STATUS`,
`WAKE_BUDGET_WARNING`, `WAKE_FINAL_TURN`.

After describing the reserve, `OPENING_PLAN_PROMPT` and `WAKE_PROMPT` each add
"Each request after the first ends with `[remaining_tool_calls: N]`."

Example opening sequence (`opening_max_tool_calls` = 16, `reserve` = 2):

| Request | `remaining` | Last message |
| --- | --- | --- |
| 1 | 16 | User message with `tool_call_budget: 16` |
| 2 | 15 | `[remaining_tool_calls: 15] 13 read calls remain. When 2 or fewer calls remain, only submit_plan is accepted.` |
| 13 | 4 | `[remaining_tool_calls: 4] 2 read calls remain. ...` |
| 14 | 3 | `OPENING_BUDGET_WARNING` with `remaining=3` |
| 15 | 2 | `OPENING_FINAL_TURN` with `remaining=2` |
| 16 | 1 | `OPENING_FINAL_TURN` with `remaining=1` |

Example wake sequence (`wake_max_tool_calls` = 6, `reserve` = 2): request 1 ends with
the observation; then `WAKE_BUDGET_STATUS` (5, reads 3), `WAKE_BUDGET_STATUS` (4, reads 2),
`WAKE_BUDGET_WARNING` (3), `WAKE_FINAL_TURN` (2) and `WAKE_FINAL_TURN` (1).

### 2.3 Opening `reserve = 2`

- `run_opening` (`:743`): `reserve=1` → `reserve=2`. `finish` stays `[SUBMIT_PLAN_TOOL]`.
- `_READ_EXHAUSTED["opening-plan"]`: "Error: read budget exhausted; the remaining calls are
  reserved for submit_plan."
- Effect at 16 calls: up to 14 reads. Calls 15 and 16 accept only `submit_plan`. A
  `submit_plan` that fails validation on call 15 (for example `invalid opening plan: ...`)
  returns its error as the tool result. The request for call 16 ends with
  `OPENING_FINAL_TURN` (`remaining=1`), and the plan can be resubmitted. B2 case
  `dspy_task:8394` f3_f4 failed because no call remained after the rejected `submit_plan`.
- When `opening_max_tool_calls` ≤ 2, every turn is final. Existing tests with
  `opening_max_tool_calls` of 1 or 2 keep their behavior. The exception is
  `test_unoffered_tool_is_counted_error`, which is listed in Section 6.2.

### 2.4 Exit reason and `wake_end` outcome

`_run_tools` returns one of the following values:

| Return value | Condition |
| --- | --- |
| `"done"` | `execute` returned `done=True` (`submit_plan` delivered, or `end_wake`) |
| `"no_calls"` | The response contained no tool call (after the textual fallback) |
| `"stopped"` | `self._stop` is set (checked at each existing stop/deadline check, and after the loop) |
| `"deadline"` | `time.monotonic() >= deadline` with `_stop` unset |
| `"budget"` | The loop ended with `run.calls >= max_calls` |

`_run_wake` maps the result to `outcome`:

| Precedence | Condition | `outcome` |
| --- | --- | --- |
| 1 | `end_wake` executed | `ended` (unchanged) |
| 2 | `TimeoutError` raised while `_stop` is set, or return `"stopped"` | `stopped` |
| 3 | `TimeoutError` raised while `_stop` is unset, or return `"deadline"` | `timeout` |
| 4 | Any other exception | `error` (unchanged) |
| 5 | Return `"budget"` or `"no_calls"` and `run.sent > 0` | `sent` (new) |
| 6 | Return `"budget"` and `run.sent == 0` | `exhausted` |
| 7 | Return `"no_calls"` and `run.sent == 0` | `no_call` (new) |

The `stale` and `finished` outcomes emitted before a wake starts (`:986-993`) are
unchanged. `messages_sent` stays in `wake_end`. `metrics()["wakes"]` keeps its four keys
(`test_a11`). The outcome is reported only in `wake_end`.

`run_opening` keeps `outcome` ∈ {`delivered`, `failed`, `disabled`}. The `opening_plan`
event gains `exit_reason` (the `_run_tools` return value, or `"error"` when an exception
ended the phase). `metrics()["opening"]` keeps its four keys (`test_a11`).

## 3. System-2 retries and reasoning effort

### 3.1 Configuration (`src/cooperagents/agent_coordination_types.py:286-297`)

Two fields are appended after `prompts`:

```python
@dataclass(frozen=True)
class AgentCoordinatorConfig:
    ...
    prompts: dict[str, str] | None = None
    max_retries: int = 2
    reasoning_effort: str | None = "medium"
```

Backward compatibility:

- Existing positional and keyword constructions are unchanged because the new fields are
  last and have defaults. This includes `AgentCoordinatorConfig()` in
  `tests/test_agent_coordination_contract.py:28`, `bench_compare.py:708`, `harness.py:2310`
  and `:3434`, and `replace(...)` in the protected `config()` helper.
- `reasoning_effort=None` restores the pre-v2 request: the value comes from
  `COOPER_REASONING_EFFORT` through `sampling_kwargs()`.
- `max_retries=0` restores single-attempt behavior.
- Validation: `max_retries` is an `int` ≥ 0. `reasoning_effort` is `None` or a non-empty
  string. Both are checked where `bench_compare.py` builds the config (an `ap.error`
  message) and in `AgentCoordinator.__init__` (`ValueError`).
- The default behavior changes for every caller that uses the built-in OpenAI client.
  System-2 requests now send `reasoning_effort="medium"`. This change is intended.
  Callers that inject `complete` receive `(messages, tools)` only, so the field has no
  effect on them.

### 3.2 Retry loop in `_call`

The SDK client keeps `max_retries=0` (`:376`), and the existing test
`clients[0]["max_retries"] == 0` stays. Retries happen in `_call` for two reasons: each
attempt is then visible in the trajectory, and the wait checks `deadline` and `_stop`.
The SDK retry sleeps check neither, and SDK attempts would continue in the abandoned
`_bounded_call` thread after the deadline.

```python
_RETRYABLE = {"APIConnectionError", "RateLimitError", "InternalServerError"}  # APITimeoutError subclasses APIConnectionError

def _retryable(exc: BaseException) -> bool:
    # Match by class name so a stubbed openai module in tests still classifies.
    return any(cls.__name__ in _RETRYABLE for cls in type(exc).__mro__)

def _call(self, messages, tools, phase, deadline, usage):
    for attempt in range(self.config.max_retries + 1):
        try:
            return self._call_once(messages, tools, phase, deadline, usage, attempt)
        except Exception as exc:
            delay = 0.5 * 2 ** attempt
            if (attempt == self.config.max_retries or not _retryable(exc)
                    or self._stop.is_set() or time.monotonic() + delay >= deadline):
                raise
            self._stop.wait(delay)
```

- `_call_once` is the current `_call` body (`:566-600`). It adds `attempt=attempt` to the
  `trace` partial, so the `request`, `error` and `response` rows carry the attempt number.
- Each attempt increments `usage["calls"]` and `_system2_usage["calls"]` and emits one
  `request` row. This preserves `test_a11` (`calls == len(request rows)`).
- Not retried:
  - `TimeoutError` from `_bounded_call` (the phase deadline or a stop).
  - `ValueError` and other non-transport errors.
  - The injected `RuntimeError` in `test_a8`, which therefore still produces exactly one
    `error` row.
- Waits are 0.5 s and then 1.0 s. A retry does not start if its wait would pass the
  deadline. `self._stop.wait(delay)` returns early on stop, and the next `_bounded_call`
  then raises `TimeoutError`, which the wake maps to `stopped`.
- Scope: all System-2 calls (opening, wake, spec-check). The retries apply to the injected
  `complete` path as well, which makes them testable offline with
  `openai.APIConnectionError`.
- `wake_time_limit_s` stays 90 s, and retries run inside it.

### 3.3 Reasoning effort wiring

- `_OpenAIComplete.__init__(model, base_url, api_key, reasoning_effort)` stores the value.
  `AgentCoordinator.__init__` passes `config.reasoning_effort` (`:481`).
- `_OpenAIComplete.request`:

  ```python
  options = sampling_kwargs()
  if self.reasoning_effort is not None:
      options["reasoning_effort"] = self.reasoning_effort
  ```

  Precedence: `config.reasoning_effort` if it is not `None`, otherwise
  `COOPER_REASONING_EFFORT`, otherwise the field is omitted.
- Workers: unchanged. `sampling_kwargs(worker=True)` still reads `COOPER_REASONING_EFFORT`
  (`high` on AWS and in polar-ext). The vendored mini-swe agent and `mini_swe_worker.py`
  are not edited.
- System 1 (`DecisionGate`, OpenRouter Decisions API) is unaffected.
- Bench flag in `scripts/bench_compare.py`, next to `--agent-opening` (`:590`):

  ```python
  ap.add_argument("--agent-coordinator-reasoning-effort", default="medium",
                  help='System-2 coordinator reasoning_effort; "inherit" uses COOPER_REASONING_EFFORT (default: medium)')
  ```

  The flag maps `"inherit"` to `None` and passes the value as
  `AgentCoordinatorConfig(..., reasoning_effort=...)` at `:708`. The guard at `:709-714`
  adds `args.agent_coordinator_reasoning_effort != "medium"` to the "agent coordinator
  flags require --agent-coordinator" condition.
- No `COOPER_COORDINATOR_REASONING_EFFORT` environment variable is added. polar-ext checks
  that the rendered `sampling.json` equals a fixed dict (`audit.py:1027-1040`), and a new
  `COOPER_*` variable would change that dict. A launch flag goes through the flag audit,
  which polar-ext already extends per variant (Section 3.4).
- Recording:
  - The `agent_coordinator_start` event gains `reasoning_effort` and `max_retries`.
  - The run-start trajectory row from `bench_compare` (`agent_fields`, `:405-415`) gains
    `agent_reasoning_effort`.
  - Each System-2 `request` row already records the full request, including
    `reasoning_effort`.
  - `metrics()["config"]` is unchanged because `test_a11` asserts its exact dict.
- The matched random arm (`--agent-random-match`) must be launched with the same
  `--agent-coordinator-reasoning-effort` value as its `agent_main` run. bench_compare does
  not check this. The AWS launcher passes the flag explicitly to every agent arm.

### 3.4 polar-ext compatibility note (text for Spec §10)

Source checked: `polar-ext/goal-cooperbench-coordination` at `2661281`,
`polar_ext/tasks/cooperagents/study/audit.py`.

| Check | Current behavior | Effect of System-2 `reasoning_effort = "medium"` |
| --- | --- | --- |
| Criterion 6 (`check_wire_criterion`, `audit.py:752-754`) | Every recorded `request` row and every `wire_request` row that has `model` or `messages` must have `reasoning_effort == "high"`. The loop at `audit.py:1238-1283` applies it to all actors, including `coordinator`. No per-actor exception exists. | Agent-coordinator `coordinator` rows carry `"medium"` and would fail with "request reasoning effort is invalid". Worker rows stay `"high"` and pass. |
| Rendered sampling (`audit.py:1027-1040`) | `sampling.json` must equal a fixed dict with `COOPER_REASONING_EFFORT = "high"` | Unchanged, because the coordinator value comes from a launch flag. |
| Fixed request dict (`audit.py:494-500`) | `reasoning_effort: "high"` in the expected wire request | The same per-actor rule as criterion 6 applies. |
| Reasoning replay (`check_reasoning`, `audit.py:596-695`) | Actor `coordinator` in system arms is excluded from the replay check (`:618-621`) | Unchanged |
| Variants (`audit.py:981`) | `system_arm` covers only `det_smoke` and `plan_smoke`. The coordinator tool allowlist (`:730-737`) is the planning protocol. | No agent-coordinator variant is audited today, so the change has no current audit failure. |

Spec §10 gains one row:

| Area | Current behavior | Required change |
| --- | --- | --- |
| System-2 reasoning effort | `audit.py:752-754` requires `reasoning_effort == "high"` on every request row, including actor `coordinator` | Agent variants check reasoning effort per actor. Worker actors (`agent*`, `integrator*`) require `"high"`. Actor `coordinator` requires the study constant `COORDINATOR_REASONING_EFFORT = "medium"`, which must equal the `--agent-coordinator-reasoning-effort` launch flag and the `agent_coordinator_start.reasoning_effort` event field. Actor `coordinator_gate` rows have no `reasoning_effort` and are excluded from criteria 5 and 6. `rendered-config/sampling.json` keeps `COOPER_REASONING_EFFORT = "high"`. Agent variant launch flag lists include `--agent-coordinator-reasoning-effort medium` explicitly. |

Related finding (cause of later audit failure, not changed by this plan):
`_OpenAIComplete.request` sends `tool_choice="auto"` (`agent_coordinator.py:382`).
`tool_choice` is in the forbidden set of criterion 9 (`audit.py:755-780`) for wire rows.
An agent variant audit needs an exception for actor `coordinator`, or the request must
omit `tool_choice`. The spec §10 row lists this as an open item for the polar-ext side.

## 4. Protected tests

### 4.1 Effect of each change

| Change | Protected effect |
| --- | --- |
| Opening `reserve = 2` | None. `test_a1` uses 2 calls, `test_a2` uses 4 and `test_a11` uses 1, all out of 16. |
| Wake exit reason and outcome | None. The tests that wait on `outcome="ended"` use `end_wake`. |
| `max_retries`, `reasoning_effort` | None. Injected completions ignore `reasoning_effort`. `test_a8` raises `RuntimeError`, which is not retried. `metrics()` keys are unchanged. |
| Per-turn notice | `test_a1` and `test_a2` (Section 1.5) |

### 4.2 Exact change to `tests/protected/test_agent_coordinator_macro.py` (PR-T)

The updated tests must pass on the current code (no notice after a tool result) and on
PR-C2 (notice after every tool result). PR-T can then merge before PR-C2, and both
PRs pass CI.

Add a helper after `tool()` (line 64):

```python
def latest_result(messages):
    """Return the newest message before a trailing coordinator budget notice."""
    if messages[-1]["role"] == "user" and messages[-1]["content"].startswith("[remaining_tool_calls: "):
        return messages[-2]
    return messages[-1]
```

`test_a1` (lines 271-272):

```python
        if latest_result(messages)["role"] == "tool":
            assert latest_result(messages)["content"] == "base two\n"
```

`test_a2` (lines 310-311):

```python
            assert latest_result(messages)["role"] == "tool"
            assert latest_result(messages)["content"] == "Error: invalid repository path"
```

No other line changes. No test is skipped, weakened in its exact content values, or
removed. The exact notice text is checked in non-protected tests (Section 6.2). On the
first request, `messages[-1]` is the user JSON message, which does not start with
`[remaining_tool_calls: `, so the helper returns it unchanged.

### 4.3 Fallback if PR-T is not approved

PR-C1 merges without PR-T. PR-C2 is not merged. Notices stay limited to the last two
turns. The other v2 items are unaffected.

## 5. PR split

`scripts/check_protected_tests.py` rejects any non-protected path in a PR that uses
`--allow-protected` (except guard support files with `guard`). Its family for
`tests/protected/test_agent_coordinator_macro.py` is `agent`. All PRs target
`cameron-chen/goal-agentic-coordinator`.

| Order | PR | Title | Files | Depends on |
| --- | --- | --- | --- | --- |
| 1 (parallel with 2) | PR-T | `test: read agent macro tool results before a trailing budget notice` | `tests/protected/test_agent_coordinator_macro.py` only | User approval before merge (CLAUDE.md, protected tests) |
| 2 | PR-C1 | `fix: reserve two opening calls, report wake exit outcomes, retry system-2 calls, and set coordinator reasoning effort` | `src/cooperagents/agent_coordinator.py`, `agent_coordination_types.py`, `agent_coordinator_prompts.py`, `scripts/bench_compare.py`, `tests/test_agent_coordinator.py`, `tests/test_agent_coordinator_harness.py`, `tests/test_agent_coordination_contract.py` (only if needed), both qualification JSON files, `docs/AGENTIC_COORDINATOR_SPEC.md` (§5.3, §5.4, §5.5, §8.2, §10, §12.1), this plan | None |
| 3 | PR-C2 | `feat: send coordinator budget notice on every turn after the first` | `agent_coordinator.py` (notice condition, 3-tuple selection), `agent_coordinator_prompts.py` (two constants, prompt sentences, `BUDGET_NOTES_PROMPT_ID`), `tests/test_agent_coordinator.py`, both qualification JSON files, spec §4.6, §5.3, §5.4 and D1, this plan | PR-T and PR-C1 merged |

Notes:

- Before merging PR-T, run its file against the PR-C2 branch locally (Section 7.2). This
  shows that it is compatible with both code states.
- PR-C1 and PR-C2 each regenerate `reader_qualification.json` and
  `direct_capture_qualification.json`, because the hashes of the three coordinator
  modules change.
- Spec D1 records the default reserve of two tool calls for the opening and each wake.
  PR-C2 records that every request after the first ends with a budget notice and that
  status notices use the configured reserve for that phase.
- No PR edits `CooperBench/`, `src/cooperagents/vendor/**`, `src/cooperagents/workers/**`,
  `scripts/check_protected_tests.py`, `RandomGate`, or the `Makefile`.

## 6. Acceptance tests

### 6.1 PR-C1 new tests (`tests/test_agent_coordinator.py`)

| Test | Setup | Assertions |
| --- | --- | --- |
| `test_opening_retries_invalid_plan_on_last_call` | `opening_max_tool_calls=16`; stub: 14 × `read_file`, then `submit_plan` with `workers={"agent3": ...}`, then a valid `submit_plan` | Request 15 ends with `OPENING_FINAL_TURN` (`remaining=2`). Request 16 ends with `OPENING_FINAL_TURN` (`remaining=1`), and `messages[-2]` is the roster error. `outcome == "delivered"`, `tool_calls == 16`. |
| `test_opening_final_window_rejects_reads` | `opening_max_tool_calls=4`; stub: `read`, `read`, `read`, `submit_plan` | Request 3: `final` and the read returns `Error: read_file is not allowed in the final turn; allowed: submit_plan`. Request 4 delivers. `tool_calls == 4`. |
| `test_wake_outcome` (parametrized) | `wake_max_tool_calls=6` | (a) 4 reads, `send_message`, `send_message` → `sent`, `messages_sent == 2`, `tool_calls == 6`. (b) 4 reads, 2 `end_wake` calls with invalid args → `exhausted`. (c) `send_message`, then an empty response → `sent`. (d) Empty first response → `no_call`. (e) `coordinator.stop()` during the first call → `stopped`. (f) The deadline passes after a response → `timeout`. |
| `test_system2_retries_transient_errors` (parametrized over opening, wake and spec-check) | Injected `complete` raises `openai.APIConnectionError(request=httpx.Request("POST", "https://example.test"))` twice, then returns a valid call | 3 `request` rows with `attempt` 0, 1, 2; 2 `error` rows; phase succeeds; `usage.coordinator.calls == 3` |
| `test_system2_retry_limits` (parametrized) | (a) 3 transport errors; (b) `RuntimeError`; (c) `max_retries=0`; (d) deadline shorter than the 0.5 s wait; (e) `stop()` during the wait | (a) 3 attempts, then the phase fails (opening `failed`, wake `error`). (b)–(d) 1 attempt. (e) No second `request` row, wake `stopped`. |
| `test_default_completion_reasoning_effort` (parametrized) | `COOPER_REASONING_EFFORT=high`; stubbed `openai.OpenAI` as in `test_default_completion_uses_sdk_and_records_usage` | Default config gives a request with `reasoning_effort == "medium"`. `reasoning_effort=None` gives `"high"`. `reasoning_effort="low"` gives `"low"`. `sampling_kwargs(worker=True)["reasoning_effort"] == "high"` in all cases. `clients[0]["max_retries"] == 0`. |
| `test_config_validation` | `max_retries=-1`, `reasoning_effort=""` | `ValueError` |

`tests/test_agent_coordinator_harness.py` (bench flag test next to `:355-366`):

- Default: the start row has `agent_reasoning_effort == "medium"`.
- `--agent-coordinator-reasoning-effort inherit` gives `None`.
- The flag without `--agent-coordinator` is rejected by `ap.error`.

PR-C1 updates to existing tests:

| Test | Update |
| --- | --- |
| `test_opening_reserves_submit_plan_turn` | With `max=4` and `reserve=2`: request 2 ends with `OPENING_BUDGET_WARNING` (`remaining=3`), request 3 with `OPENING_FINAL_TURN` (`remaining=2`) and request 4 with `OPENING_FINAL_TURN` (`remaining=1`). The third read returns the final-turn error. `tool_calls == 4`. |
| `test_opening_batched_reads_keep_reserve` | 2 reads return `"alpha\n"`. 4 reads return `"Error: read budget exhausted; the remaining calls are reserved for submit_plan."` and are not counted. Request 2 ends with `OPENING_FINAL_TURN` (`remaining=2`). `tool_calls == 3`. |
| `test_unoffered_tool_is_counted_error` | `opening_max_tool_calls=2` → `4`, so that the first turn is not final and the error text stays "is not offered in this turn". |
| `test_wake_reserves_message_turn` | Unchanged in PR-C1. |

### 6.2 PR-C2 test updates and new tests

| Test | Assertions |
| --- | --- |
| `test_budget_notice_every_turn` (opening with 16 calls and reserve 2, 3, or 0, and wake with 6 calls and reserve 2) | Request 1 has no message that starts with `[remaining_tool_calls:`. For each request `k ≥ 2`, `messages[-1]` is exactly the Section 2.2 tier text with `remaining = max - (k - 1)` and the configured reserve, and no earlier message in that request starts with `[remaining_tool_calls:`. |
| `test_wake_reserves_message_turn` | Requests 2 and 3 end with `WAKE_BUDGET_STATUS` (5, reads 3) and (4, reads 2). Request 4 ends with `WAKE_BUDGET_WARNING` (3), and request 5 with `WAKE_FINAL_TURN` (2). The existing line 467 check stays. |
| `test_opening_delivers_a_valid_plan_once` | Find the read result by its tool role because the budget notice ends the request. |
| `test_wake_uses_configured_reserve` | The last request with a zero reserve ends with the one-read warning. With a positive reserve, it ends with the final-turn notice. |
| `test_opening_reserves_submit_plan_turn`, `test_opening_batched_reads_keep_reserve` | Unchanged from PR-C1 (their notice indexes are already final or warning tiers). |
| Prompt ID test (`:278`) | `ids["budget-notes"] == BUDGET_NOTES_PROMPT_ID`, which is recomputed over 6 constants. |

### 6.3 Protected suite

All 3 protected files pass unchanged on PR-C1. On PR-C2 they pass with the PR-T version
of `test_agent_coordinator_macro.py`.

## 7. Verification commands

### 7.1 Every PR

```sh
uv run pytest -o addopts="" -q tests/protected
uv run pytest -o addopts="" -q tests/test_agent_coordinator.py tests/test_agent_coordinator_harness.py \
  tests/test_agent_coordination_contract.py tests/test_metrics.py tests/test_structural_signals.py \
  tests/test_system1_gate.py
uv run pytest -o addopts="" -q                 # full suite; failures must match the known pre-existing set
git diff --name-only origin/cameron-chen/goal-agentic-coordinator...HEAD -- CooperBench \
  src/cooperagents/vendor src/cooperagents/workers scripts/check_protected_tests.py   # must print nothing
```

### 7.2 PR-T

```sh
python3 scripts/check_protected_tests.py --base origin/cameron-chen/goal-agentic-coordinator --allow-protected agent
git diff --name-only origin/cameron-chen/goal-agentic-coordinator...HEAD   # exactly tests/protected/test_agent_coordinator_macro.py
uv run pytest -o addopts="" -q tests/protected/test_agent_coordinator_macro.py   # on b9aaa3d code
git checkout <PR-C2 branch> -- src/cooperagents && \
  uv run pytest -o addopts="" -q tests/protected/test_agent_coordinator_macro.py && \
  git checkout HEAD -- src/cooperagents                                    # on PR-C2 code; restore afterwards
```

### 7.3 PR-C1 and PR-C2

```sh
python3 scripts/check_protected_tests.py --base origin/cameron-chen/goal-agentic-coordinator   # exit 0, no allowance
git diff --name-only origin/cameron-chen/goal-agentic-coordinator...HEAD -- tests/protected      # must print nothing
python3 scripts/qualify_direct_capture.py
python3 scripts/qualify_published_reader.py
git diff --exit-code src/cooperagents/reader_qualification.json src/cooperagents/direct_capture_qualification.json  # clean after commit
uv run pytest -o addopts="" -q tests/test_published_profiles.py tests/test_direct_capture.py
uv run python scripts/bench_compare.py --help | grep -- --agent-coordinator-reasoning-effort   # PR-C1
```

### 7.4 AWS smoke after PR-C2 (1 pair, `changyu-sandbox-eval`)

Use the same host, staging and wrapper as Phase A1 (`docs/AGENTIC_COORDINATOR_FIX_AND_AWS_PLAN.md`).
Pair: `dspy_task:8394:3,4`. Arm: `--agent-coordinator --agent-gate model
--agent-coordinator-reasoning-effort medium`. After `uv sync --locked`, the editable
CooperBench install must be reinstalled.

| # | Check (from `trajectory.jsonl.gz` and `result.json`) |
| --- | --- |
| 1 | Every `coordinator` `request` row has `reasoning_effort == "medium"`. Every worker `request` row has `"high"`. The Tinker endpoint returns no error for `"medium"`. |
| 2 | Every opening and wake request after the first ends with a `[remaining_tool_calls: N]` user message, and N decreases by the number of counted calls. |
| 3 | `opening_plan.outcome == "delivered"`. The `exit_reason` field is present. |
| 4 | Every `wake_end.outcome` is in {`ended`, `sent`, `exhausted`, `no_call`, `timeout`, `stopped`, `error`, `stale`, `finished`}. No wake with `messages_sent > 0` records `exhausted`. |
| 5 | `request` rows carry `attempt`. Any `error` row with `error_type` `APIConnectionError` is followed by a request with `attempt + 1`, or by the phase end when retries or time ran out. |
| 6 | `eval.json` written; secret scan 0 hits. |
| 7 | Wake `seconds` p50/p95 and coordinator `completion_tokens` are reported next to the B2 `b2-agent_main` values. |

A B3 comparison on the new pin reruns all four arms (`det_main`, `agent_main`,
`agent_rule_main`, `agent_rand_main`) and does not pool with the B2 runs at `b9aaa3d`
(spec §10, source pin and pooling).

## 8. Decisions

| ID | Decision |
| --- | --- |
| V1 | The notice goes on every request after the first in opening and wake. The first request states the budget through `tool_call_budget`, which keeps `test_a7` and the request-prefix caching unchanged. |
| V2 | The notice text has three tiers (status, warning, final). The warning and final texts are unchanged. |
| V3 | Opening `reserve = 2`. Wake `reserve` stays 2. |
| V4 | New wake outcomes `sent` and `no_call`. A deadline detected inside the loop reports `timeout`. `metrics()` is unchanged. |
| V5 | System-2 retries are implemented in `_call` (0.5 s and 1.0 s waits, bounded by the phase deadline and `_stop`). The SDK keeps `max_retries=0`. |
| V6 | `AgentCoordinatorConfig.reasoning_effort = "medium"` applies to opening, wake and spec-check. The bench flag `--agent-coordinator-reasoning-effort` (with `inherit` for `None`) sets it, and no new environment variable is added. |
| V7 | PR order: PR-T and PR-C1 in parallel, then PR-C2. PR-T needs explicit user approval. |
