---
date: 2026-09-21 17:19:22 PDT
researcher: Codex
repository: cooperagents
branch: slurm-cooperbench
git_commit: 37766c6b7cd32fabc7778a6ff683cd5517be4244
upstream_branch: sagemaker
cooperbench_inspected_commit: 63b9d44d9f39a02fccf5bf0052db48a917a011fd
status: implementing-dummy-validation
---

# NLP CPU CooperBench adaptation plan

## Overview

Run two feature-owning workers and the existing coordinator on Stanford NLP CPU nodes,
calling a separate Qwen3.5-9B inference service. Keep only:

```text
--team-only --max-agents 2 --no-seed --coop-tools --git-share --coordinator --completion-gate
```

Presub-merge, repair, focused repair, helpers, claim-mode and additional reviewers remain off.
No longest-patch selection. Completion checks retain upstream bounded rejection semantics.
Final integration remains upstream mechanical merge, including its conflict fallback.

This document is an implementation draft, not a statement that the runtime is implemented.
The user-approved ordering is research update → adaptation plan → readiness inventory.
This phase created the branch and checked readiness; it does not launch full experiments.

## Current state

- `src/cooperagents/env/base.py:23`: existing Environment contract is sufficient.
- `src/cooperagents/env/docker.py:22`: long-lived Docker workspace; base commit, stash recovery,
  non-login shell/PATH, large-command stdin and timeout behavior should be preserved.
- `scripts/bench_compare.py:239`: feature assignments already support two workers.
- `scripts/bench_compare.py:289`: completion callback already correct.
- `scripts/bench_compare.py:301`: shared Docker volume; `:303`: DockerEnv factory.
- `src/cooperagents/harness.py:815`: worker env creation; `:821`: shared Git initialization;
  `:848`: Docker-specific recovery. `:1065`: mechanical integration environment.
- `src/cooperagents/eval/cooperbench.py:27`: preserve existing official artifact layout.
- Final calls in bench_compare hardcode Docker evaluation and do not check evaluator returncode.
  Pair exceptions can be skipped, so process exit 0 alone does not establish success.

Fresh allocation 17545758 on john7 verified x86_64, Python 3.12.3 and Apptainer 1.5.3.
Docker CLI exists but socket permission is denied. SSH, Kerberos and CPU scheduling work.
Use explicit account=nlp, ntasks=1, CPU/memory limits, no GPU. Do not build on sc login node.

## Required decisions before finalizing

1. Model service selected: OpenRouter `qwen/qwen3.5-9b`, non-thinking, identical worker/coordinator
   sampling: temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5,
   repetition_penalty=1.0. Shared request plumbing and offline tests are implemented;
   see `configs/qwen35-9b-openrouter.env.example`. Credential provisioning, provider selection,
   output budget and CPU-node live request validation remain readiness items.
2. Official evaluation location: NLP-local is the user's possible intended scope; existing upstream
   supports only Docker/Modal/GCP. Optional split route is NLP workers + official Docker evaluation
   on user-owned changyu-dev. This alternative is not yet authorized/selected.

The independent phases below can proceed without the remaining answer. Live model checks and evaluator
deployment cannot. If all execution must stay on NLP, add a separate explicit upstream evaluator
backend scope; do not silently evaluate remotely or reimplement scoring.

## Phase 1 — fixed inputs and preflight

Proposed files: `scripts/nlp_cluster/preflight.py`, `scripts/nlp_cluster/README.md`,
an experiment pair manifest and image manifest under the same environment directory.

- Pin cooperagents and CooperBench commits. Temporary inspected CB checkout is not a deployment.
- Start with one pair, e.g. go_chi_task:27:3,4 (historical availability only; current image unverified).
  Final subset/full run list must be explicit and immutable per run.
- Validate each pair has exactly two distinct features, feature.md, scorer resources and valid IDs.
- Record original task image, OCI digest, architecture, base Git commit and resulting SIF checksum.
  Fail on unsupported architecture; do not silently drop tasks or assume ARM emulation exists.
- Separate worker-visible task repos/specs from evaluator-only tests/reference patches.
- Check selected build command, writable scratch, disk capacity and endpoint configuration presence.
  Only host-side controller receives model credentials; no environment dumps or secret copying.

Data inventory already checked: upstream all.json has 30 tasks, 652 pairs and 199 unique features;
all referenced feature.md and tests.patch exist. No NLP deployment or image completeness certified.

Automated acceptance: malformed/missing input fails before model calls; preflight emits structured
ready/blocked/unverified fields and nonzero exit for required blockers.
Compute acceptance: pinned single-task image can run bash/git/toolchain and write in its sandbox.

## Phase 2 — minimal Apptainer Environment

Proposed files: `src/cooperagents/env/apptainer.py`, relevant changes in
`scripts/bench_compare.py` and `src/cooperagents/harness.py`.

- Implement existing Environment methods; leave worker/coordinator algorithms unchanged.
- Prefer cached read-only SIF with a persistent per-environment writable overlay if compute smoke
  establishes support. If unsupported, use a job-scoped writable sandbox; prove write permissions
  and filesystem state persistence before proceeding. Do not choose transient writable-tmpfs alone.
- Every worker and merge environment gets its own repo, temp and home state; preserve state across
  commands and image toolchain PATH. Explicitly contain default home/cwd/host filesystem binds.
- Same-pair workers share only an explicit job-scoped directory mounted at /cbshared.
- Preserve initial Git base, staged/committed diff behavior, stash recovery, binary-safe output,
  large-command stdin and timeout result. Kill timed-out job-owned subprocesses appropriately.
- Replace Docker-only lost-.git recovery with an existing Environment operation where possible;
  test deleted repo/cwd behavior instead of silently losing recovery.
- Add runtime selection and scratch/image-manifest arguments to bench_compare. Keep Docker default
  behavior for existing users. Selected NLP configuration needs no new harness registry.
- Add explicit generation-only mode so lack of a local official evaluator is visible, not a failed
  Docker eval after generation. Mark generated-but-unscored results accordingly.

Automated acceptance: command construction/quoting, timeouts, path containment, large input,
base diff, cleanup scoped to owned paths, five flags active and disabled flags inactive.
Compute acceptance: two scripted workers retain separate state, exchange shared Git, and a fresh
merge environment yields both changes without any LLM call. No generic test framework added.

## Phase 3 — CPU launcher and durable run records

Proposed files: `scripts/nlp_cluster/submit.sh`, `scripts/nlp_cluster/job.sh`, runbook.

- Use john low-priority CPU scheduling with explicit account=nlp and ntasks=1; smoke initially
  requests a bounded 4 CPUs/16 GB allocation, adjusted from measured image/toolchain needs.
- Use `/nlp/scr/chency/projects/cooperagents` as proposed persistent root. Code snapshots contain
  committed files only. Runtime scratch and container writable state are job-scoped on compute node.
- Use `code/cooperagents-<run-id>` and `runs/<run-id>`; record metadata, copied variant/pairs,
  submit script, exact arguments, rendered config, data locators, logs, status and artifact locators.
  A checkpoint field is explicitly not applicable for inference-only benchmark runs.
- Run ID: UTC timestamp + experiment + short SHA. Record backend/SIF/dataset/model identities and
  enabled flags; never copy .env or tokens. Use an external protected profile on the controller.
- Stage artifacts even on failure; distinguish generation failure, partial, generated/unscored,
  evaluation failure and scored completion. Compare expected pair IDs against actual artifacts.
- Do not treat existing result.json as proof of successful scoring when using --resume.
- Create/install the pinned Python environment on a compute allocation, not the login node.

Automated acceptance: rendered launcher requests no GPU; invalid readiness blocks launch; secret
values absent from metadata; failure exit preserved and no unrelated jobs/processes cleaned up.

## Phase 4 — model smoke and official evaluation

Requires the two decisions above. No full sweep before acceptance.

1. From the allocated CPU controller, verify endpoint health/model identity, then one bounded
   tool-capable worker request. Force an offline coordinator trigger to test its model call rather
   than relying on chance; distinguish successful LLM nudge from static fallback.
2. Run one pair, one concurrent pair, fixed step/token/time budget recorded in the variant.
3. Check two seed trajectories, feature attribution, completion gate activity, final patch and
   expected official layout. Confirm no repair worker and no longest-patch selector.
4. Evaluate unchanged generated artifacts using the pinned official scorer. Check process return,
   discovery, both feature results and infrastructure errors; do not require a passing task score
   merely to establish runtime readiness.
5. Expand to a small explicit cross-toolchain subset; all-pairs run only once images/resources are
   qualified and no tasks are silently skipped.

Split evaluation option: use only user-owned changyu-dev if selected; stage artifacts and run
official Docker evaluator there. No other Google Cloud VM may be accessed.
All-NLP option: official backend adapter requires additional work in a scoped CooperBench fork
or a documented public extension point, with evaluator parity checks. No monkey-patching or
duplicating official scoring as an ad hoc shortcut.

## Scope and estimate

Worker runtime + launcher + focused validation: approximately 2–4 engineering days once inputs
and endpoint exist. Native NLP official evaluation: additional roughly 2–4 days if required.
Image conversion waits, inference hosting and benchmark runtime/cost are separate.

No training, model checkpoint conversion, full fin-min restoration, new agent framework,
changes to task semantics, or full benchmark launch are included in this preparation phase.

## References

Sampling implementation note: shared settings live in `src/cooperagents/sampling.py`, read by
mini-SWE build_model and the planner client used by coordinator. OpenAI-nonstandard fields use
extra_body. OpenRouter reasoning.enabled=false requests non-thinking; exclude=true alone would
only hide reasoning. provider.require_parameters=true rejects routes missing explicit parameter
support; it does not replace a live provider conformance test. Current endpoint metadata advertises
all six sampling controls plus tools/reasoning for DeepInfra and Together; provider not yet pinned.
The Qwen model card has conflicting parameter sets between its quickstart and Best Practices;
the explicit user-selected values above are authoritative for this experiment.
Fourteen related offline tests passed, using a mocked SDK transport, without model/network calls.

- https://huggingface.co/Qwen/Qwen3.5-9B#using-qwen35-via-the-chat-completions-api
- https://openrouter.ai/api/v1/models/qwen/qwen3.5-9b/endpoints

- Updated research: `/Users/cameron_chen/Desktop/Code/cooperator/polar-ext/thoughts/shared/research/2026-09-21-cooperbench-fin-min-integration-effort.md`
- Historical NLP Docker probe: `/Users/cameron_chen/Desktop/Code/cooperator/polar-ext/thoughts/shared/research/2026-09-05-nlp-coordinator-eval-environment.md`
- Official SC compute-node environment guidance: https://cluster.cs.stanford.edu/sc/
- Official evaluator backend registry: https://github.com/cooperbench/CooperBench/blob/63b9d44d9f39a02fccf5bf0052db48a917a011fd/src/cooperbench/eval/backends/__init__.py

## Implementation progress (2026-09-21)

All-NLP evaluation selected: writable Apptainer worker and evaluator sandboxes,
loopback dummy HTTP service, pinned official backend injection patch, CPU launcher
and committed-source run snapshots implemented. Dataset validation checks all 652
pairs. Image job 17547420 completed on john17. Dummy development runs identified
and fixed missing evaluator import dependencies and unavailable make discovery.
Run 17547498 exercised one deliberate gate rejection followed by successful
completion per worker, and confirmed every selected sampling field on the wire.
Final committed-source smoke and gold evaluator checks are pending.

Local focused adapter/sampling/planner/eval checks: 29 passed, 2 integration skips.
Two repair-integrator harness tests fail identically on clean sagemaker HEAD and
the working branch in this local environment; repair is disabled in this profile.
Full benchmark image qualification remains per-subset; no all-pairs model launch
is included in this preparation. Real API/provider conformance remains deferred.
