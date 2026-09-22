#!/usr/bin/env bash
set -euo pipefail
: "${COOPER_CODE:?}" "${COOPER_RUN:?}" "${COOPERBENCH_DIR:?}" "${COOPER_IMAGE_MANIFEST:?}"
mkdir -p "$COOPER_RUN"/{logs,data,rendered-config}
trap 'code=$?; if [ "$code" = 0 ]; then echo completed; else echo failed:$code; fi > "$COOPER_RUN/status.txt"' EXIT
echo running > "$COOPER_RUN/status.txt"
export COOPER_RUNTIME=apptainer
export COOPER_SCRATCH="${SLURM_TMPDIR:-/scr/$USER}/cooperagents/${SLURM_JOB_ID:?}"
export APPTAINER_CACHEDIR="/nlp/scr/$USER/apptainer-cache"
export APPTAINER_TMPDIR="$COOPER_SCRATCH/build"
mkdir -p "$COOPER_SCRATCH" "$APPTAINER_TMPDIR"
cd "$COOPER_CODE"
python3 -m venv "$COOPER_SCRATCH/venv"
"$COOPER_SCRATCH/venv/bin/pip" install -q 'litellm==1.99.0' 'openai==2.54.0' rich tenacity jinja2 pydantic pyyaml modal redis python-dotenv platformdirs docker
export PATH="$COOPER_SCRATCH/venv/bin:$PATH"
export PYTHONPATH="$COOPER_CODE/src:$COOPERBENCH_DIR/src"
export LITELLM_LOCAL_MODEL_COST_MAP=True
python -c "import cooperbench.eval"
python -m pip freeze > "$COOPER_RUN/data/dependencies.txt"
python scripts/nlp_cluster/prepare.py --check-only --images unused --manifest unused
cp "$COOPER_IMAGE_MANIFEST" "$COOPER_RUN/data/images.json"
python - <<'PYHASH'
import hashlib, json, os
from pathlib import Path
images = json.loads(Path(os.environ['COOPER_IMAGE_MANIFEST']).read_text())
checksums = {}
for image, path in images.items():
    with open(path, 'rb') as handle:
        checksums[image] = hashlib.file_digest(handle, 'sha256').hexdigest()
(Path(os.environ['COOPER_RUN']) / 'data/images.sha256.json').write_text(json.dumps(checksums, indent=2))
PYHASH
cp scripts/nlp_cluster/patches/cooperbench-backend.patch "$COOPER_RUN/data/"
sha256sum scripts/nlp_cluster/patches/cooperbench-backend.patch > "$COOPER_RUN/data/evaluator-patch.sha256"
if [ "${COOPER_MODE:-dummy}" = dummy ]; then
  python scripts/nlp_cluster/dummy_smoke.py "$COOPER_RUN"
else
  : "${COOPER_CREDENTIAL_FILE:?}"
  # Credential file remains outside the immutable snapshot and run record.
  set -a
  source "$COOPER_CREDENTIAL_FILE"
  set +a
  : "${OPENAI_API_KEY:?}"
  export ENV_FILE="$COOPER_CREDENTIAL_FILE"
  read -ra pairs <<< "${COOPER_PAIRS:?}"
  args=(--pairs "${pairs[@]}" --team-only --max-agents 2 --no-seed --coop-tools --git-share
        --coordinator --completion-gate --step-limit 30 --eval-concurrency 1
        --team-name real --log-dir "$COOPER_RUN/logs")
  printf '%s\n' "${args[@]}" > "$COOPER_RUN/training-args.txt"
  python scripts/bench_compare.py "${args[@]}"
fi
