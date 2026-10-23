#!/usr/bin/env bash
set -uo pipefail
cd /Users/i776966/workspace/cooperagents
export COOPER_BENCHMARK=swebench
export SWEBENCH_DATA=data/swebench_verified.jsonl
export SWEBENCH_IMAGE_TEMPLATE='swebench/sweb.eval.arm64.{key}:latest'
export AZURE_OPENAI_DEPLOYMENT=sap/gemini-2.5-flash
export SWEBENCH_MAX_WORKERS=4
RUNS=runs/sweb_small
INSTANCES="pylint-dev__pylint-4551 pallets__flask-5014 sympy__sympy-22914 django__django-16082 pytest-dev__pytest-6202 sphinx-doc__sphinx-8621"
for id in $INSTANCES; do
  echo "===== TEAM $id ====="
  uv run python scripts/bench_programbench.py --instance "$id" --arm coopgitc2 --rep small --team-size 3 --repair --completion-gate --env-brief --presub-merge --step-limit 80 --agent-time-limit 2400 --runs-dir "$RUNS" || echo "TEAM $id FAILED (continuing)"
done
echo "===== COMBINE + GRADE TEAM ====="
cat $RUNS/pb-coopgitc2-small/*/predictions.jsonl > $RUNS/team_predictions.jsonl
uv run python -m swebench.harness.run_evaluation --dataset_name data/swebench_grade_six.jsonl --predictions_path $RUNS/team_predictions.jsonl --run_id sweb_small_team2 --max_workers 4
echo "===== DONE ====="
