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
