# Qwen3.5-9B merge-repair ablation on cb-mixture-36

Three independent 36-pair rounds compare the [non-thinking worker + coordinator
baseline](RESULTS_2026-09-23.md) with merge repair enabled. The only active
harness changes are `--repair-integrator --repair-attempts 2`. The attempts run
**sequentially**, only when the merged tree fails the existing health check,
and stop early when it passes. Each repair attempt has the default 25-step
limit. The harness submits the resulting patch without scoring the original
and repaired candidates against each other.

The same 36 pairs, two non-thinking workers, coordinator, completion gate,
cooperation tools, shared Git, model, and sampling settings are retained.
Workers have 1,000 steps and 3,600 seconds each; presub-merge, focused repair,
and the behavioral gate remain off. Each round runs ten pairs concurrently and
officially evaluates one at a time, with 20 CPUs, 128 GiB, and an eight-hour
limit on `visionlab-dgx1`. All 17 task images passed qualification, including
the Rust-derived tiktoken image. The benchmark revision is `63b9d44`;
official tests and reference patches are unchanged. Its evaluator copy retains
the baseline backend adapter and byte-preserving large-patch transport fix.

Source: `ef8b4d844cbcb33116c93be51496a7f9273ca90d`. The campaign manifest,
exact job/run paths, generated submissions, and official evaluations are under
`/nlp/scr/chency/projects/cooperagents/runs/20260925-cb36x3-repair-ef8b4d8`.
The Qwen3.5-9B service endpoint moved from `john8:61472` to `john14:21024`;
the user confirmed it is the same backend. Five replicas were healthy during
dispatch and monitoring. The private model profile remained outside source
snapshots and run records.

## Official results

A pair passes only when both feature results pass in the same official
`eval.json`. Every round has 36 `result.json` and 36 `eval.json` files, with no
evaluator errors or feature-test timeouts. All three Slurm jobs ended
`COMPLETED (0:0)`.

| Round | Slurm job | Feature passes | Both-feature pairs | Workers submitted / limited | Pairs invoking repair 1 / repair 2 | Coordinator events | Elapsed | Batch MaxRSS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 17598419 | 30/72 | 10/36 | 64 / 8 | 27 / 14 | 214 | 2:30:38 | 80959643K |
| 2 | 17599374 | 30/72 | 11/36 | 64 / 8 | 28 / 12 | 216 | 2:25:53 | 18705436K |
| 3 | 17599449 | 25/72 | 8/36 | 68 / 4 | 27 / 15 | 210 | 2:04:22 | 54438744K |

Across three rounds, repair passed **85/216 feature attempts and 29/108 pair
attempts**; **16 distinct pairs** passed both features at least once. The
non-thinking coordinator-on baseline passed **30/216 features and 9/108 pair
attempts**, with **six distinct passing pairs**. Its first round had complete
official scores but Slurm reported `OUT_OF_MEMORY`; its other two rounds
completed normally. The improvement is large in this sample, but three
stochastic rounds and the endpoint migration limit causal precision.

Repair attempts often exhausted their 25-step budget: first attempts ended
`limit` for 26, 28, and 27 pairs respectively; one first attempt in round 1
ended `submitted`. All recorded second attempts ended `limit`. An attempt's
status is not an official correctness score. The current artifacts do not
record pre-repair official scores or select the better of original and repaired
patches, so individual pair gains cannot be attributed to a particular
repair attempt.

Batch MaxRSS excludes some container memory. During monitoring, each job's
cgroup peaked near 100 GiB and recorded no OOM kill. One round-1 Jinja task
container briefly ran a Python process near 70 GiB RSS; it exited and the job
completed normally. The exact script-level cause of that spike was not proven.
Harness-reported cost zero does not establish zero inference or cluster cost.
