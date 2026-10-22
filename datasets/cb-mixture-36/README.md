# cb-mixture-36

All 36 unique feature pairs from `qwen-14` (14), `dev-set-2` (14), and
`flash-10` / `fixed-10` (10), assigned to **train**. They cover 17 repository
tasks across 10 repositories. A training sample is a feature pair, not a
repository task; different pairs on the same task remain separate samples.

Deduplication uses `(repo, task_id, sorted feature IDs)`. The two overlaps are
`dottxt_ai_outlines_task:1706:4,6` and `huggingface_datasets_task:6252:4,6`,
both shared by qwen-14 and flash-10. Output order is sorted by that key.

## Files and provenance

- `manifest.json`: native CooperBench subset structure (`tasks[].pairs`),
  source script paths and SHA-256 hashes, counts, and pinned CooperBench revision.
- `pairs.txt`: the same 36 samples, one `repo:task:f1,f2` per line, for existing
  `--pairs` arguments.

Task specifications and scoring assets come from CooperBench revision
`63b9d44d9f39a02fccf5bf0052db48a917a011fd`. Every selected pair was checked
against its `all.json`; each selected feature has nonempty `feature.md`,
`feature.patch`, and `tests.patch`. These upstream assets are not duplicated here.
Workers receive feature specifications; reference patches and hidden tests
remain evaluator inputs.

## Use

From the cooperagents repository root, with the pinned CooperBench checkout
available through `COOPERBENCH_DIR`, pass the list to an existing rollout command:

```bash
uv run python scripts/bench_compare.py \
  --pairs $(cat datasets/cb-mixture-36/pairs.txt) \
  --team-only --no-seed --coop-tools --git-share --coordinator --completion-gate
```

This command runs model rollouts and evaluation; creating this dataset does not
run it. The same `--pairs` list is accepted by `scripts/nlp_cluster/submit.py`.
For consumers using `load_subset`, install `manifest.json` as
`$COOPERBENCH_DIR/dataset/subsets/cb-mixture-36.json` and select
`--subset cb-mixture-36`.

This is a task selection dataset for rollout-based training, not an SFT dataset
with generated answers or a coordinator replay JSONL. Those require separate
trajectory generation and the corresponding training adapter. No validation
or test split is reserved. After training on this union, the original three
subsets cannot serve as held-out evaluations; task-disjoint evaluation must
also exclude all 17 selected `(repo, task_id)` identities.

Verify the frozen union against its source scripts without network or model calls:

```bash
uv run pytest tests/test_training_union.py
```
