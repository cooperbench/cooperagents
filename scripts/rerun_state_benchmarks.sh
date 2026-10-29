#!/usr/bin/env bash
# Re-run the four STATE benchmarks after the prose-lapse loop fix, for clean
# before/after comparability. Writes to *_v2 out-dirs so the pre-fix runs are
# preserved. Model: sap/gemini-2.5-flash. Finance uses the keyless ddg backend.
set -uo pipefail
cd /Users/i776966/workspace/cooperagents
export FINANCE_DATA=data/finance_open50.csv
export FINANCE_GRADER_MODEL=sap/gemini-2.5-flash
export FINANCE_SEARCH=ddg
M=sap/gemini-2.5-flash

run() { echo "===== $* ====="; uv run python scripts/scaling_box.py "$@" --models "$M" || echo "FAILED: $*"; }

# Quick three + Finance first (smaller), then WorkBench (the long pole) by domain.
run --benchmark plancraft  --split val.small --team-size 3 --step-limit 15 --out-dir runs/scaling/plancraft_v2
run --benchmark browsecomp --split test       --team-size 3 --step-limit 20 --out-dir runs/scaling/browsecomp_v2
run --benchmark finance    --limit 50          --team-size 3 --step-limit 25 --out-dir runs/scaling/finance_v2

for d in email calendar analytics project_management customer_relationship_manager multi_domain; do
  run --benchmark workbench --split "$d" --team-size 3 --step-limit 30 --out-dir "runs/scaling/wb_${d}_v2"
done
echo "===== ALL STATE RE-RUNS DONE ====="
