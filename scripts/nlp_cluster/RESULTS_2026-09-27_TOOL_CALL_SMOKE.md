# Coordinator tool-call interface smoke — 2026-09-27

The coordinator now sends `send_message` and optional `update_notebook` function
definitions through the OpenAI-compatible API and executes only
`message.tool_calls`. It never parses model-specific tags in `message.content`.
An empty `tool_calls` field is a no-op. The existing whole-batch checks still
reject unknown tools, malformed or duplicate JSON arguments, duplicate recipients,
and failed notebook writes before messages are delivered. Current code truncates
oversized message or notebook content with a visible notice. The smoke runs below
predate that change and did not test truncation.

Local verification: 70 focused coordinator, sampling, trajectory, harness and
budget tests passed; 9 planner tests passed. Focused Ruff, Mypy and
`git diff --check` passed. Repository-wide Ruff still reports five pre-existing
issues in `scripts/fleet/validate_run.py`, `src/cooperagents/adapters/terminalbench.py`
and `tests/test_frozen_harness.py`.

Isolated Stanford NLP Slurm job **17638077** completed on `sphinx1` with exit
`0:0` in 8m22s. It launched Qwen3.5-9B on SGLang 0.5.15.post1 with
`--tool-call-parser qwen3_coder`, `--reasoning-parser qwen3` and NEXTN
speculative decoding. Three OpenAI-compatible requests used `tools` and
`tool_choice=auto`, without `response_format`:

| Request | API result | Elapsed |
| --- | --- | ---: |
| `send_message` to `agent1` | HTTP 200; one named `tool_call` with correct JSON arguments | 22.25s |
| Multiline `update_notebook` | HTTP 200; one named `tool_call`; newlines preserved | 0.495s |
| No action needed | HTTP 200; `tool_calls: null` | 0.245s |

The three raw API responses were also replayed through the local coordinator's
batch validator and action executor. The notebook advanced to v1, the message
reached `agent1`, both workers received the notebook path reminder, and the
no-op made no change (`REAL_RESPONSE_REPLAY_PASSED`).

Run artifacts, including the exact probe, server log and raw responses:
`/nlp/scr/chency/projects/cooperagents/runs/20260927T232434Z-native-tools-e60b47d`.
At the time of this isolated job, the shared Qwen service was not reconfigured;
an earlier request to it returned raw tool markup in `message.content` and no
`tool_calls`. The isolated job validated the parser configuration. The later
shared-service worker run is recorded below; neither run measures CooperBench
score gain.

## Shared-service two-worker smoke

The parser-enabled shared service was started from `polar-ext` commit `0c106c9`
at `http://john2.stanford.edu:63008/v1`. A short request with `tools` and
`tool_choice=auto` returned HTTP 200 and a structured `send_message` call with
the requested recipient and content.

A temporary clean snapshot of this uncommitted cooperagents branch was made at
commit `2bab3c0`; the original branch was not committed or changed for launch.
CPU dummy job **17638178** completed `0:0` in 4m31s. Its `smoke.json` passed,
the updated OpenAI-compatible dummy responder exercised coordinator tool calls,
and both official gold patches passed. Its deliberately incorrect dummy patch
scored 0/2, as expected.

Real job **17638201** used the new shared router on
`go_chi_task:27:3,4`, with two workers, coordinator and notebook enabled,
8 steps per worker, a 300-second worker limit and trajectory collection.
It completed `0:0` in 1m35s; trajectory audit verified one complete pair
journal and two worker histories. The coordinator's initial model response had
`finish_reason=tool_calls` and one valid `update_notebook` call. The notebook
advanced from v0 to v1; both workers received the v1 path notice, executed
`cat /coordination/notebook.md`, and the v1 file contents appeared in each
worker's subsequent model context. No coordinator `send_message` was chosen in
this run; the separate shared-router probe above validated that function.
Both workers hit the deliberately short 8-step limit without submitting a
patch. Official scoring was intentionally skipped; this is an interface and
mount smoke, not evidence of task quality or notebook benefit.

Dummy record:
`/nlp/scr/chency/projects/cooperagents/runs/20260927T235021Z-cooperbench-2bab3c06`.
Real record:
`/nlp/scr/chency/projects/cooperagents/runs/20260927T235810Z-cooperbench-2bab3c06`.
