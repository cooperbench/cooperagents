#!/usr/bin/env bash
# Per-boot job wrapper, installed to /opt/cooperagents/deploy and run by the
# cooperagents-job systemd unit on EVERY boot. A spot stop->start therefore
# auto-resumes the run (run_benchmarks.sh is idempotent). On clean completion it
# marks DONE in S3, disables itself, cancels the spot request, and self-terminates
# so nothing is left running or costing money.
set -uo pipefail
cd /opt/cooperagents
# shellcheck disable=SC1091
source deploy/job.env
LOG=/var/log/cooperagents-job.log
exec >>"$LOG" 2>&1
echo "=== boot_run $(date -u) ==="

# IMDSv2 self-identity
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
IID=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
REGION=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
export AWS_DEFAULT_REGION="$REGION"

# Reachability check: a 401 proves the AI Core endpoint is reachable + auth-gated
# (not network-gated). Abort the boot run if it is unreachable (e.g. no route).
code=$(curl -sS -o /dev/null -m 15 -w '%{http_code}' "https://${AICORE_HOST}/v2/lm/deployments" 2>/dev/null || echo 000)
echo "AI Core reachability: http_code=$code  (401 = reachable, good)"
if [ "$code" = "000" ]; then
  echo "AI Core NOT reachable from this box — leaving instance up for inspection."
  exit 1
fi

bash deploy/run_benchmarks.sh
rc=$?
aws s3 sync "${RUNS_DIR:-runs/remote}" "s3://$S3_BUCKET/${S3_PREFIX:-cooperagents}/runs/" --only-show-errors || true

if [ $rc -eq 0 ]; then
  echo "job complete rc=0 — DONE, cancelling spot request, self-terminating"
  echo "done $(date -u)" | aws s3 cp - "s3://$S3_BUCKET/${S3_PREFIX:-cooperagents}/DONE.txt" || true
  systemctl disable cooperagents-job.service || true
  SIR=$(aws ec2 describe-instances --instance-ids "$IID" \
        --query 'Reservations[0].Instances[0].SpotInstanceRequestId' --output text 2>/dev/null || echo None)
  if [ -n "$SIR" ] && [ "$SIR" != "None" ]; then
    aws ec2 cancel-spot-instance-requests --spot-instance-request-ids "$SIR" || true
  fi
  aws ec2 terminate-instances --instance-ids "$IID" || true
else
  echo "job exited rc=$rc — NOT terminating; will resume on next boot / inspect via SSH"
fi
