# Coordinator prompt and structured-output probes — 2026-09-27

This is a historical probe of the earlier JSON-text protocol. The current
tool-call interface and its later smoke are recorded in
[RESULTS_2026-09-27_TOOL_CALL_SMOKE.md](RESULTS_2026-09-27_TOOL_CALL_SMOKE.md).

## Change and scope

Strengthened the single-turn coordinator instructions in `harness.py`: the
coordinator cannot inspect code or wait for replies; it requests inspection by
workers and uses later observations. Initial decisions must leave concrete edit
boundaries pending evidence and confirmation. The prompt distinguishes task
requirements from observed code, proposals from agreement, and worker reports
from verification. It asks for one writer per shared definition, feasible budget
advice, short notebooks and escaped JSON strings. Each worker observation now
includes its actual `feature_id` alongside the common `task_id`.

No agent loop, tools, barrier or automatic reminder was added. The production
client still sends a single user message and does **not** request structured
output. No inference configuration or service source was changed.

Coordinator/sampling tests (17 cases) and harness tests (46 cases) passed. Targeted
Ruff, test formatting and `git diff --check` passed. The new parametrized check
covers notebook on/off, initial-only guidance and task/feature identity. These
checks verify request construction, not model compliance.

## Probe setup

Eight sequential, bounded HTTP requests used the existing NLP service at
`http://john2.stanford.edu:61472/v1`, model `qwen3.5-9b`. Worker logs confirmed
SGLang with `grammar_backend='xgrammar'` and `reasoning_parser=None`.
Temperature 1, top-p .95, top-k 20, presence penalty 1.5 and thinking disabled
matched the earlier smoke. Coordinator requests used a 2000-token completion
limit; the two small control requests used 128, and the JSON-object probe 256.
The client timeout was 60 seconds with no retries.

The tightened initial prompt used the previous smoke's actual feature texts,
task 27, features 3/4, step limit 30 and time budget 300 seconds. The last request
replayed the **old** input that previously produced invalid JSON, adding only
`response_format`. Responses were inspected offline, never executed as actions.
These probes are not a worker run, a pilot or an effect comparison.

Local raw artifacts: `logs/coordinator-protocol-20260927/`, including
`requests.json`, `responses.jsonl`, `schema.json`, `prompt.txt`, `summary.json`,
`probe.py`, `source.diff` and a manifest with base commit and diff SHA256.

## Results

| Request | Result | Seconds |
| --- | --- | ---: |
| Plain-text control, no response format | HTTP 200, exactly `PLAIN_TEXT_ONLY` | 0.506 |
| Same prompt, nonce JSON Schema | HTTP 200, schema-only field and exact enum value | 4.777 |
| JSON-object mode, multiline Markdown | Client timeout | 60.061 |
| Tightened initial prompt, plain output, attempt 1 | HTTP 500, upstream connection error on sphinx8 | 6.237 |
| Tightened initial prompt, plain output, attempt 2 | HTTP 200, two valid inspection requests | 1.750 |
| Tightened initial prompt, plain output, attempt 3 | HTTP 200, duplicate JSON keys and an unobserved filename | 1.346 |
| Tightened initial prompt, action JSON Schema | HTTP 200, two valid inspection requests | 8.767 |
| Previously failing input, action JSON Schema | Client timeout | 60.056 |

The nonce existed only in the schema, not the message. Its successful enforcement
despite the contrary plain-text instruction establishes that `response_format`
is effective through the existing router. The action schema also succeeded once:
it used `anyOf` for message/notebook actions, enum recipients, required fields,
`additionalProperties: false`, content lengths and a maximum array length.
This does not establish support for every JSON Schema keyword or stable operation.
The schema tested does not enforce recipient uniqueness or a single notebook
update; the harness must retain whole-batch semantic validation.

The API matches the [SGLang structured-output documentation](https://docs.sglang.io/docs/advanced_features/structured_outputs):
`response_format={"type":"json_schema","json_schema":{"name":...,"strict":true,"schema":...}}`.
This is a serving-stack capability, not a guarantee that an unconstrained Qwen
response follows instructions or that its proposed coordination is correct.

## Remaining failures

Prompt-only attempt 3 mentioned `mux.go`, absent from the input, and put two
intended messages into one object with repeated `action`, `recipient` and
`content` keys. Python's default JSON parser accepted these and kept the last
values; the then-current harness consequently parsed only one message. The diagnostic
duplicate-key check flagged it. The later tool-call validator rejects duplicate
argument keys. Some successful responses also asked for a broader coding pause
than the intended soft workflow.
The prompt changes therefore do not establish reliable compliance.

During these probes, inference workers logged scheduler exceptions at
15:01:07 PDT (job 17623988, sphinx8) and 15:02:25 PDT (job 17623987, sphinx7):

```text
eagle_worker_v2.verify -> eagle_prepare_for_verify
-> prepare_mamba_track_for_verify -> set_mamba_track_indices_from_reqs
TypeError: 'NoneType' object cannot be interpreted as an integer
```

Both logs then recorded SIGQUIT. A subsequent scheduler snapshot showed job
17623988 FAILED (247:0); 17623987 was still RUNNING at that snapshot despite its
inference-process exception. The three other original worker jobs were RUNNING.
The request timing overlaps the JSON-object and final schema timeouts, but no
request-ID correlation was established, so the exact trigger is not proven.
No additional inference requests were sent after this batch, and no services
were restarted or reconfigured.

## Decision

JSON Schema support is demonstrated, but this shared inference deployment is not
qualified for reliable structured-output use. Keep it disabled in the production
coordinator until the speculative-decoding/Mamba runtime failure is isolated and
resolved. Then validate the coordinator schema on both notebook arms, retain
semantic validation (including duplicate-key rejection), and rerun the bounded
worker smoke before the planned pilot. Neither protocol acceptance nor notebook
effectiveness is established by these eight requests.
