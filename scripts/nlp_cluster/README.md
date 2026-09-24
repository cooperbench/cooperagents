# NLP CPU execution

The six completed Qwen3.5-9B runs and their exact thinking settings are recorded
in [the cb-mixture-36 result report](RESULTS_2026-09-23.md).

This path runs the existing two-worker team in independent, writable Apptainer
sandboxes. Messaging uses the existing in-memory bus; Git sharing uses one
job-owned directory. The coordinator and completion gate are enabled. Repair,
presub-merge, helper spawning and longest-patch selection are disabled.

The dummy smoke runs a loopback-only OpenAI-compatible HTTP server with fixed
responses. It uses the real worker and coordinator clients, deliberately fails
then passes the completion gate, collects and merges both worker changes, invokes
the pinned official evaluator, and separately checks both official gold patches.
Dummy benchmark scores do not measure model quality.

## Prepared runtime

Cluster root: `/nlp/scr/chency/projects/cooperagents`.
CooperBench source: `runtime/cooperbench-63b9d44`, pinned to
`63b9d44d9f39a02fccf5bf0052db48a917a011fd`. Apply
`patches/cooperbench-backend.patch` with `patch -p1` in that checkout. This adds
backend-object injection only; official merge, tests and scoring remain upstream.
The evaluator's import dependencies are installed by `job.sh` on a compute node.

`runtime/images.json` maps public task image names to prepared SIF paths. Image
conversion must run inside a CPU allocation, with Apptainer cache and temporary
paths on scratch. To validate all dataset files without building images:

```bash
PYTHONPATH=src COOPERBENCH_DIR=/path/to/cooperbench \
  python scripts/nlp_cluster/prepare.py --check-only --images unused --manifest unused
```

To prepare a selected subset on a compute node, omit `--check-only` and supply
`--subset lite` (or another official subset), `--images` and `--manifest`.
Preparation records SIF SHA256 values. Only prepared and tested images are
qualified; a successful Go task smoke does not establish other toolchains.

## Submit

Commit runtime changes first. From this repository on the Mac:

```bash
python3 scripts/nlp_cluster/submit.py
```

This submits a 4 CPU / 16 GB / 30 minute `john` job, using committed source only.
It creates `code/cooperagents-<run-id>` and `runs/<run-id>`, containing metadata,
variant, submission resources, exact arguments, dependency versions, evaluator
patch, image manifest, logs and status. No credentials are archived.

After the dummy smoke passes and an API key is available, place the exported
`OPENAI_API_KEY` in a private file on the cluster (outside the source/run paths):

```bash
python3 scripts/nlp_cluster/submit.py --mode real --env-file /private/path/model.env
```

The real path loads the private file passed through `--env-file`, including its
model and sampling settings. Provider acceptance
of reasoning/sampling settings, tool behavior, token limits and model performance
still require a real API smoke. Dummy transport verifies outgoing parameters only.
Use `--pairs repo:task:f1,f2 ...` for explicit real-run pairs; prepare their images
first. The fixed dummy scenario is `go_chi_task:27:3,4`.

## Verified result

Job **17547514**, john8, source **b6fb39d3**, completed with exit **0:0** in
4m48s. All dummy assertions passed: seven HTTP requests, three steps per worker,
one gate rejection followed by normal completion each, shared Git pushes, both
markers in the merged patch, and exact worker/coordinator sampling payloads.
Official gold feature 3 passed **3/3**, feature 4 passed **4/4** tests.
The deliberately unimplemented dummy patch scored 0/2 without evaluator errors.

Record: `/nlp/scr/chency/projects/cooperagents/runs/20260922T022723Z-cooperbench-b6fb39d3`.
This qualifies the Go task27 image and complete runtime path, not every benchmark
image or real provider behavior. Local focused checks: 29 passed, 2 optional
integration skips; two repair-integrator tests also fail on clean upstream HEAD
in this local environment (repair is disabled in this experiment).

## Real API smoke (2026-09-21)

Job **17548220**, john17, source **dff0f875**, completed with exit **0:0** in
3m12s. OpenRouter authentication succeeded. The selected Qwen3.5-9B profile
produced real tool calls; generation took 106s. Both workers reached the 30-step
limit (neither submitted normally); the coordinator recorded four LOOP events.
Official evaluation completed without infrastructure errors and scored **0/2**.
This validates connectivity and execution, not satisfactory task performance.
Do not interpret the harness's reported zero cost as proof that API calls were
free; provider billing was not independently checked.

Record: `/nlp/scr/chency/projects/cooperagents/runs/20260922T040339Z-cooperbench-dff0f875`.
Private credentials remain outside snapshots and records; file mode 600 and parent
directory mode 700 were verified. No key is included in this document.

## cb-mixture-36 budget and qualification

The real launcher now uses **1000 steps and 3600 seconds per worker**, with
non-thinking sampling unchanged. `bench_compare.py --agent-time-limit` reaches
both solo and team workers. The deadline is checked before calls/actions; an
in-flight operation can overrun it. Real Slurm jobs have a 2-hour outer limit to
leave time for setup, final merge and evaluation. Submit one pair per job at this
budget; do not pass all 36 pairs to a single 2-hour serial job.

Qualification array **17548790** tests all 17 task images and every selected gold
feature without any model calls, up to three jobs concurrently (4 CPU / 16 GB
each). Reports: `/nlp/scr/chency/projects/cooperagents/runs/qualification-cb-mixture-36-20260922`.
Source: `8ecd950`. SIFs and builds use job-owned node-local scratch because the
persistent project filesystem has only about 13 GB free. Reports record each
image's node, path and SHA256; these images are node-affine, not durable storage.
Qualification was submitted; completion and pass counts must be checked before
launching the two evaluation rounds.

Read-only GCP inspection: changyu-dev is n4a-standard-8, ARM64, 8 vCPU / 32 GiB,
currently TERMINATED (not started by this work). The user's 10-pair concurrency
on that VM is a baseline, not a measured NLP limit. An initial NLP estimate is
10 concurrent pair jobs at 2 CPU / 8 GB each, across nodes (20 CPU / 80 GB total).
Typical john nodes expose 32 CPU and 120/240 GB; john17 exposes 20 CPU and 720 GB.
Actual launch concurrency remains subject to scheduling, task-specific peak
memory/compilation demand and external inference rate limits. Prior Go smoke
MaxRSS was only 0.65–0.78 GB, which does not qualify the other toolchains.
