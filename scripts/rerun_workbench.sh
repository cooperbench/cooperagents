#!/usr/bin/env bash
set -uo pipefail
cd /Users/i776966/workspace/cooperagents
M=sap/gemini-2.5-flash
run() { echo "===== $* ====="; uv run python scripts/scaling_box.py "$@" --models "$M" || echo "FAILED: $*"; }
for d in email calendar analytics project_management customer_relationship_manager multi_domain; do
  run --benchmark workbench --split "$d" --team-size 3 --step-limit 30 --out-dir "runs/scaling/wb_${d}_v2"
done
echo "===== WORKBENCH 690 RE-RUN DONE ====="
