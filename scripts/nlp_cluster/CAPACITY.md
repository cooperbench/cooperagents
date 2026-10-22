# Pair concurrency and NLP CPU resource selection

Target: **10–18 independent CooperBench feature pairs concurrently**. One pair is
one Slurm job, containing two concurrent mini-SWE workers and an event-triggered
coordinator. Qwen3.5-9B inference is external through OpenRouter: these jobs need
CPU, RAM and local disk, not GPU allocations.

## Initial requests (estimates, not load-qualified limits)

Start with **2 CPUs and 8 GiB per pair**, including both workers and subsequent
serial official evaluation. Each worker has 1000 steps and a 3600-second budget;
the job requests two hours for setup, possible in-flight deadline overrun, merge
and evaluation. Raise a task to 4 CPUs / 16 GiB when compilation or measured
memory warrants it. Do not equate submitted jobs with simultaneously running jobs.

| Concurrent pairs | Concurrent workers | Initial CPU / RAM total | Heavy-task CPU / RAM total |
|---:|---:|---:|---:|
| 10 | 20 | 20 CPU / 80 GiB | 40 CPU / 160 GiB |
| 12 | 24 | 24 CPU / 96 GiB | 48 CPU / 192 GiB |
| 14 | 28 | 28 CPU / 112 GiB | 56 CPU / 224 GiB |
| 16 | 32 | 32 CPU / 128 GiB | 64 CPU / 256 GiB |
| 18 | 36 | 36 CPU / 144 GiB | 72 CPU / 288 GiB |

Use multiple shared nodes instead of requesting an exclusive node. For the
initial request, scheduler-visible capacity limits on an otherwise empty node:

| Node class observed 2026-09-21 | Slurm CPU / RAM | Arithmetic pair ceiling | Practical initial target |
|---|---:|---:|---:|
| Most john nodes | 32 / 120000 MB | 14 | 10–12 |
| john10 / john11 | 32 / 240000 MB | 16 | 10–14 |
| john17 | 20 / 720000 MB | 10 | up to 10 |

The arithmetic ceiling uses `floor(CPU/2)` and `floor(memory_MB/8192)`; it is not
an availability guarantee. For 16–18 pairs, plan across at least two nodes rather
than packing one node to its limit. Existing allocations reduce these ceilings.
CPU nodes also use Slurm priority/backfill: `Priority` does not prove CPUs are
all occupied, and a cap of 10 does not reserve 10 simultaneous slots.

## Comparison with changyu-dev

Read-only GCP inspection returned **n4a-standard-8, 8 vCPU / 32768 MB, ARM64**.
It was TERMINATED; this work did not start it. The user's 10-pair concurrency on
that VM is a useful empirical baseline, but it is not a linear CPU scaling law:
NLP nodes are x86_64, task images/toolchains differ, and most agent time can be
spent waiting for external inference. Two CPUs / 8 GiB per pair is a conservative
starting allocation relative to that baseline, not a measured requirement.

Previous Go smoke peak MaxRSS was roughly 654–776 MB for a pair job. Do not use
that one lightweight task to size Python/Rust/native-extension builds. After the
10-job smoke, inspect per-job MaxRSS, CPU utilization, elapsed time, OOMs,
timeouts, model rate-limit errors and feature scores before raising concurrency.
An 18-pair campaign may have 36 worker requests in flight, plus coordinator
calls; provider request/token quotas can bind before cluster resources do.

## Disk and image placement

The persistent project filesystem had only about 13 GB free. Task SIFs, extracted
writable worker filesystems, build scratch and caches therefore belong under
`/scr/$USER/cooperagents/` on the allocated compute node. Persistent records keep
only metadata, checksums, trajectories, patches and scores.

There is no honest fixed disk-per-pair number before image qualification:
allow for the SIF, two extracted worker trees, build/test artifacts, and temporary
image-conversion storage. Inspect `df -h /scr` and image/rootfs sizes. Concurrent
pairs of the same task can share an immutable SIF, but never a writable rootfs.
Node-local images are node-affine and best-effort. Use the qualification report's
node and SHA256; requalify if an image disappears or changes.

## Launch and acceptance

1. Qualification array 17548790 tests all 17 selected task images and their gold
   features without model calls (maximum three image-test jobs in parallel).
2. Require successful qualification before the model smoke. Choose one pair per
   repository in manifest order: ten jobs cover all ten repositories without
   choosing tasks based on model scores. Record the exact list before dispatch.
3. Submit each pair with `submit.py --mode real --cpus 2 --memory 8G
   --qualification-report <report.json> --pairs <repo:task:f1,f2>
   --env-file <private-cluster-profile>`; this pins the image node and checks its
   checksum. Private profile contents never enter source/run snapshots.
4. Report infrastructure success separately from agent success: `COMPLETED` is
   not a passing feature. Count worker submissions vs step/time limits, feature
   passes out of 20, both-feature passes out of 10, coordinator events and errors.
5. The ten-job smoke precedes the two full rounds on cb-mixture-36. Keep each
   round's records separate; do not choose the better round as the reported score.

### Docker VM smoke migration (2026-09-22 UTC)

The user authorized starting `changyu-dev` (`soe-gemini-llm-agents`,
`us-west1-a`) and running the fixed ten pairs directly with Docker.
Observed capacity: 8 ARM64 vCPUs, 31 GiB usable RAM, about 200 GiB free disk.
All ten public task images were already present and passed a container-start,
Git and `/workspace` check. This is not a gold-feature qualification result.

Campaign: `/home/cameron_chen/cooperagents-smoke/runs/20260922-changyu-smoke10-9eba828`.
Its manifest fixes the ten pairs, source `9eba828` and CooperBench `63b9d44`.
Generation concurrency is 10 pairs (20 workers); official Docker evaluation
concurrency is 2. Each worker retains 1,000 steps and 3,600 seconds, with
Qwen3.5-9B non-thinking, coordinator and completion gate enabled, repair and
presub-merge disabled. The outer process has a three-hour timeout.

The initial startup failed authentication because the launcher did not source
the shell profile; this was corrected before continuing the same campaign.
Credentials remain outside source/run records, directory 700 and file 600.
`launcher.pid`, `status.txt`, `console.log`, `resources.txt`, and official
`eval.json` files are the monitoring sources. A dispatch lock prevents duplicate
campaign creation. Pending NLP qualification jobs 17548903–17548905 were
cancelled; the existing ten-minute monitor now follows this VM campaign.
VM concurrency is an empirical smoke configuration, not a demonstrated capacity
for 18 pairs; assess memory, elapsed time and task failures before scaling.

Provider pinning is supported through `COOPER_PROVIDER_ONLY=venice`. Both worker
and coordinator then send `provider.only=["venice"]` and
`provider.allow_fallbacks=false`, preserving `require_parameters`. This applies
to newly launched processes only. The already-running `9eba828` smoke remains
unpinned. As checked on 2026-09-22, Venice's Qwen3.5-9B endpoint advertises neither
`min_p` nor `repetition_penalty`; the user approved omitting these neutral-valued fields. The local and VM
private profiles now set `COOPER_PROVIDER_ONLY=venice` and omit both fields. Do not silently disable parameter validation or restart
the current paid campaign to change providers.

The real API probe on `changyu-dev` passed through both production client paths:
worker 0.97 s and coordinator 0.54 s, both returned `OK` and reported provider
`Venice`. Strict parameter checking and disabled reasoning were retained.
Probe record: `/home/cameron_chen/cooperagents-smoke/runs/venice-probe-2a8e7f9.json`.
These are connectivity/routing checks, not benchmark quality or load tests.
The active ten-pair smoke keeps its already-loaded original profile.

### Final smoke outcome (2026-09-22 UTC)

The original unpinned campaign completed with process exit 0 in 1:02:35.
All ten official score files exist: reported feature passes 2/20 and pair
passes 1/10; eight pair failures and one evaluator error. The evaluator's
11.1% display excludes its error case; the fixed campaign denominator is ten.
The Go Chi feature1 output says `no tests to run`, so its recorded pass does
not establish feature correctness. Seven pairs failed with merge-conflict
markers in submitted code; Hugging Face datasets had an empty/invalid patch.
Jinja failed transferring its 186,254-byte patch. Upstream sandbox.py sends
base64 content in a single shell argument (~248 KB), consistent with Linux's
single-argument size limit; no benchmark or reference patch was changed.

Workers: 19 submitted and one hit its time limit (DSPy feature4, 558 steps;
pair duration 3,623.8 seconds). Coordinator: 60 events, 35 COLLISION and 25 LOOP.
Process MaxRSS was 354,556 KiB (~346 MiB), excluding Docker-container memory.
Observed whole-VM memory use reached 5.7 GiB at polling points; this is not a
measured peak. No OOM/resource failure was observed. Zero harness cost is not
zero API expenditure. No paid retries or full evaluation rounds were launched.
The periodic monitor was paused after terminal results; VM remains running.
This smoke demonstrates execution capacity, not reliable final-merge quality.

### Remaining 26 pairs authorized (2026-09-22 UTC)

Coordinator enablement was verified in the original exact launch arguments and
worker trajectories, including a delivered instruction to inspect a failed
`sed` edit and edit `mux.go` manually. Event counts alone are not the evidence
of delivery. Coordinator activity does not imply successful final integration.

The user then authorized the set difference of the 36-pair manifest and the
fixed ten-pair smoke list: exactly 26 new pairs, no repeats. Campaign path:
`/home/cameron_chen/cooperagents-smoke/runs/20260922-changyu-remaining26-75dc1b2`.
Source `75dc1b2`, Venice-only routing, neutral unsupported sampling fields omitted,
strict parameters enabled. Pair concurrency remains ten and evaluator concurrency
two, with the same coordinator/completion gate and per-worker 1000-step/3600-second
limits. Outer campaign timeout is five hours to accommodate queued pair waves.
The original ten results and these 26 use different provider routing profiles;
they must not be described as a homogeneous fixed-provider evaluation round.
Existing integration failures remain part of the unchanged harness; no repair,
benchmark-test edits, or paid retries are authorized by this continuation.

### Remaining-26 terminal failure (2026-09-22 07:16 UTC check)

Campaign failed after 32:02.51. OpenRouter returned insufficient credits to six
recorded workers. Additionally, kernel logs confirm global OOM killed generation
Python PID 432809 at 07:12:11 UTC (anonymous RSS 24,378,600 KiB). Process MaxRSS
was 24,380,860 KiB (~23.25 GiB), excluding separate Docker memory. Thus ten-pair
concurrency on this VM is not validated as reliably safe for longer runs.

Only 22 pair records/scores exist: 38 workers submitted, six errored; four pairs
have no final record (go_chi_task:56:1,5; pallets_jinja_task:1559:5,8;
pallets_jinja_task:1621:4,6; samuelcolvin_dirty_equals_task:43:3,7). These missing
workers must not be counted as submissions or scored failures. Official partial
scores report six passing features out of the planned 52 and two passing pairs
out of the planned 26, not a complete evaluation. Passing pairs are
huggingface_datasets_task:6252:4,6 and pallets_jinja_task:1621:2,9. Pillow4,5 has
an evaluator patch-transfer error. Recorded coordinator events total132
(60 LOOP,72 COLLISION). Monitor paused; no paid retry launched. Both credit
availability and memory pressure need resolution before an authorized retry.

### NLP inference recovery (2026-09-22)

User authorized a new profile using the existing NLP SGLang service from task
01a0c830-28f6-71f3-b532-77098e005c0d. Private local profile:
`configs/qwen35-9b-nlp.env` (gitignored, mode600), mirrored to VM
`~/.config/cooperagents/qwen35-9b-nlp.env`. Model `qwen3.5-9b`, temperature1,
top_p0.95,top_k20,presence_penalty1.5; nonthinking uses
`chat_template_kwargs.enable_thinking=false`. No OpenRouter routing fields.
Worker XML tool-call parsing and coordinator text completion were verified live.

Campaign `/home/cameron_chen/cooperagents-smoke/runs/20260922-nlp-recovery-61843a2`
retries nine pairs (four missing and five containing API-error workers) and
re-evaluates original Jinja1465 f1f7 without regenerating its submission.
Two separate pair subprocesses run concurrently, releasing process memory on
completion; each retains1000steps/3600seconds. Evaluation concurrency1; outer8h.
The exact manifest, per-pair logs/exit codes and launch script are in the run.
A copied upstream evaluator changes only large-patch transfer to 65,536-character
base64 chunks; unicode/large/empty byte preservation was verified. The exact
transport diff is retained in `data/evaluator-transport.patch`; benchmark tests,
reference patches, scoring and original generated submissions are unchanged.

VM cannot directly reach john8:61472. Loopback-only SSH forwarding connects
VM127.0.0.1:61473 through this Mac to john8:61472 via sc. This depends on Mac
connectivity, and is not an independent VM-to-cluster route. The existing
inference-service monitor remains paused; this campaign's monitor is active.

### NLP recovery terminal results (2026-09-22 19:34 UTC check)

Campaign ended after 1:55:36 with exit1 because Jinja1559 f5f8's process was
OOM-killed (PID1334081, kernel timestamp18:43:04 UTC). MaxRSS31,531,228KiB,
about30.1GiB, demonstrates single-pair growth independent of concurrency.
Eight new pairs completed with16 submitted workers, no recorded worker API
errors or limits; the ninth pair has no final output. New results scored
4/16 available features,0/8 available pairs (planned denominators18 and9;
the missing pair remains unscored). All four passing features are in Jinja1559
f4f7 (f4), Jinja1559 f7f9 (f7), Jinja1621 f4f6 (f6), dirty_equals43 f7f9 (f7).
Recorded coordinator events for the eight new pairs:48 COLLISION.

The evaluation-only original Jinja1465 f1f7 now scored successfully at the
infrastructure level,0/2features due to a syntax error. Nine total eval.json
files have no evaluator error; the transport repair resolves the prior patch
write failure. Generated code still fails on merge markers and semantic tests.
Periodic monitor paused after final collection; no generation rerun initiated.

### Three-round NLP campaign (2026-09-23)

The planned homogeneous evaluation is three independent rounds over the same 36
pairs. Each round runs ten pairs concurrently: 20 mini-SWE workers, with up to
ten event-triggered coordinator requests. Three simultaneous rounds target 30
pairs and 60 workers, plus up to 30 coordinators. This is a client workload
target, not a measured or configured SGLang request limit. The existing
Qwen3.5-9B service has five healthy replicas; stage the three round launches and
watch queueing and latency before reaching the full target.

Request 20 CPU, 128 GiB, eight hours per round, or 60 CPU and 384 GiB total.
The node must also hold 17 qualified task SIFs and separate writable sandboxes;
use node-local `/scr`. At planning time the shared `visionlab-dgx1` node had
76 unallocated CPUs, about 493 GiB Slurm memory and 6.2 TiB scratch, and a
long-running small allocation. Scheduler availability can change. Pin each
round to the node where all 17 gold-feature image qualifications passed and
verify image checksums at start. Use `--no-requeue` to prevent an automatic
repeat of paid model calls after preemption. If that node has insufficient
capacity, wait or requalify images on another eligible node; do not silently
split a round across nodes.

The model profile talks directly to the NLP router (no VM tunnel): model
`qwen3.5-9b`, temperature 1, top-p 0.95, top-k 20, presence penalty 1.5,
`chat_template_kwargs.enable_thinking=false`, with the same sampling for
workers and coordinator. Each worker has 1,000 steps and 3,600 seconds;
completion gate is on, repair and presub-merge are off. Official evaluation
uses one evaluator at a time per round. The three outputs must stay separate;
report each round's feature and pair pass rates over 72 and 36 respectively.
