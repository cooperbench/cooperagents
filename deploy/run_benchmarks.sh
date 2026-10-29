#!/usr/bin/env bash
# Benchmark command set for the remote x86_64 job. Idempotent: the underlying
# runners reuse finished work (scaling_box --resume; the SWE/TB runners skip
# instances that already have output), so a re-invocation after a spot
# stop->start continues rather than restarts. Driven by env (see deploy/job.env):
#   BENCH             swebench | terminalbench | all          (default swebench)
#   COOPER_MODEL      model id                                (default sap/gemini-2.5-flash)
#   S3_BUCKET/S3_PREFIX  result sync target
#   SWEB_INSTANCES    space-separated ids; empty = all 500
#   TB_TASKS          comma-separated TB tasks; empty = curated light subset
#   STEP_LIMIT        per-agent step cap
set -uo pipefail
cd "${REPO_DIR:-/opt/cooperagents}"

MODEL="${COOPER_MODEL:-sap/gemini-2.5-flash}"
export AZURE_OPENAI_DEPLOYMENT="$MODEL"
RUNS="${RUNS_DIR:-runs/remote}"
mkdir -p "$RUNS"

sync_s3() { [ -n "${S3_BUCKET:-}" ] && aws s3 sync "$RUNS" "s3://$S3_BUCKET/${S3_PREFIX:-cooperagents}/runs/" --only-show-errors || true; }

run_swebench() {
  export COOPER_BENCHMARK=swebench
  export SWEBENCH_DATA=data/swebench_verified.jsonl
  # x86_64 = canonical grading arch with full image coverage (no arm64 gap).
  export SWEBENCH_IMAGE_TEMPLATE='swebench/sweb.eval.x86_64.{key}:latest'
  export SWEBENCH_MAX_WORKERS="${SWEBENCH_MAX_WORKERS:-6}"
  local ids="${SWEB_INSTANCES:-}"
  if [ -z "$ids" ]; then
    ids=$(.venv/bin/python -c "import json;print(' '.join(json.loads(l)['instance_id'] for l in open('data/swebench_verified.jsonl')))")
  fi
  SWEB_IDS="$ids" uv run python deploy/build_grade_set.py > data/swebench_grade_remote.jsonl
  local n=0
  for id in $ids; do
    n=$((n+1)); echo "===== [$n] SWE $id ====="
    uv run python scripts/bench_programbench.py --instance "$id" --arm solo --rep remote \
      --step-limit "${STEP_LIMIT:-80}" --agent-time-limit 1800 --env-brief --runs-dir "$RUNS/sweb" || echo "SOLO $id failed"
    uv run python scripts/bench_programbench.py --instance "$id" --arm coopgitc2 --rep remote --team-size 3 \
      --repair --completion-gate --env-brief --presub-merge \
      --step-limit "${STEP_LIMIT:-80}" --agent-time-limit 2400 --runs-dir "$RUNS/sweb" || echo "TEAM $id failed"
    sync_s3
  done
  cat "$RUNS"/sweb/pb-solo-remote/*/predictions.jsonl > "$RUNS/sweb/solo_predictions.jsonl" 2>/dev/null || true
  cat "$RUNS"/sweb/pb-coopgitc2-remote/*/predictions.jsonl > "$RUNS/sweb/team_predictions.jsonl" 2>/dev/null || true
  for arm in solo team; do
    uv run python -m swebench.harness.run_evaluation --dataset_name data/swebench_grade_remote.jsonl \
      --predictions_path "$RUNS/sweb/${arm}_predictions.jsonl" --run_id "remote_${arm}" \
      --max_workers "$SWEBENCH_MAX_WORKERS" || echo "grade $arm failed"
  done
  cp -f cooperagents.remote_*.json "$RUNS/sweb/" 2>/dev/null || true
}

run_terminalbench() {
  # On x86_64 the x86-pinned TB tasks run native (no emulation). Curated light
  # subset by default; set TB_TASKS to a comma-separated list to override.
  local extra=""
  [ -n "${TB_TASKS:-}" ] && extra="--tasks ${TB_TASKS}"
  uv run python scripts/tb_core_box.py --model "$MODEL" --team-size 3 \
    --step-limit "${STEP_LIMIT:-50}" --agent-time-limit 900 \
    --out-dir "$RUNS/tbcore" $extra || echo "TB failed"
}

case "${BENCH:-swebench}" in
  swebench)      run_swebench ;;
  terminalbench) run_terminalbench ;;
  all)           run_swebench; run_terminalbench ;;
  *) echo "unknown BENCH='${BENCH}'"; exit 2 ;;
esac
sync_s3
echo "===== run_benchmarks done ($BENCH) ====="
