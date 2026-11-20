# Optional per-task uv cache

The repair and Apptainer evaluation APIs accept the same `RuntimeCache` capability.
It contains a sealed, authenticated SQFS descriptor, reused by a pool of episodes
for exactly one `(repo, task_id)` environment. Every sandbox gets its own overlay
upper/work and sees the merged cache at `/home/agent/.cache/uv`. Dependency edits
and downloads affect only that sandbox. `.venv` remains private.

Omitting the option keeps existing argv and host environment unchanged. Missing,
closed, mismatched, corrupt or unmountable caches log a fallback reason and run
cold. Invalid image/checkpoint provenance still follows existing admission rules.
The HTTP API, scoring, saved budgets and process memory limits do not change.

## Build and admission

Use `cooperagents.env.runtime_cache.RuntimeCacheIdentity.from_files` with the
original image manifest, original SIF SHA256 and the environment's pre-patch
`pyproject.toml`/`uv.lock`. The manifest is the caller's trusted original image
manifest, not a per-checkpoint publication manifest (which would prevent reuse
across checkpoints). Both the SIF and manifest digests are in the key. Do not use
a manifest supplied by an episode.

```python
from cooperagents.env.runtime_cache import RuntimeCacheArtifact, RuntimeCacheIdentity

identity = RuntimeCacheIdentity.from_files(
    repo=repo, task_id=task_id, image_sha256=image_sha256,
    image_manifest=image_manifest, pyproject=pyproject, uv_lock=uv_lock,
)
artifact = RuntimeCacheArtifact.build(cache_store, identity, warm)
# Persist artifact.model_dump_json() in a trusted controller catalog.
```

`warm(destination)` is a caller-owned preparation callback. It must finish
warming a uv cache using the exact admitted image and dependencies, stop all
writers, and return. Bind `destination` as that preparation container's uv cache;
keep the preparation venv elsewhere. Only the uv directory enters the SQFS.
Relative uv wheel-index links are allowed within that directory; links out of it
and special files are rejected. Preparation is an explicit operation, never a
runtime fallback or an episode-triggered builder.

Builders serialize under a per-key `flock`. All SQFS and receipt bytes are
complete and read-only before atomic directory rename; the published directory
is made read-only and the parent is fsynced. A failed warm/pack publishes nothing.
Existing keys are verified and never overwritten. Pin the returned receipt SHA256
in the controller; do not derive trust by scanning untrusted episode files.

`artifact.open()` verifies the pinned receipt and SQFS digest and copies it once
into a Linux sealed memfd. `F_SEAL_WRITE/GROW/SHRINK/SEAL` prevent host descriptor
writes as well as consumer writes. Open one capability per environment pool and
reuse it across episodes; opening one per episode would repeat the large copy.
Consumers inherit only the sealed descriptor into the holder. The writable
artifact directory is never mounted in a sandbox. The cache SQFS is mounted
read-only, beneath a separate private overlay in the existing namespace.

The restored dependency hashes, repo/task and original SIF are checked before a
cache is attached. A mismatch leaves the cold cache path in place. The external
manifest digest is a controller admission anchor; repair does not invent a new
image manifest or substitute the checkpoint's manifest for it.

## Repair consumer

```python
with artifact.open() as runtime_cache:
    result = run_repair_checkpoint(
        checkpoint, published=reference, purpose="training", scratch=scratch,
        run_id=run_id, image_base=image_base, runtime_cache=runtime_cache,
    )
```

The same option works with `purpose="difficulty"`. Native historical checkpoint
restoration without a published holder logs a cold fallback. The cache never
changes publication contents or the pristine image's inventory/audit. The reader
qualification refresh admits the exact prior image-reader digest because image
packing, native inventory comparison and image audit semantics are unchanged.
The prior direct-capture writer identity remains explicitly archived.

## Evaluation consumer

Pass `EvalRuntimeCache(channel, locator, receipts)` to `run_eval` with the
Apptainer backend, or pass `--runtime-cache-channel`, `--runtime-cache-locator`
and `--runtime-cache-receipts` together to the evaluator CLI. The channel
supplies a descriptor for the cache the controller already copied into a sealed
memfd. The grader reads the bounded locator and pinned `receipt.json`, verifies
the channel proof, descriptor identity, seals and size, and matches the cache
to the active `COOPER_IMAGE_MANIFEST`. A shared base also requires the same
image checksum. The grader borrows the pooled bytes without reopening the
artifact or reading `uv.sqfs` from disk.

Library callers can still pass
`ApptainerEvalBackend(..., runtime_caches={canonical_image_name: runtime_cache})`.
The cache capability is scoped to the repository and task in its receipt,
including when multiple tasks share identical SIF bytes. With a shared
`ImageBase`, the backend borrows it. Otherwise, it admits one audited base per
pinned SIF and reuses it across concurrent sandboxes. The holder verifies the
pristine dependency hashes before running evaluator commands. Each sandbox has
its own writable overlay over the sealed cache, so dependency installations
after a candidate patch write only into that sandbox's cache.

The CLI writes one hit or fallback receipt per created sandbox to a new JSON
file with mode 0600, even when evaluation raises. Sandboxes terminate before
`backend.close()` releases its owned image bases, then the CLI closes its
borrowed cache descriptor. The controller retains ownership of its channel and
descriptor. A borrow or mount failure uses cold evaluation and records the
reason. To disable the evaluator cache, omit all three flags or leave
`runtime_cache` unset in `run_eval`.

## Receipts and cleanup

For an explicitly requested cache, `<env.root>/receipts.json` records
`runtime_cache.status` (`hit` or `fallback`). A hit includes the key, repo/task,
image/manifest/dependency SHA256 values, SQFS SHA256, creation time, packed size
in bytes, and warm-up seconds. A fallback records its reason. Repair copies the
same record into result restore metrics; eval exposes `env.runtime_cache_receipt`.
Receipts in the ephemeral sandbox disappear with cleanup, so persist result
metrics or copy receipts before termination when gathering evidence.

Holder EOF/namespace teardown releases the FUSE/overlay mounts. Sandbox cleanup
removes all private upper/work files. The pool owner closes the shared capability
only after all borrowers finish. A host that opens the 12 study artifacts holds
12 packed SQFS copies in memfd storage: budget memory/swap for their packed size
and check cgroup accounting without increasing the 4096 MiB process limit.

## Verification and deployment evidence

Run an explicit preflight on the real Linux host **as the campaign service UID**
before enabling a cache. Run it for each of the 12 environment artifacts:

```sh
python -m cooperagents.env.runtime_cache_preflight \
  --artifact <trusted-pinned-locator.json> --image <original-image.sif> \
  --scratch <campaign-scratch> --expected-uid <campaign-service-uid> \
  --receipt <preflight-receipt.json>
```

This is a fail-closed deployment gate: a missing helper, failed SQFS/overlay
mount, fallback, incorrect uv environment/bind, write outside the private upper,
cross-sandbox pollution, or cleanup leak exits nonzero. It checks the actual
existing `MountHolder` user/mount namespace and records host UID/GID and namespace
UID mapping. The cache helper resolves Apptainer's bundled `squashfuse_ll` or the
trusted host PATH. Successful receipts include explicit helper-exists,
SQFS-mounted and overlay-mounted checks, and two independent sandbox probes.
Normal optional cache failures still return valid cold runs; they cannot pass
this separate preflight gate.

No Linux campaign host is available in this macOS implementation workspace, so
real overlay hits are **unverified** here. If the real service UID cannot mount
through the existing holder mechanism, stop rollout; investigate an explicitly
qualified Apptainer-overlay/fuse-overlayfs alternative. Do not deploy a campaign
that merely succeeds through cold fallback.

Offline tests use fake Apptainer/FUSE/overlay operations, plus a real sealed-fd
write rejection on Linux. They cover default-off byte identity against a frozen
method from `094a8bb`, key scope, atomic concurrent publication, admission and
mount failures, private writes, both consumers, receipts and owned/borrowed cleanup.
Simulated overlay tests establish the mount plan and private paths; they do not
certify real overlayfs, package availability or performance.

Before deployment, qualify real Apptainer/FUSE/overlay behavior in both repair and
eval. Compare identical pre-patch images/dependencies and candidate patches with
cache off/on (including a patch that changes each dependency file): identical
commands, exit status and scoring, no cache poisoning across sandboxes, and
complete upper/work release. Keep cold fallback valid and report its count.

Run the owner-approved 2-update, 112-slot repair smoke after review, never from
this implementation task. Require exactly 12 distinct `(repo, task_id)` cache
identities, **status="hit" in every sandbox receipt**, no cross-task hits, at
least 80% lower per-episode uv `write_bytes`
than the measured 5.23 GB baseline (at most 1.046 GB on the same unit basis), no
update-interval regression, and no new error signature. Exclude one-time build
and pool admission from episode I/O but report them separately. Cache misses are
valid slow runs; a smoke dominated by fallbacks does not establish the benefit.
The training driver lives outside this repository. The manager will render the
112-slot / 2-update repair variant from the `038af87` run record and fill the
exact submission command after review. Its known controller entrypoint is:

```text
python -m polar_ext.recipes.cooperagents_tinker.aws.controller --run-dir <run-dir>
```

This is the controller shape, not a complete submission command. The rendered
config must pin this cooperagents revision and admit the 12 cache capabilities;
external driver wiring and exact submission remain a manager action. Do not
substitute invented flags or run it as part of offline verification.

Rollback: omit `runtime_cache`/`runtime_caches` and the optional eval catalog flag.
Existing qualified SIFs/checkpoints remain unchanged; artifacts can be retained
for later inspection or removed after all borrowed descriptors are closed.
