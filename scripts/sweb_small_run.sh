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
STEP=80

for id in $INSTANCES; do
  echo "===== SOLO $id ====="
  uv run python scripts/bench_programbench.py --instance "$id" --arm solo --rep small --step-limit $STEP --agent-time-limit 1800 --env-brief --runs-dir "$RUNS" || echo "SOLO $id FAILED (continuing)"
done

for id in $INSTANCES; do
  echo "===== TEAM $id ====="
  uv run python scripts/bench_programbench.py --instance "$id" --arm coopgitc2 --rep small --team-size 3 --repair --completion-gate --env-brief --presub-merge --step-limit $STEP --agent-time-limit 2400 --runs-dir "$RUNS" || echo "TEAM $id FAILED (continuing)"
done

echo "===== COMBINE PREDICTIONS ====="
cat $RUNS/pb-solo-small/*/predictions.jsonl > $RUNS/solo_predictions.jsonl
cat $RUNS/pb-coopgitc2-small/*/predictions.jsonl > $RUNS/team_predictions.jsonl
wc -l $RUNS/solo_predictions.jsonl $RUNS/team_predictions.jsonl

echo "===== GRADE SOLO ====="
uv run python -m swebench.harness.run_evaluation --dataset_name data/swebench_grade_six.jsonl --predictions_path $RUNS/solo_predictions.jsonl --run_id sweb_small_solo --max_workers 4

echo "===== GRADE TEAM ====="
uv run python -m swebench.harness.run_evaluation --dataset_name data/swebench_grade_six.jsonl --predictions_path $RUNS/team_predictions.jsonl --run_id sweb_small_team --max_workers 4

echo "===== DONE ====="
