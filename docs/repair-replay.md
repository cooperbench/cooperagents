# Preparing repair replay

The historical A/B/C cb-mixture-36 experiment passed 9/108, 12/108 and 7/108
pairs respectively. Those runs retained final integrated submissions, not exact
worker deliveries or the repair handoff filesystem. Periodic shared branches
cannot replace those missing checkpoints. New collection produces new worker
outputs; it does not recover the old 108 A outputs.

Add `--checkpoint-repair` to `scripts/bench_compare.py` or
`scripts/nlp_cluster/submit.py --mode real`. It automatically records full I/O
trajectories and works with the normal official evaluation. Add
`--collect-trajectories` on the cluster (or `--skip-eval` on the benchmark) only
when scoring should be deferred. Collection alone does not enable repair.

```bash
# Append to the normal qualified cluster launch arguments:
--checkpoint-repair --repair-integrator --repair-attempts 2

# Direct benchmark example, using the normal task/model environment:
python scripts/bench_compare.py --pairs go_chi_task:26:1,2 \
  --team-only --no-seed --coop-tools --git-share \
  --checkpoint-repair --repair-integrator --repair-attempts 2
```

The supported shape is a single concurrent mini-SWE no-seed team with at least
two workers and mechanical integration. Alternative selectors, helper spawning,
best-of-N and decomposition fail explicitly. The launcher saves immutable source,
dependencies, image hashes and evaluator patches through its existing workflow.

## Recorded boundaries

Each pair contains `checkpoints/run.json` with the task assignments, effective
team configuration and worker/repair budgets. Every snapshot includes its own
copy and a checksum manifest. Snapshots are created in the worker thread when it
returns, rather than when the main thread later consumes ordered results.

| Directory | State captured |
| --- | --- |
| `worker-agent1`, `worker-agent2` | Accepted worker end, before `collect_diff` stages files or restores stashes; includes limit/error outcomes |
| `pre-repair` | Mechanical merge after reject/orig cleanup and the initial gate, if repair is enabled |
| `before-integratorN` | Exact filesystem before each repair call, after focused evidence gathering; exact repair task included |
| `post-repair` | Final integrated delivery after the last `git_diff`; exists even when repair was skipped |

Each snapshot contains:

- `state.json`: base commit, HEAD/status/stashes with exit codes, runtime identity,
  timestamps, capture boundary, patch recovery source and worker result/context.
  Merge snapshots also include the gate commands, outputs and exit codes; final
  snapshots include repair results and per-attempt time.
- `raw.patch`: the unfiltered collected diff. For worker snapshots this is the
  actual `collect_diff` input, including stash/shared-branch recovery when used.
- `integration.patch`: the exact test-stripped patch used by integration.
- `submission.patch`: the evaluator-ready patch, also excluding `.cb_checks`.
- Filesystem archives retaining `.git`, staged/unstaged changes, untracked and
  ignored files, binary content, permissions and symlinks. Diff generation uses
  Git's binary patch format. Pre-repair patch previews use a temporary Git index;
  they do not stage the live tree or restore its stashes.

The capture lock waits for any in-flight Git push and blocks sync writes during
capture and collection. Normal periodic sync resumes afterwards so an active
teammate can still receive the finished worker's final branch. The filesystem is saved **before** patch recovery; a
recovered shared-branch patch may differ from the archived live working tree,
and `patch_source` records that distinction. The submitted marker and its
completion gate have already finished at this boundary. Limits/errors remain
separate from successful submission.

With repair disabled, `healthy` is null and `gate_checks` is empty: collection
does not introduce an extra build/test into the no-repair baseline. With repair
enabled, the existing health policy still decides whether repair runs; timeouts
and missing tools can be classified healthy by that policy. Raw exit codes allow
that outcome to be distinguished from a confirmed passing check.

`repair_duration_seconds` sums repair calls, cleanup and subsequent gates,
excluding filesystem capture time. The separate wall time includes captures
between attempts. Full snapshotting also adds overhead to overall team duration;
do not compare that duration directly with historical runs without accounting
for recording time.

## Verify and restore

```bash
PYTHONPATH=src python -m cooperagents.checkpoint /path/to/pair/checkpoints
PYTHONPATH=src python scripts/audit_trajectories.py /path/to/cluster/run
```

The cluster auditor verifies all expected snapshots, matches saved worker
contexts/statuses against the exported trajectories, and matches the final
`submission.patch` against the official `integrated.patch`. The manifest is
written only after every artifact is saved and hashed. Disk/archive failures,
truncated diffs, missing snapshots and checksum mismatches fail collection or
audit. Existing checkpoints are never overwritten.

Apptainer saves the whole writable sandbox as `rootfs.tar.gz`, plus each external
shared/notebook bind as `mount-N.tar.gz`. Restore **your own verified archives**
on a Linux host into fresh directories, preserving permissions and symlinks:

```bash
mkdir replay-fs
tar -xzf /path/to/checkpoint/rootfs.tar.gz -C replay-fs --no-same-owner
# Extract each mount archive into its own fresh directory as well.
# Use the target/read-only mapping in state.json; never bind the old live share.
apptainer exec --writable --containall --cleanenv \
  --no-mount hostfs,bind-paths \
  --home "$PWD/replay-fs/home/agent:/home/agent" \
  --bind "$PWD/replay-fs/tmp:/tmp" \
  --bind "$PWD/replay-fs/var/tmp:/var/tmp" \
  --bind /etc/resolv.conf:/etc/resolv.conf:ro \
  --pwd /workspace/repo "$PWD/replay-fs" bash
```

Add restored bind mounts recorded in `state.json` when present, for example
`--bind "$PWD/replay-share:/cbshared"` and
`--bind "$PWD/replay-notebook:/coordination:ro"`. Use the recorded `repo_path`
for tasks with a different working directory. The image SHA256 records
provenance; the archive contains the installed filesystem needed for repair.

Docker pauses the worker, saves `rootfs.tar` with `docker export`, and separately
copies each bind/volume into a mount archive before unpausing. The saved image ID
and container `Config` provide environment, user and working-directory settings;
`network_mode` records the original isolation policy.
Use `docker import rootfs.tar repair-replay:<unique-tag>` and start a fresh
container with those recorded settings, network isolation and restored mounts. Export does not
include volumes, so importing only the rootfs is insufficient. Local tests save
only `repo.tar.gz`; their host tools are not checkpointed.

Snapshots save files, not processes, RAM, network connections or a simultaneous
global state of the team. Shared mount archives are per-worker observations;
another worker can update that share while it is copied. Docker freezes its own
container, not other containers writing shared volumes. Rootfs device/socket and
runtime-generated mounts are not process checkpoints. The stdlib tar archives
do not preserve extended attributes or ACLs. Host model credentials are
not copied intentionally, but filesystem archives and saved raw conversation
state can contain secrets already written by a task; treat them as private run
artifacts. Full rootfs snapshots require substantial storage, especially Docker's
uncompressed export. Start with 1–2 pairs before collecting three full rounds.

For a paired experiment, score `pre-repair/submission.patch` and
`post-repair/submission.patch` with the same official evaluator and preserve
both score directories. Compare passes, gains, regressions and extra repair
time on identical worker deliveries. For counterfactual repair, restore
`before-integrator1` and use its recorded task/budgets with a new model call;
model sampling need not reproduce the original response. When collecting with
repair disabled, restore `pre-repair` and apply the chosen replay gate/policy.

## Verified NLP smoke (2026-09-29)

Final-source Slurm job `17657758` ran on `john11` with 4 CPUs, 16 GiB and a
20-minute limit. Run records:
`/nlp/scr/chency/projects/cooperagents/runs/20260929T235535Z-repair-checkpoint-smoke`.
It completed successfully with two submitted workers, one submitted repair,
five verified snapshots and three restored sandboxes. The actual mini-SWE loop
used deterministic synthetic model replies; this validates checkpoint/recovery
behavior, not model quality or official feature accuracy.

The smoke checked staged versus unstaged content, ignored runtime files,
`/tmp` state, binary bytes, executable bits, symlinks, the shared Git bind and
notebook bind. Restored repair input/output matched the saved patches; the build
gate remained failing before repair and passing afterwards. The trajectory and
checkpoint auditor passed. Local relevant tests: 95 passed, 5 optional skips;
Ruff and `git diff --check` passed. Real Docker execution was not tested here.

The exact tested source is retained as `source.tar.gz` with SHA256
`23f641f98ec37f86c9c56329c80b716366a71164c04da27eee1d5b3629147f6b`.
`source-provenance.json`, `dependencies.txt`, `job.sh`, `smoke.json` and
`trajectory-audit.json` retain the launch and verification details.

Earlier job `17657673` saved all five snapshots and completed repair but failed
a smoke-driver assertion: worker2 printed notebook text before the required
first-line submission marker and reached its step limit. The corrected driver
passed in job `17657738`; the final-source rerun also passed. Those runs remain
separate and retain their own source provenance.

The final smoke stored 654,667,457 bytes (about 624 MiB) for one lightweight Go
pair. At that footprint alone, 108 pairs need about 66 GiB, exceeding the roughly
48 GiB free observed in the project filesystem. Larger task/toolchain images can
need more. Arrange sufficient persistent storage before a full three-round
collection; do not assume node-local scratch is durable storage.

Run the same smoke in a CPU Slurm allocation with the project's mini-SWE
dependencies installed:

```bash
PYTHONPATH=src LITELLM_LOCAL_MODEL_COST_MAP=True \
  python scripts/nlp_cluster/checkpoint_smoke.py /path/to/new/private/run \
  --image /nlp/scr/chency/projects/cooperagents/runtime/images/go-chi-task27.sif \
  --image-sha256 23927242c619a73414449d7cc7ba56d6cc5598adc8639f60ac86b6b1f83af4eb \
  --scratch /path/to/job-owned/scratch
```

# Repair-only checkpoint replay API

The checkpoint API saves complete filesystem state at a repair boundary. The replay
entrypoint restores a fresh Apptainer sandbox and runs only integrator attempts. It
returns the existing `RunResult`; official evaluation uses the existing result writer
and evaluation API. The original checkpoint is never overwritten.

## Capture

Construct `UnifiedHarness(checkpoint_dir=Path(...))` for a single mini-SWE, coop-tools,
no-seed mechanical-merge team. Use its default fresh in-memory bus. Checkpoint capture
supports one or two repair attempts, ordinary messaging, Git share, and prompt flags.
It rejects task boards, claim mode, custom bus state, spawn, adaptive/decomposed teams,
best-of-N, and other integration modes.

The completion gate may be disabled or supplied as
`functools.partial(cooperagents.verification.validate, merged=False, build_artifact=None)`.
Arbitrary callable gates cannot be serialized.

`before-integrator1/repair-input.json` is covered by the checkpoint manifest. It captures
the fully prefixed task, actual initial system/user messages, agent configuration
(including compaction), model formatting, exact tools, generation settings, budgets,
gate descriptor, assignments, and source hashes. API credentials, endpoint addresses,
and absolute deadlines/output paths are excluded. Capture finishes before any model
request and does not consume the agent's wall-clock budget.
Repair input v2 also saves unread inboxes for both integrator attempts, including sender,
recipient, content, timestamp and delivery order. Capture does not drain the live bus.
Replay restores these queues once into its fresh bus; normal agent steps read them.
Previously consumed messages are not redelivered, and messages sent by the new first
attempt remain available to the new second attempt.

## Restore and repair

```python
from pathlib import Path
from cooperagents.repair import run_repair_checkpoint

result = run_repair_checkpoint(
    Path("/runs/source/checkpoints/before-integrator1"),
    scratch=Path("/scratch/new-run"),
    run_id="new-run",
)
```

`ApptainerEnv.from_checkpoint(checkpoint, scratch=...)` is also public. It verifies all
manifest hashes, restores a new rootfs and independent mount copies, checks the original
base commit and recorded HEAD/status/stashes/patch, and cleans partial restores on error
or cancellation. Rootfs permissions and valid system symlinks are retained; archive
writes through links or outside the owned destination are rejected. Only `/cbshared`
(writable) and `/coordination` (read-only) checkpoint mounts are supported. The source
SIF is provenance and is not rebuilt or required to exist during restore.

The first attempt runs unconditionally with the saved prompt. A second attempt uses the
current tree and fresh evidence in a new conversation. It preserves the saved prompt
wrapper, including the capture host's system information, and adds prompt prefixes only
once. It never loads an old `before-integrator2`. The final binary-capable patch includes
all worker and repair edits relative to the original task base; existing stripping and
result writing remain applicable.

Step/time limits are valid agent outcomes. Nonzero tool exits are normal observations.
Restore/export failures, truncated control output, and failed agent execution invalidate
replay instead of generating a zero score. Post-attempt health keeps the harness's existing
semantics and is recorded separately from official scoring.

## Legacy v1 import

Legacy checkpoints without effective inputs require explicit evidence:

```python
from cooperagents.repair import import_legacy_repair_input

sidecar = Path("/runs/new-run/data/repair-input.json")
import_legacy_repair_input(
    checkpoint,
    journal=Path("/runs/source/trajectory.jsonl.gz"),
    source=Path("/code/immutable-collection-source"),
    variant=Path("/runs/source/variant.toml"),
    launch_args=Path("/runs/source/training-args.txt"),
    destination=sidecar,
)
result = run_repair_checkpoint(checkpoint, repair_input=sidecar, scratch=scratch, run_id="new-run", max_attempts=1)
```

The supported collection sources are commits `514ed98a59c611ee5a38027c8459d6fcbcec92b8`
and `043fa798a8fcf667f14d32153a19dc0abff80c33` (the latter requires the saved
`historical_a` collection identity). Both retain the same repair templates, agent defaults,
tools and standard verification gate; committed source bytes and the actual first SDK
request remain required evidence.
Its required files must match committed contents. Import verifies the checkpoint receipt,
journal ordering, startup task/budgets, exact initial SDK request, source templates and
tools, variant and actual launch arguments, and the standard completion gate. Changed,
missing or redacted evidence fails before inference. The sidecar records evidence hashes
and selected event sequences. Legacy contract-first assignments are rejected because their
effective assignment provenance was not saved. Imported files are written only to the new
run. Supporting a new legacy source requires evidence-backed compatibility work.
The exact initial two-message SDK request proves only integrator1's inbox was empty.
Legacy imports therefore support one attempt; integrator2's pending inbox is unknown.
Existing v1 effective-input checkpoints without inbox evidence fail before restore or
inference and must be recaptured. Missing queue state is never treated as an empty queue.

## Verification and current boundary

The offline suite uses actual archive/Git state, public restore, and the real mini-SWE
agent/action parser with fake completions and an in-process replacement for container
execution. It does not call a paid model API or start Apptainer:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True uv run --extra mini --extra llm --extra dev pytest \
  tests/test_checkpoint.py tests/test_harness.py tests/test_repair_replay.py \
  tests/test_repair_completion.py tests/test_apptainer_env.py tests/test_worker_guard.py
```

Filesystem replay, effective-input preservation and full-response injection are implemented.
Native training traces and rewards are supplied by the downstream Polar extension. Real
container/model replay, reward learning signal and optimizer/weight acceptance require their
separate live checks; offline fake completions do not prove task repair stability.

## Optional integrator transport

`cooperagents.completion.CompletionBinding` binds two full-response callbacks and
validated `CompletionSettings`: `action` runs the current policy, while `summary`
runs a fixed model and revision. Pass it as `completion=` to `run_repair_checkpoint`,
or `repair_completion=` to `UnifiedHarness`. Ordinary workers never receive it.
Callbacks receive `MiniSweCompletionRequest` with prepared SDK-typed messages/tools,
actor and call identities, purpose, effective sampling and timeout. They return a
complete LiteLLM `ModelResponse`; a native tool-only message may have `content=None`.
The callback owns its bounded network call and recording before parsing. Injection
has no hidden retries, background threads, old-profile override or fallback endpoint.
The existing action parser, observations, submission gate and compaction stay in use.
Endpoint/summary failures invalidate replay; model format errors still produce the
existing correction observation. `trajectory=` optionally preserves the ordinary
Cooperagents journal; it does not substitute for native training gateway records.

The integrated implementation lives on `cooperagents-coordinator-training`; the
`cy/repair-only-rollout` source branch is retired after its PR is merged. New team
collections use the notebook/tool-call coordinator when enabled. Repair-only replay
does not restart workers or the coordinator: it uses saved integrator inputs and
preserves the repair execution chain. Enabling the new coordinator can change the
worker deliveries in a new collection, and therefore the repair inputs.

The full offline suite has two Linux process-inspection tests that fail on macOS;
repair and coordinator-focused suites run without model or container services.
