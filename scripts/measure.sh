#!/usr/bin/env bash
# One-command measurement for the self-improvement loop.
# Runs solo + team-shared (mini-swe worker) on the FIXED 10-pair
# benchmark and prints pass-rate. Usage: scripts/measure.sh [run-label]
# Model profile: ENV_FILE=.env.qwen scripts/measure.sh qwen-base  (default .env = GPT-5.5)
set -euo pipefail
cd "$(dirname "$0")/.."

# Load model creds from the selected profile.
ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then set -a; . "./$ENV_FILE"; set +a; fi
export LITELLM_LOG=ERROR

# Most CooperBench flash images are arm64-only; ensure emulation on amd64 hosts.
docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null 2>&1 || true

LABEL="${1:-cmp}"

# The FIXED benchmark set — keep this identical across runs so deltas compare.
PAIRS="\
dottxt_ai_outlines_task:1655:6,7 \
dottxt_ai_outlines_task:1655:7,10 \
dottxt_ai_outlines_task:1706:4,6 \
dottxt_ai_outlines_task:1706:5,8 \
dspy_task:8394:3,4 \
dspy_task:8394:3,5 \
go_chi_task:26:1,2 \
go_chi_task:56:1,5 \
huggingface_datasets_task:3997:2,4 \
huggingface_datasets_task:6252:4,6"

# shellcheck disable=SC2086
uv run python scripts/bench_compare.py \
  --pairs $PAIRS \
  --concurrency 3 --eval-concurrency 4 --step-limit 50 \
  --solo-name "${LABEL}-solo" --team-name "${LABEL}-team"
