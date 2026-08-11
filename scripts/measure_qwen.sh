#!/usr/bin/env bash
# Qwen3.5-9B measurement for the self-improvement loop.
# Runs solo + team (mini-swe worker, Qwen3.5-9B on Modal) on the FIXED
# qwen-calibrated 14-pair set and prints pair- and feature-level pass-rates.
# Usage: scripts/measure_qwen.sh [run-label] [extra bench_compare flags...]
#
# Set provenance (2026-07-31): from a 45-pair solo sweep of flash (fixed-10 +
# 40-pair calibration, step-limit 50), qwen solo passes ~11% at pair level.
# This set = the 5 full-pass + 8 half-pass pairs + 1 hard sibling — all fast
# Python evals (typst excluded: ~1h/pair Rust eval under QEMU). Keep it FIXED
# so deltas compare; measure at FEATURE level (28 features) for resolution.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${ENV_FILE:-.env.qwen}"
set -a; . "./$ENV_FILE"; set +a
export LITELLM_LOG=ERROR

docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true

LABEL="${1:-qwen}"
shift || true

PAIRS="\
openai_tiktoken_task:0:3,6 \
openai_tiktoken_task:0:6,8 \
pallets_jinja_task:1465:2,3 \
dottxt_ai_outlines_task:1706:4,6 \
huggingface_datasets_task:6252:4,6 \
llama_index_task:18813:1,5 \
pallets_click_task:2068:1,6 \
pallets_click_task:2068:2,10 \
pallets_click_task:2800:1,3 \
pallets_jinja_task:1621:2,9 \
pillow_task:290:2,3 \
pillow_task:290:4,5 \
samuelcolvin_dirty_equals_task:43:2,3 \
samuelcolvin_dirty_equals_task:43:3,7"

# shellcheck disable=SC2086
uv run python scripts/bench_compare.py \
  --pairs $PAIRS \
  --concurrency 4 --eval-concurrency 4 --step-limit 50 \
  --solo-name "${LABEL}-solo" --team-name "${LABEL}-team" "$@"
