# Explicit published repair training

`run_repair_checkpoint(..., published=reference, purpose="training", ...)` executes new
integrator actions from authenticated Legacy/V3 task state. `purpose="difficulty"`
remains available for evaluation. Omitted/unknown purposes are rejected for publications;
native task checkpoints keep their existing contract. Direct V3 captures use the same
explicit purpose through their authenticated publication locator.

Both purposes use the same node, runtime, source-profile, payload/inventory, image, Git,
patch and namespace checks. The purpose travels into the per-rollout holder process and
is recorded as `execution_purpose` in restore metrics. No writable sandbox is shared.

`training_eligible=false` and `repair_validated=false` remain immutable producer claims:
this API does not retroactively certify historical completeness, model repair ability or
feature correctness. Explicit training is consumer opt-in to using these experimental
snapshots, with their recorded risks. Training selection and reward policy belong to the
caller; the API does not select by reward, turn infrastructure errors into failed repairs,
retry model calls or run an optimizer. Missing or tampered state still fails explicitly.

The model-free tests exercise both formats and both purposes through the official repair
loop with a fake container and completion binding, including commit, patch export, saved
budgets and cleanup. They do not establish real overlay/container or model-training
performance on a cluster. Consumers must pin this API revision and qualify their actual
execution environment before launch.
