#!/usr/bin/env bash
set -uo pipefail
cd /Users/i776966/workspace/cooperagents
export FINANCE_DATA=data/finance_open50.csv
export FINANCE_GRADER_MODEL=sap/gemini-2.5-flash
export FINANCE_SEARCH=ddg
M=sap/gemini-2.5-flash
run() { echo "===== $* ====="; uv run python scripts/scaling_box.py "$@" --models "$M" || echo "FAILED: $*"; }
run --benchmark plancraft  --split val.small --team-size 3 --step-limit 15 --out-dir runs/scaling/plancraft_v2
run --benchmark browsecomp --split test      --team-size 3 --step-limit 20 --out-dir runs/scaling/browsecomp_v2
run --benchmark finance    --limit 50         --team-size 3 --step-limit 25 --out-dir runs/scaling/finance_v2
echo "===== SMALL-3 RE-RUN DONE ====="
