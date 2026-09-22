#!/usr/bin/env bash
set -euo pipefail
: "${COOPER_CODE:?}" "${COOPER_RUN:?}" "${COOPERBENCH_DIR:?}"
export COOPER_SCRATCH="/scr/$USER/cooperagents/qualification/${SLURM_ARRAY_JOB_ID:?}/${SLURM_ARRAY_TASK_ID:?}"
export APPTAINER_CACHEDIR="$COOPER_SCRATCH/cache" APPTAINER_TMPDIR="$COOPER_SCRATCH/build"
export PYTHONPATH="$COOPER_CODE/src:$COOPERBENCH_DIR/src" LITELLM_LOCAL_MODEL_COST_MAP=True
mkdir -p "$COOPER_SCRATCH" "$APPTAINER_TMPDIR"
df -h "$COOPER_SCRATCH"
python3 -m venv "$COOPER_SCRATCH/venv"
"$COOPER_SCRATCH/venv/bin/pip" install -q 'litellm==1.99.0' 'openai==2.54.0' rich tenacity jinja2 pydantic pyyaml modal redis python-dotenv platformdirs docker
"$COOPER_SCRATCH/venv/bin/python" "$COOPER_CODE/scripts/nlp_cluster/qualify.py" \
  "$COOPER_CODE/datasets/cb-mixture-36/manifest.json" "$COOPER_RUN/reports"
