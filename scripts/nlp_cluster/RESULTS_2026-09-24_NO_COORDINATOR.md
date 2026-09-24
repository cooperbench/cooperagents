# Qwen3.5-9B coordinator ablation on cb-mixture-36

Three independent no-coordinator rounds completed on the same 36 training
pairs as the [non-thinking coordinator-on baseline](RESULTS_2026-09-23.md).
This ablation keeps two non-thinking workers, cooperation tools, shared Git,
and the completion gate. It is therefore a coordinator ablation, not an exact
replica of unmodified CooperBench coop mode.

## Fixed setup and provenance

- Dataset: [`cb-mixture-36`](../../datasets/cb-mixture-36/manifest.json), 36
  exact feature pairs across 17 qualified task images. Upstream CooperBench is
  `63b9d44d9f39a02fccf5bf0052db48a917a011fd`; its evaluator copy has only
  the byte-preserving large-patch transport fix. Benchmark tests and reference
  patches were unchanged.
- Model: Qwen3.5-9B on the five-replica NLP SGLang router, with chat-template
  thinking disabled, temperature `1.0`, top-p `0.95`, top-k `20`, and presence
  penalty `1.5`. The private model profile stayed outside source and run
  records. This was not an OpenRouter experiment.
- Harness: two mini-SWE workers per pair, `--coop-tools`, `--git-share`, and
  `--completion-gate` enabled; `--coordinator`, repair, and presub-merge
  disabled. Each worker had at most 1,000 steps and 3,600 seconds. Each round
  generated up to ten pairs concurrently and evaluated them serially on
  `visionlab-dgx1` with 20 CPUs, 128 GiB, and an eight-hour Slurm limit.
- Source: `0aad5fe0aa193ec21614cbb26d1a186973680241`. The baseline used
  `676daba`; the intervening worker-thinking override was inactive in this
  non-thinking profile. The no-coordinator source adds the launcher switch.
- Campaign manifest, exact job/run records, and machine-readable summary:
  `/nlp/scr/chency/projects/cooperagents/runs/20260923-cb36x3-no-coordinator-0aad5fe/`.

## Official results

All three jobs ended `COMPLETED` (`0:0`), with 36 `result.json` and 36
official `eval.json` files each. None of the evaluations reports an error.
Feature passes count the two official tests separately; a pair passes only
when both features pass in the same evaluation.

| Round | Slurm job | Feature passes | Both-feature pairs | Workers submitted / limited | Coordinator events | Elapsed | Batch MaxRSS |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 17574961 | 12/72 | 4/36 | 54 / 18 | 0 | 3:24:05 | 37658619K |
| 2 | 17575034 | 8/72 | 3/36 | 53 / 19 | 0 | 3:36:49 | 40351745K |
| 3 | 17575036 | 14/72 | 5/36 | 57 / 15 | 0 | 3:36:48 | 6873190K |

Across three rounds, no-coordinator passed **34/216 feature attempts** and
**12/108 pair attempts**. Ten distinct pairs passed both features at least
once; 14 distinct pairs passed at least one feature. The non-thinking
coordinator-on baseline passed **30/216 features** and **9/108 pairs**, with
six distinct pairs passing both features at least once. Across the 36 pair
identities, the number of both-feature successes over three rounds increased
for eight pairs, decreased for four, and stayed equal for 24. These runs show
no benefit from the coordinator on this selection, but three unseeded rounds
and low pass rates do not establish a general causal or model-quality result.

## Execution caveats

- `huggingface_datasets_task:3997:2,4` had `merge.status=missing_input` in
  all three no-coordinator rounds: agent 1's input failed to apply and both
  official features failed. The baseline also had `missing_input` for this
  pair in round 1, but its rounds 2 and 3 applied cleanly and still failed.
  These are scored failures, not missing evaluation files.
- The jobs recorded no cgroup OOM kill during monitoring. Batch `MaxRSS`
  excludes some container memory and should not be read as total job memory.
  The model client logged 240-second request timeouts and retried them while
  generation continued.
- The 36 pairs are the training selection, not a held-out test set. Keep the
  three rounds separate when reporting variability; do not select only the
  best round as the setting's score.
