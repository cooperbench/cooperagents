# Agent-Mode Coordinator: Tool and Budget Fix, and AWS Smoke Plan

Status: plan for review (2026-11-23). No code in this document is implemented.

Base: `cameron-chen/goal-agentic-coordinator` @ `ac202ddc` (PRs #27–#31 merged).

Inputs:

- 1-pair live smoke `smoke1-agent-main` / `smoke1-agent-rand` on
  `huggingface_datasets_task:6252:4,6` (local, Tinker `Inkling-2-Small`, OpenRouter
  `liquid/d1.2-ultra`, `--step-limit 40 --agent-time-limit 900`).
- Audit of `src/cooperagents/agent_coordinator.py`,
  `src/cooperagents/agent_coordination_types.py`,
  `src/cooperagents/agent_coordinator_prompts.py`, `scripts/bench_compare.py`,
  `tests/protected/test_agent_coordinator_macro.py`, `tests/test_agent_coordinator.py`,
  `tests/test_agent_coordinator_harness.py`, `tests/test_agent_coordination_contract.py`,
  `tests/test_metrics.py`.
- polar-ext AWS recipe `polar_ext/recipes/cooperbench_coordination/aws/` (README,
  `deployment.example.toml`) and `polar_ext/recipes/cooperagents_tinker/aws/ssm_job.sh`.

## 0. Summary

| Item | Result |
| --- | --- |
| Fix PR | One PR into `cameron-chen/goal-agentic-coordinator`. Files: the three agent-coordinator modules, `scripts/bench_compare.py`, non-protected tests, both qualification JSON files, and the spec. Zero edits to `tests/protected/**` and `scripts/check_protected_tests.py`. |
| Main fix | The opening and wake tool loops keep a reserved final turn inside the existing call cap. In that turn only the finishing tools are offered (`submit_plan`; `send_message` + `end_wake`). A budget note is appended as a trailing user message only in the last two turns. |
| Other fixes | Tool descriptions state the path convention, the fixed-string search rule and the accepted revisions; empty results and errors return explanatory text; read tools take an optional `rev`; invalid `submit_plan` arguments no longer end the opening while calls remain; `summary.json` gains `coordinator_gate.reported_cost_usd`. |
| Protected-test compatibility | Every exact assertion in `tests/protected/test_agent_coordinator_macro.py` was checked against the design (Section 2.3). The design changes none of the asserted values. Baseline at `ac202ddc`: the five test files pass (157 passed); `check_protected_tests.py --base ac202ddc` exits 0. |
| AWS | `changyu-sandbox-eval` (`i-0f892273b477977c7`, `us-west-2`, `m6a.16xlarge`, instance profile `polar-ext-repair-sandbox-ssm`). State at plan time: `stopped`; `instanceInitiatedShutdownBehavior` = `stop`. Runtime: Apptainer through `COOPER_RUNTIME=apptainer` and the cooperagents Apptainer evaluator. Docker is not required. |
| Open decisions | Five, listed in Section 5. |

## 1. Smoke findings that this plan addresses

| # | Phenomenon (smoke1-agent-main unless noted) | Cause | Section |
| --- | --- | --- | --- |
| F1 | Opening: 16/16 tool calls, no `submit_plan`, `outcome=failed`. Both arms. | The model is not told its call budget, and nothing reserves a call for `submit_plan`. Several `search_code` calls sent regular expressions (`a\|b`) to `git grep -F` and received `""`. | 2.1, 2.4, 3.3, 3.4 |
| F2 | The single wake used 6/6 tool calls on reads and sent 0 messages (`outcome=exhausted`). The random arm's wake also sent 0 messages. | Same budget issue as F1, for `send_message`/`end_wake`. | 2.4, 3.4 |
| F3 | `spec_check.checked = 0`. Both submissions were rejected by the base completion gate with `make: ruff: not found`. | `verification.validate` runs the default `make` target; in this task image the default target calls `ruff`, which is not installed. The composed gate returns the base error before the spec check runs (`agent_coordinator.py:1151-1156`). After 3 base rejections the worker stops calling the gate (`mini_swe_worker.py:142`), so the spec check is never reached for this task. | 2.8, D3 |
| F4 | `summary.json` has no `reported_cost_usd`. | `_write_summaries` sums only `calls`, `prompt_tokens`, `cached_tokens`, `completion_tokens` (`bench_compare.py:511-516`). | 2.6, 3.8 |
| F5 | `team_worker_steps` 81 against `total_steps` 80 (main) and 73 against 80 (random). | Different units (Section 2.7). No fix required. | 2.7 |
| F6 | Local `cooperbench eval` could not reach Docker. | The local Docker context is an SSH context; the eval is moved to AWS with Apptainer. | 4 |

## 2. Audit

### 2.1 Tool schemas

`_tool()` (`agent_coordination_types.py:301-310`) sets every `description` to the tool name
with underscores replaced by spaces, and no parameter has a description. The model
therefore receives no information about the items below.

| Tool | Behavior in code | Missing from the schema |
| --- | --- | --- |
| `list_dir` | `git ls-tree --name-only <rev>:<path>`; `"."`, `"./"` and `""` select the root; ≤ 400 entries (`agent_coordinator.py:200-204`) | Repository-relative path; `"."` for the root; entry limit |
| `read_file` | `git show <rev>:<path> \| sed -n start,min(end,start+399)p`; ≤ 16000 characters (`:205-212`) | Repository-relative path; 1-based inclusive range; 400-line and 16000-character limits |
| `search_code` | `git grep -n -F -I --max-count=20 -e <pattern> <rev> -- :(glob)<path_glob>`; exit code 1 accepted; ≤ 200 rows (`:213-221`) | Fixed-string matching (`-F`): `\|`, `.*`, `\b` are matched literally; `path_glob` is a Git glob relative to the root, `"."` or `""` for the whole repository; per-file and total limits |
| `git_show` | Without `path`: `git show --stat <rev>`. With `path`: `git diff <base> <rev> -- <path>` (`:222-227`). `rev` must be a full 40/64-hex SHA equal to the base SHA or a snapshot SHA (`:183-187`) | Accepted `rev` values; `HEAD` and branch names are rejected; meaning of `path` |
| `send_message` | Recipient is a roster ID or `group`; ≤ 1500 characters; ≤ 2 calls per wake (`:897-910`) | Valid recipients; per-wake limit |
| `submit_plan` | Validated by strict `from_json(OpeningPlan)`; `workers` keys must equal the roster (`:586-599`) | `workers` must have one entry per roster ID; the call ends the opening |
| `spec_verdict` | Each finding's `quote` must be a substring (1–300 characters) of the supplied feature text, or the verdict becomes `ERROR` (`:989-992`) | The exact-substring rule |
| `end_wake` | Requires a string `note` | When to call it |

Wake read revision. In a wake, `list_dir`, `read_file` and `search_code` read the latest
snapshot of the wake target (`_read_tool(..., worker=target)`, `agent_coordinator.py:911`;
`ReadOnlyGit.read`, `:182-183`). The wake observation does not name the target (protected
A7 fixes its keys), so the model cannot know which tree it reads, and it cannot read
another worker's snapshot with these tools. Section 3.3 adds an optional `rev` argument
with the base SHA as default (decision D2).

### 2.2 Error messages and empty outputs

Errors are returned to the model as `f"Error: {exc}"` (`agent_coordinator.py:556-557`).

| Source | Current text | Assessment | Test constraint |
| --- | --- | --- | --- |
| `_path` | `invalid repository path` | Adequate together with the new path description | **Protected A2 asserts the exact tool content `"Error: invalid repository path"`. Keep unchanged.** Unit test matches `"path"`. |
| `_path` | `repository path is required` | Does not say which form is expected | Unit test matches `"path"` |
| `ReadOnlyGit.read` | `revision is outside the available snapshots` | Does not list accepted values | Unit test matches `"revision"` |
| `ReadOnlyGit.read` | `invalid line range` | Does not state the rule | Unit test matches `"line range"` |
| `ReadOnlyGit.read` | `invalid fixed search pattern` | Adequate | None |
| `ReadOnlyGit.read` | `Git object query failed` | Returned for a missing path; gives no hint | None |
| `ReadOnlyGit.read` | `unknown read-only tool` | Returned when the model calls a tool that is not offered in this phase (for example `send_message` in the opening), because every non-finishing name falls through to `_read_tool` | None |
| wake `send_message` | `message limit reached` | Does not state the limit or the next action | None |
| wake `send_message` | `unknown message recipient` | Does not list valid recipients | None |
| wake `send_message` | `message exceeds the 1500-character limit or is invalid` | Adequate | None |
| opening `submit_plan` | `opening plan must cover the worker roster` | Does not list expected and received IDs | None |
| opening `submit_plan` | strict decoder messages from `from_json` | Adequate; no prefix identifies the source | None |
| `_tool_args` | `json.loads` message, `expected a JSON object`, `malformed tool call` | Adequate | Unit test sends `"{invalid"` and only checks `outcome="failed"` |
| `search_code` exit 1 | `""` | The model cannot tell "no match" from a failed call | **Unit test `test_agent_coordinator.py:189` asserts `git.read(...) == ""`.** Replace the empty text in the coordinator wrapper; `ReadOnlyGit.read` keeps returning `""`. |
| `git_show` with `path`, no change | `""` | Same | None |
| `read_file` past end of file | `""` | Same | None |
| Truncation at 400 lines / 16000 characters / 200 rows | Silent | The model cannot tell that output was cut | **Unit test `:183-184` asserts the truncated lengths from `git.read`.** Add the notice in the wrapper only. |

Opening behavior on an invalid `submit_plan`: any exception for a `submit_plan` call sets
`done=True` (`agent_coordinator.py:558-559`), so one malformed plan ends the opening even
with calls left. Section 3.4 removes this special case.

### 2.3 Protected and unit-test constraints on the design

| Test | Exact assertion | Consequence for the design |
| --- | --- | --- |
| A1 (`protected:268-292`) | The scripted coordinator branches on `messages[-1]["role"] == "tool"` and asserts `messages[-1]["content"] == "base two\n"`; request phases `["opening-plan", "opening-plan"]`; response tool names `["read_file", "submit_plan"]` | A remaining-call indicator in every turn is excluded: a trailing user message would change `messages[-1]`, and a suffix on the tool result would change its content. The indicator appears only when ≤ reserve + 1 calls remain (opening budget 16: never in A1). Read results stay byte-identical. |
| A2 (`:295-322`) | Tool content equals `"Error: invalid repository path"`; no command executes for a rejected path | `_path` message unchanged. No notes in the first 4 calls of a 16-call budget. |
| A3, A6, A12 (`:325-356`, `:439-466`, `:729-768`) | `end_wake` helper asserts `"end_wake" in tool_names(tools)`; wakes end with `outcome="ended"` | The final wake turn keeps `end_wake` in the offered tools. |
| A7 (`:478-530`) | For each wake request, `json.loads(messages[-1]["content"]) == wake_start.observation`; observation keys are exactly `{roster, opening_plan, workers, git_evidence, unread_messages, previous_messages}` | No note in a wake's first turn. No new observation key (the budget goes in the second message, Section 3.4). |
| A8 (`:533-578`) | `tool_names(tools) == {"spec_verdict"}` for spec check | Spec check does not use `_run_tools`; unchanged. |
| A11 (`:640-726`) | `opening.tool_calls == 1`; `metrics.config.prompt_ids == agent_coordinator_start.prompt_ids`; `usage.coordinator.calls == count(request events)` | New `prompt_ids` keys are allowed because both sides come from the same dict. The final turn is a normal `_call`, so usage and request counts stay equal. |
| `test_opening_failure_is_passive` (unit) | With `opening_max_tool_calls=1`: `outcome="failed"`, no bus messages, `len(complete.inputs) == 1` for a `list_dir` response, a malformed `submit_plan` and a `TimeoutError` | The reserve is taken inside the cap (no extra call after the cap). With a cap of 1 the first turn is the final turn; `list_dir` is rejected as not offered and counted; the loop ends. |
| `test_wake_limits_blind_observation_and_compacts_prior_tools` (unit) | `wake_max_tool_calls=4`; one turn with 5 calls yields `tool_calls == 4`, two messages delivered, and the second wake's `messages[-1]` is the observation | Finishing tools (`send_message`, `end_wake`) are always counted; only read calls beyond the read allowance are refused without being counted. First turn of each wake has no note. |
| `test_opening_delivers_a_valid_plan_once` (unit) | `prompt_id("opening-plan", config.prompts["opening-plan"])` and `inputs[1][0][-1]["content"] == "alpha\nbeta\n"` | Opening prompt ID formula unchanged; read output unchanged. |
| `test_prompts` (contract) | `prompt_id("wake", p.WAKE_PROMPT)` equals `"wake-v1:" + sha256(text)[:12]` | Computed from the constant, so new prompt text passes. |
| `agent_config.json` fixture (contract) | Exact `AgentCoordinatorConfig` field set | No new config fields. Reserves are module constants. |
| `test_agent_coordinator_harness.py:203` | `usage.coordinator.calls == 4` | Scripted coordinator finishes in one turn per phase; unchanged. |
| `test_bench_summary` (`tests/test_metrics.py:28-105`) | `summary.agent_coordinator.usage[actor] == 2 × metrics.usage[actor]` for every key in the fixture | Add `reported_cost_usd` to the fixture's `coordinator_gate` usage; the doubled expectation then covers the new key. |

### 2.4 Budget and wrap-up behavior at `ac202ddc`

`_run_tools` (`agent_coordinator.py:525-567`) calls the model while
`run.calls < max_calls` and the deadline has not passed. The same `tools` list is offered
in every turn; a turn's calls are cut to `max_calls - run.calls`; the loop returns when a
call sets `done`, when the model returns no calls, or when the cap is reached. The prompts
(`agent_coordinator_prompts.py:10-17`) state no budget. When the cap is reached in the
opening the result is `outcome="failed"`; in a wake it is `outcome="exhausted"` with no
message. Both occurred in the smoke (F1, F2).

`_OpenAIComplete.request` sends `tool_choice="auto"`, and the smoke environment sets
`COOPER_SEND_TOOL_CHOICE=false` for Tinker. The design therefore restricts the offered
`tools` list in the final turn and does not depend on `tool_choice`.

### 2.5 Time budget

Only the call cap triggers the final turn. A phase that reaches its time limit
(`opening_time_limit_s` 180, `wake_time_limit_s` 90) still ends without a plan or message.
The smoke did not reach either time limit. Not changed in this PR; the AWS smoke reports
`opening.seconds` and wake `outcome="timeout"` counts (Section 4.8).

### 2.6 `summary.json` cost field

`metrics.usage.coordinator_gate.reported_cost_usd` exists per pair
(`agent_coordinator.py:1081`) and is dropped by `_write_summaries`. Fix in Section 3.8.

### 2.7 `team_worker_steps` and `total_steps`

| Counter | Source | Unit |
| --- | --- | --- |
| `RunResult.total_steps` | Sum of `agent.n_calls` (`mini_swe_worker.py:664`) | Worker model calls |
| `metrics.agent_coordinator.team_worker_steps` | `AgentCoordinator.observe` from `parse_steps` (`structural_signals.py:89-130`) | Executed `bash` actions seen by the poller |

A model call with two `bash` actions adds 2 to `team_worker_steps` (main: 81 > 80). A
call with only `send_message`, a non-executed action, and the actions after the last poll
before a `LimitsExceeded` exit add 0 (random: 73 < 80). The random arm draws its schedule
over `S = team_worker_steps` of the matched run and counts with the same parser, so both
arms use one unit. No code change. The spec (Section 8.1) gains one sentence that states
the unit. A random arm that observes fewer steps than `S` reports the undrawn remainder in
`random_unused`.

### 2.8 Completion gate and `ruff`

`verification.validate` (used by `--completion-gate` and `--presub-merge`) runs the
discovered build command; for a repository with a `Makefile` this is `make` with the
default target (`verification.py:37-45`). In the `huggingface_datasets` image the default
target calls `ruff`, which is absent, so every build check fails. The same validator runs
in all four arms, including `det_main`. Changing it alters the `det_main` protocol and
its comparability with the polar-ext groups, and it sits outside the agent-coordinator
modules. This plan makes no change and records the affected pairs (decision D3).

## 3. Fix PR specification

### 3.1 Scope

| Item | Value |
| --- | --- |
| Branch | `cameron-chen/agent-coord-tool-budget-fix` from `ac202ddc` |
| Target | `cameron-chen/goal-agentic-coordinator` |
| Title | `fix: reserve agent coordinator wrap-up turn and clarify tools` |
| Edited | `src/cooperagents/agent_coordinator.py`, `src/cooperagents/agent_coordination_types.py`, `src/cooperagents/agent_coordinator_prompts.py`, `scripts/bench_compare.py`, `tests/test_agent_coordinator.py`, `tests/test_metrics.py`, `src/cooperagents/reader_qualification.json`, `src/cooperagents/direct_capture_qualification.json`, `docs/AGENTIC_COORDINATOR_SPEC.md` |
| Not edited | `tests/protected/**`, `scripts/check_protected_tests.py`, `CooperBench/`, `src/cooperagents/vendor/**`, `src/cooperagents/workers/**`, `src/cooperagents/verification.py`, `src/cooperagents/harness.py`, `AgentCoordinatorConfig` fields, gate and signal code |
| Worker held constant | Worker prompt and tool list unchanged (protected A9). |

### 3.2 Prompt text (`agent_coordinator_prompts.py`)

The first sentence of each prompt is kept. Exact new constants:

```python
TOOL_RULES = (
    "Tool rules: paths are relative to the repository root; use \".\" for the root. "
    "search_code matches one fixed string (git grep -F); regular-expression syntax such as |, .* or \\b "
    "is matched literally, so search one identifier or phrase per call. "
    "rev accepts only the base_sha or a worker snapshot sha listed in git_evidence; read tools default to base_sha."
)
OPENING_PLAN_PROMPT = (
    "Inspect the base checkout and propose ownership, interfaces and risks for the workers. "
    "Submit a plan grounded in evidence; ask workers to verify and correct it.\n"
    + TOOL_RULES + "\n"
    "Budget: the user message states tool_call_budget. Every tool call, including a rejected one, uses one unit. "
    "The last call is reserved for submit_plan: when one call remains, only submit_plan is offered. "
    "When the evidence is incomplete, submit the plan with the evidence collected so far and record the gaps in risks."
)
WAKE_PROMPT = (
    "Review the team state and inspect committed snapshots as needed. "
    "Send concise, actionable coordination messages when useful, then end the wake.\n"
    + TOOL_RULES + "\n"
    "Budget: the second message states tool_call_budget. At most 2 send_message calls per wake. "
    "The last 2 calls are reserved for send_message and end_wake: when two calls remain, only those tools are offered."
)
OPENING_BUDGET_WARNING = (
    "[remaining_tool_calls: {remaining}] One read call remains. The next turn offers only submit_plan."
)
OPENING_FINAL_TURN = (
    "[remaining_tool_calls: {remaining}] Final turn: the read budget is exhausted. "
    "Call submit_plan now with the evidence collected so far."
)
WAKE_BUDGET_WARNING = (
    "[remaining_tool_calls: {remaining}] One read call remains. The next turn offers only send_message and end_wake."
)
WAKE_FINAL_TURN = (
    "[remaining_tool_calls: {remaining}] Final turn: the read budget is exhausted. "
    "Send any needed coordination message with send_message, then call end_wake."
)
BUDGET_NOTES_PROMPT_ID = prompt_id("budget-notes", json.dumps(
    [OPENING_BUDGET_WARNING, OPENING_FINAL_TURN, WAKE_BUDGET_WARNING, WAKE_FINAL_TURN],
    ensure_ascii=False, separators=(",", ":"),
))
```

`SPEC_CHECK_PROMPT`, the gate texts and `GATE_PROMPT_ID` are unchanged, so the
decision-model requests stay identical to the smoke.

### 3.3 Tool schemas (`agent_coordination_types.py`)

`_tool(name, description, properties, required)` gains a `description` argument; each
property may carry a `description`. `TOOL_SCHEMAS` keeps its order and tool names. Exact
texts:

| Tool | Tool description | Parameter descriptions |
| --- | --- | --- |
| `list_dir` | `List one directory of a committed tree (git ls-tree). Returns at most 400 names.` | `path`: `Directory relative to the repository root, for example "src/pkg"; use "." for the root.` `rev` (optional, new): `base_sha or a worker snapshot sha from git_evidence. Default: base_sha.` |
| `read_file` | `Read lines start to end of one committed file (git show rev:path). Returns at most 400 lines and 16000 characters.` | `path`: `File path relative to the repository root, for example "src/pkg/core.py".` `start`: `First line, 1-based.` `end`: `Last line, inclusive.` `rev` (optional, new): as `list_dir` |
| `search_code` | `Search committed files for one fixed string (git grep -n -F). Regular-expression syntax is matched literally. Returns path:line:text rows, at most 20 per file and 200 in total.` | `pattern`: `One fixed string, one line, for example "def load_dataset".` `path_glob`: `Git glob relative to the repository root, for example "src/**/*.py"; use "." for the whole repository.` `rev` (optional, new): as `list_dir` |
| `git_show` | `Show a committed snapshot. Without path: commit summary and changed files. With path: the diff of that file from base_sha to rev.` | `rev`: `base_sha or a worker snapshot sha from git_evidence, as full hex. Names such as HEAD are rejected.` `path`: `Optional file path relative to the repository root.` |
| `send_message` | `Send one message to a worker, or to every worker with recipient "group". At most 2 calls per wake.` | `recipient`: `A worker ID from the roster, or "group".` `content`: `Concise, actionable message, at most 1500 characters.` |
| `submit_plan` | `Deliver the opening plan to every worker and end the opening. workers has exactly one entry per roster worker ID.` | `schema`: `Always "opening-plan/1".` `version`: `Always 1.` |
| `spec_verdict` | `Return ACCEPT or REJECT for the frozen diff. Each finding quotes an exact substring, at most 300 characters, of the supplied feature text; any other quote invalidates the verdict.` | none |
| `end_wake` | `End the wake after the coordination messages, or with a note when no message is needed.` | `note`: `One-line reason.` |

`ReadOnlyGit.read`: for `list_dir`, `read_file` and `search_code`, `rev = args.get("rev")`
when present, else the snapshot of `worker` when given, else `base_sha`. The existing
membership check (`:184-187`) validates the chosen value. `_run_wake` stops passing
`worker=target` (decision D2), so a wake read without `rev` reads `base_sha`. The `worker`
keyword of `ReadOnlyGit.read` stays for the existing unit test at
`test_agent_coordinator.py:181`.

### 3.4 Tool loop (`AgentCoordinator._run_tools`)

New keyword arguments: `finish: list[dict]` (tools offered in the final turn) and
`reserve: int`. Callers:

| Phase | `tools` | `finish` | `reserve` | Read allowance |
| --- | --- | --- | --- | --- |
| `opening-plan` | `[*_READ_TOOLS, SUBMIT_PLAN_TOOL]` | `[SUBMIT_PLAN_TOOL]` | 1 | `opening_max_tool_calls - 1` (15) |
| `wake` | `[*_READ_TOOLS, SEND_MESSAGE_TOOL, END_WAKE_TOOL]` | `[SEND_MESSAGE_TOOL, END_WAKE_TOOL]` | 2 | `wake_max_tool_calls - 2` (4) |

Algorithm:

```python
while run.calls < max_calls and not self._stop.is_set() and time.monotonic() < deadline:
    remaining = max_calls - run.calls
    final = remaining <= reserve
    offered = finish if final else tools
    note = []
    if run.turns and remaining <= reserve + 1:          # never in a phase's first turn
        text = (FINAL if final else WARNING)[phase]      # constants of Section 3.2
        note = [{"role": "user", "content": text.format(remaining=remaining)}]
    calls = self._call(messages + run.turns + note, offered, phase, deadline, run.usage)
    ...                                                  # existing empty / stop / deadline checks
    allowed = {tool["function"]["name"] for tool in offered}
    for call in calls[:remaining]:
        name, raw, args = parse (existing)
        if name not in allowed:                          # counted
            result = f"Error: {name} is not offered in this turn; offered: {', '.join(sorted(allowed))}"
        elif name in READ_NAMES and run.calls >= max_calls - reserve:   # not counted
            result = READ_EXHAUSTED[phase]
        else:                                            # counted, existing execute()
            ...
```

Rules:

1. The note is ephemeral. The note is included in the request (and so of the recorded `request`
   row) but is not appended to `run.turns`, so it never enters the wake history or the
   compacted summaries.
2. A read call beyond the read allowance inside a non-final turn is answered with
   `READ_EXHAUSTED` and is not counted. Text: opening `Error: read budget exhausted; the
   remaining call is reserved for submit_plan.`; wake `Error: read budget exhausted; the
   remaining calls are reserved for send_message and end_wake.` This keeps the reserved
   calls available when a model batches several reads in one turn.
3. A call to a tool outside `offered` is counted. In the final turn this is the only way
   a read call can consume the reserve.
4. The special case `if phase == "opening-plan" and call.get("name") == "submit_plan": done = True`
   on error is removed. An invalid plan returns its error; the loop continues while calls
   remain, and the final turn offers `submit_plan` again.
5. Budget statement in the first user message of each phase:
   - opening: `self._user({"roster": ..., "base_sha": ..., "tool_call_budget": opening_max_tool_calls})`;
   - wake: the second message becomes `self._user({"opening_plan": ..., "tool_call_budget": wake_max_tool_calls})`.
   The wake observation (last message) is unchanged (protected A7).
6. `tool_calls` in `opening_plan` and `wake_end` events keeps its meaning: counted calls.
   The final turn is a normal `_call`, so `usage.coordinator.calls` equals the number of
   `request` rows (protected A11).
7. The final turn's tool list differs from earlier turns, so a provider prompt cache may
   miss that one request. All other requests keep the same prefix; the note is appended
   at the end.
8. Edge cases: with `opening_max_tool_calls = 1` the first turn is final and offers only
   `submit_plan`. With `wake_max_tool_calls <= 2` a wake has no read call. No validation is
   added; the defaults (16, 6) are unchanged.

### 3.5 Error strings

| Location | New text (`Error: ` prefix added by `_run_tools`) |
| --- | --- |
| `_path` | `invalid repository path` (**unchanged**) |
| `_path` | `repository path is required; use a path relative to the repository root` |
| `ReadOnlyGit.read` | `revision is outside the available snapshots; use base_sha or a worker snapshot sha from git_evidence` |
| `ReadOnlyGit.read` | `invalid line range; start and end are integers with 1 <= start <= end` |
| `ReadOnlyGit.read` | `Git object query failed; the path may not exist at this revision` |
| `ReadOnlyGit.read` | `unknown read-only tool` (unchanged; now reached only through direct calls, because `_run_tools` rejects tools that are not offered) |
| `send_message` | `message limit reached: at most 2 send_message calls per wake; call end_wake` |
| `send_message` | `unknown message recipient; use "group" or one of: <sorted roster IDs>` |
| `submit_plan` decode | `invalid opening plan: <decoder message>` |
| `submit_plan` roster | `opening plan must cover the worker roster: expected <sorted roster IDs>, received <sorted keys>` |
| `submit_plan` version | `opening plan version must be 1` (split from the roster check) |

Every new text keeps the substring that unit tests match (`path`, `revision`,
`line range`).

### 3.6 Empty and truncated outputs

Applied in `AgentCoordinator._read_tool` after `ReadOnlyGit.read` returns, so
`ReadOnlyGit` unit tests keep their exact values. `<rev12>` is the first 12 characters of
the revision used.

| Tool and condition | Returned text |
| --- | --- |
| `search_code`, empty | `No matches for the fixed string <pattern as JSON> under <path_glob as JSON> at <rev12>. search_code does not interpret regular expressions.` |
| `search_code`, 200 rows | output + `\n[output limited to 200 rows; narrow path_glob or the pattern]` |
| `read_file`, empty | `No lines <start>-<end> in <path> at <rev12>; the file is shorter or empty.` |
| `read_file`, 400 lines or 16000 characters | output + `\n[output limited to 400 lines or 16000 characters; request a narrower range]` |
| `list_dir`, empty | `Directory <path or "."> at <rev12> is empty.` |
| `list_dir`, 400 entries | output + `\n[output limited to 400 entries; list a subdirectory]` |
| `git_show` with `path`, empty | `No difference in <path> between base_sha and <rev12>.` |

Protected A1 reads one line (`"base two\n"`): not empty, not truncated, returned
unchanged.

### 3.7 Prompt IDs

`AgentCoordinator._prompt_ids` gains two keys:

- `"tools"`: `prompt_id("tools", json.dumps(TOOL_SCHEMAS, sort_keys=True, separators=(",", ":")))`;
- `"budget-notes"`: `BUDGET_NOTES_PROMPT_ID`.

The existing keys and their formula are unchanged; `opening-plan` and `wake` change value
because their text changes. `_call` keeps `prompt_id=self._prompt_ids[phase]`.

### 3.8 `summary.json`

In `_write_summaries` (`bench_compare.py:511-522`), after the token sums:

```python
usage["coordinator_gate"]["reported_cost_usd"] = sum(
    metrics["usage"]["coordinator_gate"].get("reported_cost_usd", 0.0) for metrics in coordinator_metrics
)
```

The printed line gains ` gate reported_cost=$<value:.6f>` at its end. Earlier printed
lines are unchanged.

### 3.9 Tests in the fix PR

Edits to non-protected tests only.

| Test (`tests/test_agent_coordinator.py` unless noted) | Checks |
| --- | --- |
| `test_opening_reserves_submit_plan_turn` | Scripted model returns `read_file` until `submit_plan` is the only offered tool, then submits. With `opening_max_tool_calls=4`: calls 1–3 offer 5 tools, call 4 offers `{"submit_plan"}`; the request for call 3 ends with the `OPENING_BUDGET_WARNING` text (remaining 2) and call 4 with `OPENING_FINAL_TURN` (remaining 1); `outcome="delivered"`, `tool_calls=4`. |
| `test_opening_batched_reads_keep_reserve` | One turn returns 6 `read_file` calls with `opening_max_tool_calls=4`: 3 executed, the rest answered `READ_EXHAUSTED` and not counted; next turn offers only `submit_plan`. |
| `test_opening_invalid_plan_retries` | First `submit_plan` has a wrong roster; the error lists expected and received IDs; second `submit_plan` is delivered. |
| `test_wake_reserves_message_turn` | `wake_max_tool_calls=6`, model reads until restricted; the final turn offers exactly `{"send_message", "end_wake"}`; message delivered; `outcome="ended"`; notes absent from the next wake's history. |
| `test_unoffered_tool_is_counted_error` | `send_message` in the opening returns `Error: send_message is not offered in this turn; offered: ...` and counts. |
| `test_read_tool_messages` | Empty search, empty diff, out-of-range read, truncation notices, and `rev` selection through `_read_tool`; `git.read` values unchanged. |
| `test_tool_schema_descriptions` | Every tool and parameter has a non-empty description; `search_code` mentions `fixed string`; tool names and required fields unchanged. |
| `test_prompt_ids_cover_tools_and_notes` | `"tools"` and `"budget-notes"` keys present; changing a description changes `"tools"`. |
| `tests/test_metrics.py::test_bench_summary` | Fixture `coordinator_gate` gains `reported_cost_usd=0.001`; the doubled expectation and printed line include it. |
| Existing tests | Unchanged and passing: `test_opening_failure_is_passive`, `test_wake_limits_blind_observation_and_compacts_prior_tools`, `test_opening_delivers_a_valid_plan_once`, `test_tool_rejection_does_not_execute`, the `git.read` test at `:180-193`. |

### 3.10 Acceptance commands

```sh
uv run pytest -q tests/protected
uv run pytest -q tests/test_agent_coordinator.py tests/test_agent_coordinator_harness.py \
  tests/test_agent_coordination_contract.py tests/test_metrics.py tests/test_structural_signals.py \
  tests/test_system1_gate.py
python3 scripts/check_protected_tests.py --base ac202ddc
git diff --name-only ac202ddc...HEAD -- tests/protected scripts/check_protected_tests.py CooperBench \
  src/cooperagents/vendor src/cooperagents/workers src/cooperagents/harness.py src/cooperagents/verification.py
                                        # must print nothing
python3 scripts/qualify_direct_capture.py   # all three modules are in producer_identity
python3 scripts/qualify_published_reader.py
git diff --exit-code src/cooperagents/reader_qualification.json src/cooperagents/direct_capture_qualification.json  # clean after commit
uv run pytest -q tests/test_published_profiles.py tests/test_direct_capture.py
uv run pytest -q                            # full suite; failures must match the known pre-existing set
```

Known pre-existing failures at `af9b42ff` and later: the `_TEST_PATH` import error in
`tests/test_training_selection.py` and failures in `tests/test_trajectory.py`. The PR
description lists the full-suite result against that set.

### 3.11 Spec updates in the same PR (`docs/AGENTIC_COORDINATOR_SPEC.md`)

| Section | Change |
| --- | --- |
| §4.6 item 1 | Add the `tools` and `budget-notes` prompt IDs and the four note constants. |
| §5.2 | Tool table: optional `rev` on `list_dir`, `read_file`, `search_code` with default `base_sha`; schema descriptions; empty-output and truncation texts of Section 3.6; the "not offered in this turn" error. Replace "Any other tool name returns an error result" with the offered-tools rule. |
| §5.3 | Budget statement in the opening user message; reserved final turn (reserve 1); `READ_EXHAUSTED` rule; invalid plans retry while calls remain. Failure handling: `failed` now means the final turn produced no valid plan, or a timeout or transport error. |
| §5.4 step 3 | Reserve 2; warning and final-turn notes; final turn offers `send_message` and `end_wake`; reads default to `base_sha`; budget in the second message. |
| §8.1 | One sentence: `team_worker_steps` counts executed `bash` actions observed by the poller and differs from `RunResult.total_steps` (worker model calls). |
| §8.3 | `summary.json` and the printed line include `coordinator_gate.reported_cost_usd`. |
| §12.1 | Add the decisions taken in Section 5 of this plan. |

## 4. AWS smoke and evaluation workflow

### 4.1 Host facts

| Item | Value | Source |
| --- | --- | --- |
| Instance | `i-0f892273b477977c7`, Name `changyu-sandbox-eval`, `m6a.16xlarge` (64 vCPU, 256 GiB), `us-west-2` | `aws ec2 describe-instances`, 2026-11-23 |
| State | `stopped` | same |
| Shutdown behavior | `stop` | `describe-instance-attribute instanceInitiatedShutdownBehavior` |
| Instance profile | `polar-ext-repair-sandbox-ssm` (SSM access) | same |
| Runtime | systemd, cgroup v2, Apptainer, controller user `polar-sandbox` | polar-ext `cooperbench_coordination/aws/README.md` |
| Protected paths on the host | `/opt/polar-repair-assets`, `/opt/polar-repair-publications`, `/opt/polar-cache/polar-repair-assets`, `/opt/polar-cache/polar-repair-publications`, `/srv/polar-ext/assets/grader`, `/srv/polar-ext/state/cbc-backfill-*`, and all of `/srv/polar-ext/state/cbc-study*` | same README; this plan adds the polar-ext study roots |
| Staged contents | Not inspected: the instance was stopped and starting it is billable. Phase A0 inspects them. | |

Root for this work: `/srv/polar-ext/state/agent-coord/` (new, owned by `polar-sandbox`,
mode 0700). Nothing is written outside it except Apptainer's own cache under the
controller user's home.

### 4.2 Phase A0: start and inventory (read-only)

Operator, from the laptop:

```sh
aws ec2 start-instances --region us-west-2 --instance-ids i-0f892273b477977c7
aws ec2 wait instance-status-ok --region us-west-2 --instance-ids i-0f892273b477977c7
aws ssm describe-instance-information --region us-west-2 \
  --filters Key=InstanceIds,Values=i-0f892273b477977c7 --query 'InstanceInformationList[0].PingStatus'
```

Inventory script, sent with `aws ssm send-command --document-name AWS-RunShellScript`
(runs as root; prints no file contents and no environment):

```sh
set -u
id polar-sandbox; nproc; free -g | head -2; df -h / /srv /opt/polar-cache 2>/dev/null
command -v apptainer git uv python3 docker || true; apptainer --version || true
uname -m; ls /proc/sys/fs/binfmt_misc/ | grep -i -e aarch64 -e arm || echo "no arm64 binfmt"
ls -la /srv/polar-ext/state/ /opt/polar-cache/ 2>/dev/null
ls /opt/polar-cache/cbc-images/ 2>/dev/null | grep -i -e outlines -e dspy -e go-chi -e datasets || true
ls -la /srv/polar-ext/state/cbc-study/assets/ 2>/dev/null
test -f /srv/polar-ext/state/cbc-study/assets/cooperbench-63b9d44.tar.gz && \
  sha256sum /srv/polar-ext/state/cbc-study/assets/cooperbench-63b9d44.tar.gz
p=/home/polar-sandbox/.config/polar-ext/tinker.env
test -f "$p" && stat -c '%a %U' "$p" && grep -c -E '^(TINKER|OPENAI)_API_KEY=' "$p"
cat /etc/sudoers.d/polar-cbc-poweroff 2>/dev/null | grep -c 'systemctl poweroff'
for url in https://openrouter.ai/api/v1/models https://tinker.thinkingmachines.dev https://github.com; do
  printf '%s ' "$url"; curl -sS -o /dev/null -w '%{http_code}\n' --max-time 10 "$url" || echo fail
done
systemctl list-units 'cbc-*' --no-legend || true
pgrep -a -u polar-sandbox -f 'apptainer|python' || echo "no controller processes"
```

Record the output in `/srv/polar-ext/state/agent-coord/logs/a0-inventory.txt` and on the
laptop. Stop if a `cbc-*` controller or Apptainer process from another study is running;
report and wait for a decision.

### 4.3 Staging

All steps run as `polar-sandbox` (`runuser -u polar-sandbox --` under SSM).

| Input | Action | Check |
| --- | --- | --- |
| cooperagents | `git clone` the GitHub origin into `/srv/polar-ext/state/agent-coord/cooperagents`; `git checkout <fix-PR merge SHA>`. Use the existing owner-only GitHub read credential if the repository is private. | `git rev-parse HEAD` equals the merge SHA; `git status --porcelain` is empty |
| Python environment | `cd cooperagents && UV_PROJECT_ENVIRONMENT=/srv/polar-ext/state/agent-coord/venv uv sync --locked` | `uv run pytest -q tests/protected` passes on the host |
| CooperBench `63b9d44` | Preferred: extract the staged archive `/srv/polar-ext/state/cbc-study/assets/cooperbench-63b9d44.tar.gz` (read-only source) into `/srv/polar-ext/state/agent-coord/CooperBench` after its SHA-256 matches polar-ext's `COOPERBENCH_ARCHIVE_SHA`. Fallback: `git clone https://github.com/cooperbench/CooperBench` and `git checkout 63b9d44d`. Apply no patch. Install it into the venv with `uv pip install -e` (the Apptainer evaluator imports `cooperbench.eval`). | `git -C CooperBench rev-parse HEAD` starts with `63b9d44` (or the archive hash matches); `python -c "import cooperbench.eval"` |
| Task images | For the 7 images of the 10 pairs (`akhatua/cooperbench-{dottxt-ai-outlines:task1655, dottxt-ai-outlines:task1706, dspy:task8394, go-chi:task26, go-chi:task56, huggingface-datasets:task3997, huggingface-datasets:task6252}`, from `eval/dataset.image_name`): reuse a SIF from `/opt/polar-cache/cbc-images/` when its recorded digest identifies the same image; else `apptainer build /srv/polar-ext/state/agent-coord/images/<name>.sif docker://<image>`. | `apptainer exec <sif> true` succeeds; `sha256sum` recorded |
| Image manifest | `/srv/polar-ext/state/agent-coord/images.json`: `{"<image name>": "<absolute SIF path>"}` | All 7 keys present; paths exist |
| Scratch | `/srv/polar-ext/state/agent-coord/scratch` | ≥ 200 GiB free (estimate: 4 arms × 5 concurrent teams × 4 environments; Phase A1 measures the per-team peak) |

`measure.sh` notes that most flash images are arm64-only. On this `x86_64` host such an
image needs an arm64 binfmt handler for Apptainer, or an amd64 image variant. A0 reports
the binfmt state; image build reports each image's architecture
(`apptainer exec <sif> uname -m`). An image that does not run is a stop condition for the
pairs that use it (decision D5).

### 4.4 Credentials

Sources on the laptop: `/Users/cameron_chen/Desktop/Code/cooperator/polar-ext/.env`.

| Variable on the host | Source key in that file | Use |
| --- | --- | --- |
| `OPENAI_API_KEY` | `TINKER_API_KEY` | Workers and System-2 coordinator through Tinker |
| `OPENROUTER_API_KEY` | `OPENAI_API_KEY` | Decision model through OpenRouter |

Rules:

1. Secret values never appear in an SSM command, an SSM parameter, a command line,
   a log, a Git checkout, or a result file. SSM `send-command` parameters are stored in
   the command history and CloudTrail.
2. Tinker key: reuse `/home/polar-sandbox/.config/polar-ext/tinker.env` when A0 shows it
   exists with mode 600 and one key line. The run script reads it with the same
   `grep | cut` method as the local smoke script and exports it as `OPENAI_API_KEY`.
3. OpenRouter key: the operator writes a two-line file locally (`umask 077`, in a
   temporary directory) and copies it over an SSM-tunneled SSH session:
   `scp -o ProxyCommand='aws ssm start-session --region us-west-2 --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p' <file> polar-sandbox@i-0f892273b477977c7:/home/polar-sandbox/.config/agent-coord/openrouter.env`,
   then deletes the local file. If SSH keys for `polar-sandbox` are not set up, the
   operator creates the file in an interactive `aws ssm start-session` shell with an
   editor. Mode 600, owner `polar-sandbox`, outside `/srv/polar-ext/state/agent-coord/`.
4. Non-secret environment (`OPENAI_BASE_URL`, `AZURE_OPENAI_DEPLOYMENT`, `COOPER_*`
   sampling values, `COOPER_RUNTIME=apptainer`, `COOPER_IMAGE_MANIFEST`, `COOPER_SCRATCH`,
   `COOPERBENCH_DIR`, `ENV_FILE=/dev/null`) lives in
   `/srv/polar-ext/state/agent-coord/run.env`, copied from the local smoke script with the
   two key lines removed.
5. After each phase, scan outputs for both key values without printing them:
   `grep -rlF -f <(keys only) <run dirs>` with `gzip -dc` for `trajectory.jsonl.gz`; the
   pass condition is no file listed.

### 4.5 Run wrapper and auto-stop

One root script per phase, sent through `AWS-RunShellScript` with
`executionTimeout` set to the phase deadline:

```sh
#!/usr/bin/env bash
set -euo pipefail
deadline_min=$1; phase=$2                     # e.g. 720 a2
shutdown -h "+${deadline_min}" "agent-coord ${phase} deadline"     # safety net; shutdown behavior is stop
trap 'shutdown -c || true; shutdown -h +5 "agent-coord ${phase} finished"' EXIT
runuser -u polar-sandbox -- bash /srv/polar-ext/state/agent-coord/bin/run_${phase}.sh \
  >> /srv/polar-ext/state/agent-coord/logs/${phase}.log 2>&1
```

The five-minute delay after completion leaves time to start result retrieval. To keep the
host running after a phase (for example between A1 and A2), the operator runs
`sudo shutdown -c` through SSM before the delay expires. `instanceInitiatedShutdownBehavior`
is `stop`, so the instance and its EBS volume are kept.

### 4.6 Phase A1: 1-pair verification

Purpose: verify the fix PR on the host with the same pair as the local smoke, and verify
the Apptainer runtime and evaluator.

```sh
set -a; . /srv/polar-ext/state/agent-coord/run.env; set +a
export OPENAI_API_KEY=$(grep -E '^(TINKER|OPENAI)_API_KEY=' ~/.config/polar-ext/tinker.env | head -1 | cut -d= -f2- | tr -d "\"'")
export OPENROUTER_API_KEY=$(grep -E '^OPENROUTER_API_KEY=' ~/.config/agent-coord/openrouter.env | cut -d= -f2- | tr -d "\"'")
cd /srv/polar-ext/state/agent-coord/cooperagents
COMMON="--pairs huggingface_datasets_task:6252:4,6 --team-only --max-agents 2 --no-seed --coop-tools --git-share
  --repair-integrator --focused-repair --repair-attempts 2 --completion-gate --presub-merge
  --step-limit 1000 --agent-time-limit 3600 --record-trajectory --persist-worker-patches
  --concurrency 1 --eval-concurrency 2 --log-dir /srv/polar-ext/state/agent-coord/logs"
uv run python scripts/bench_compare.py $COMMON --agent-coordinator --agent-gate model --team-name a1-agent_main
```

Pass conditions (all from `result.json` and `trajectory.jsonl.gz`):

| Check | Condition |
| --- | --- |
| Opening | `opening.outcome = "delivered"`; the last opening request offers only `submit_plan` if `tool_calls` reached 16 |
| Wakes | Every `wake_end` with `tool_calls = 6` has `messages_sent ≥ 1` or `outcome = "ended"` |
| Search | No `search_code` response equals `""` |
| Gate | `gate.model_calls > 0`; `fallbacks` all 0; `usage.coordinator_gate.reported_cost_usd > 0` |
| Notes | Every request carrying a budget note is the last or second-to-last turn of its phase |
| Evaluation | `eval.json` exists for the pair (Apptainer evaluator) |
| `summary.json` | Contains `coordinator_gate.reported_cost_usd` |
| Secrets | Scan of Section 4.4 rule 5 lists no file |
| Resources | Record peak scratch use and peak memory for one team (`du -sh` of scratch, `free -g` sampled every 60 s by the run script) |

If any check fails: stop, keep the instance running for inspection only on explicit
instruction, otherwise let the wrapper stop it, and report the failing check with the
event rows.

### 4.7 Phase A2: 10-pair 4-arm smoke

Pairs: the fixed set of `scripts/measure.sh`:

```text
dottxt_ai_outlines_task:1655:6,7 dottxt_ai_outlines_task:1655:7,10 dottxt_ai_outlines_task:1706:4,6
dottxt_ai_outlines_task:1706:5,8 dspy_task:8394:3,4 dspy_task:8394:3,5 go_chi_task:26:1,2
go_chi_task:56:1,5 huggingface_datasets_task:3997:2,4 huggingface_datasets_task:6252:4,6
```

All arms use `$COMMON` of Phase A1 with `--pairs <the 10 pairs>`, `--concurrency 5`,
`--eval-concurrency 8`. These are the polar-ext system-arm flags
(`runner.py:protocol_arguments`), with `--concurrency` raised for the shared host.

| Order | Arm | Extra flags | Team name |
| --- | --- | --- | --- |
| 1 (parallel) | `det_main` | `--deterministic-coordinator` | `a2-det_main` |
| 1 (parallel) | `agent_main` | `--agent-coordinator --agent-gate model` | `a2-agent_main` |
| 1 (parallel) | `agent_rule_main` | `--agent-coordinator --agent-gate rule` | `a2-agent_rule_main` |
| 2 (after `agent_main` has a `result.json` for every pair) | `agent_rand_main` | `--agent-coordinator --agent-gate random --agent-random-match a2-agent_main` | `a2-agent_rand_main` |

Each arm runs as its own `bench_compare.py` process, which scores its own run with the
Apptainer evaluator after generation (`COOPER_RUNTIME=apptainer`). If evaluation fails
after generation, rerun that arm with `--resume`; generation is skipped for pairs with
`result.json`. Phase deadline: 12 hours (`deadline_min=720`), to be revised from the A1
duration.

Concurrency: phase 1 runs 15 teams at once (3 arms × 5), each with two workers, a
coordinator environment (agent arms) and a merge environment. Reduce `--concurrency`
when A1's resource record shows that 15 teams exceed memory or scratch, or when Tinker
returns rate-limit errors.

### 4.8 Results and report

Per arm, from `summary.json`, `result.json`, `eval.json`:

| Metric | Arms |
| --- | --- |
| Pairs passed, features passed | all |
| Completion time n, p50, p90, p95, p99; total worker cost | all |
| Opening outcomes, opening tool calls and seconds | agent arms |
| Wakes, messages sent, wake outcomes (`ended`, `exhausted`, `timeout`, `stale`) | agent arms |
| Gate candidates, wakes, skips, `by_source`, fallbacks, `p_wake` p50/p90, latency | `agent_main`, `agent_rule_main` |
| `random_planned`, `random_unused` | `agent_rand_main` |
| `spec_check` checked, accepted, rejected, errors | agent arms |
| Base completion-gate rejections by error head (identifies `ruff` and similar cases) | all |
| Coordinator and gate tokens, `reported_cost_usd` | agent arms |
| Probe latency p50/p95 against median worker step time | agent arms |

Retrieval: `tar` the four run directories and the logs on the host; copy them to the
laptop through the SSM-tunneled `scp` of Section 4.4, or to S3 when a bucket is
configured. Run the secret scan before the archive leaves the host.

Contrasts reported at n = 10 with pair-level differences only; no significance claim at
this size. Primary: `agent_main − agent_rand_main`; secondary: `agent_main − agent_rule_main`
and `agent_main − det_main`.

### 4.9 Stop conditions

| Condition | Action |
| --- | --- |
| Tinker or OpenRouter: 3 consecutive authentication, 400 or endpoint errors | Stop all arms; report the error and trace without secrets |
| Harness exception or `gate_error` events | Stop; report the trace; no code edit on the host |
| A0 finds another study's controller running | Do not start; report |
| An image does not run on the host | Exclude no pair without a decision (D5); report |
| Scratch free space below 40 GiB | Stop new pairs (`kill` the `bench_compare` processes after the current pair writes `result.json`); report |

## 5. Decisions for review

| ID | Question | Recommendation |
| --- | --- | --- |
| D1 | Reserve sizes: opening 1, wake 2; notes in the last two turns only. | Accept. A note in every turn is excluded by protected A1/A2. |
| D2 | Wake reads default to `base_sha` with optional `rev`, replacing the implicit read of the unnamed wake target's snapshot. | Accept. The model receives every snapshot SHA in `git_evidence`. |
| D3 | `make` default target fails in some task images (`ruff` absent), which blocks the spec check for those pairs in all arms. | No change in this PR; report affected pairs from A2. A later PR may change build discovery for all arms together with a new `det_main` control. |
| D4 | A2 limits: polar-ext protocol values (`--step-limit 1000 --agent-time-limit 3600`) or the local smoke values (`40`, `900`). | Use the polar-ext values; the local values stopped both workers at the step limit. |
| D5 | Images that do not run on the `x86_64` host. | Install an arm64 binfmt handler on the host only with operator approval; otherwise run the affected pairs elsewhere and report them separately. |

## 6. Not in this plan

- Time-based wrap-up (Section 2.5).
- Changes to gate prompts, thresholds, budgets (8/4) or signal definitions.
- Changes to `verification.py` build discovery (D3).
- Validation of `opening_max_tool_calls` and `wake_max_tool_calls` lower bounds.
- polar-ext variant files for the agent arms (spec §10); the AWS smoke uses
  `bench_compare.py` directly.
