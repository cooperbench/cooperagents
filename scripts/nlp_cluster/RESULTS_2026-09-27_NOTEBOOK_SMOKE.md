# Coordinator notebook smoke — 2026-09-27

## Scope

Run the existing deterministic container smoke and one bounded real Qwen3.5-9B
smoke on Stanford NLP. This is execution validation, not the notebook ablation
pilot or a feature-score comparison. Only CPU resources were requested; the
existing inference service was reused unchanged.

## Deterministic container smoke

- Job `17637578`, `john2`, `COMPLETED`, exit `0:0`, elapsed `00:03:41`.
- Source `4a1574f8`, pair `go_chi_task:27:3,4`.
- Run: `/nlp/scr/chency/projects/cooperagents/runs/20260927T211916Z-cooperbench-4a1574f8`.
- Two non-root Apptainer workers read the same v0 notebook, then both read v1
  and v2 after atomic replacement. Writes were rejected. Notebook contents did
  not appear in the submitted patch.
- The real worker/HTTP loop completed with both workers submitted in four steps;
  it exercised a rejected finish followed by a corrected finish and merged both
  marker files. Both workers read notebook v1 into their subsequent model input.
- Ten synthetic HTTP requests included two coordinator decisions, both valid.
  No worker replies to the coordinator were scripted in this deterministic case.
- Gold feature 3 passed 3/3 tests, gold feature 4 passed 4/4, without evaluator
  errors. The dummy patch scored 0/2 by design and does not measure model quality.
- Locally replayed final histories exactly match both exported worker histories;
  no unfinished calls or deliveries after worker completion were found.

## Real-model smoke

- Job `17637587`, `john2`, `COMPLETED`, exit `0:0`, elapsed `00:02:23`.
- Source `3f8917ad` (documentation-only difference from the code above).
- Run: `/nlp/scr/chency/projects/cooperagents/runs/20260927T212636Z-cooperbench-3f8917ad`.
- Model ID `qwen3.5-9b`; captured worker and coordinator requests both used
  temperature 1, top-p .95, top-k 20, presence penalty 1.5 and
  `chat_template_kwargs.enable_thinking=false`.
- Pair `go_chi_task:27:3,4`; 30 steps and 300 seconds per worker. Both workers
  reached the expected 30-step limit without an exception. Pair duration was
  73.23 seconds. Official feature scoring was deliberately not run.
- Both workers read notebook v1 successfully (shell requests seq 31/34,
  responses 35/39); the content then appeared in their SDK requests.
- Two replies reached the coordinator. Full trajectory audit passed on the
  cluster and locally; SHA256 matched, histories replayed exactly, no calls
  remained unfinished, and no messages were delivered after worker completion.

### Coordinator behavior observed

| Decision | Worker steps at observation | Outcome |
| --- | --- | --- |
| Initial, seq 11 | 0 / 0 | Valid notebook update to v1 |
| Second, seq 339 | 17 / 18 | Valid message to each worker about shared interfaces |
| Third, seq 534 | 30 / 30 | Invalid JSON; whole batch rejected |

Only **2/3** real returns were valid. The rejected response contained a literal
newline inside a JSON string (line 5, column 1518); the host notebook correctly
remained at v1 and none of that batch's actions executed. There were no transport
failures. Three returns are too few to estimate a reliable validity rate and do
not meet the pilot's minimum sample size; the observed rejection still needs
attention before scaling this configuration.

The initial notebook named `handle.go`, `handle_func.go`, `parser.go` and
`pattern_parser.go` before workers had inspected code. Their subsequent listings
instead showed the relevant implementation in `mux.go` and `tree.go`. It also
mislabeled agent2's feature as “Task 28”; both workers were executing task27,
features 3 and 4. That label appeared again in worker replies.

The coordinator proposed a `ParsedPattern` struct, then asked both workers to
define it. Worker replies adopted that name, and agent1 eventually inserted a
struct in `tree.go`; the final integrated patch was 664 bytes and agent2's patch
was empty. This is concrete evidence of influence on the implementation process,
not evidence of better correctness. No messages-only counterfactual was run.

Agent1's reply proposed `Method string`, while agent2 reported agreement on
`Method methodTyp`. The rejected final notebook would have labeled the agreement
“LOCKED” and “Verified”; it was never published. Agent1's later edit did use
`methodTyp`, so the two replies alone do not prove a final implementation
mismatch. Agreement claims should identify the supporting reply or code evidence.
Agent2 also twice attempted to send a message through bash before using the
actual messaging tool.

### Next gate

Keep full-batch validation. Before the two-arm pilot, improve JSON-output
reliability and the startup prompt's evidence discipline: initial coordination
should request inspection and proposed ownership, then ground filenames and
interfaces in worker observations. The notebook should assign one owner for a
shared definition and distinguish proposals, reports, and explicit agreement.
Re-run the bounded smoke after a targeted change, then collect the planned
minimum 20 model returns per arm. Do not proceed directly to fixed10×k3.

## Remaining scope

Docker mounting is not qualified by an Apptainer run. This smoke does not meet
the pilot's minimum of 20 model returns per arm and does not replace fixed10×k3.
