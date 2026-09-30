# Repair-only checkpoint replay

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
result = run_repair_checkpoint(checkpoint, repair_input=sidecar, scratch=scratch, run_id="new-run")
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

This compatibility branch is based on coordinator-training commit `741fbdb2`, with
filesystem capture backported from `514ed98a`. It deliberately retains the existing
string coordinator callback and does not migrate notebook/list coordinator semantics.
The full offline suite has two unchanged Linux process-inspection tests that fail on
macOS; repair and coordinator-focused suites run without model or container services.
