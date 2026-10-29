#!/usr/bin/env bash
# One-time cloud-init bootstrap (runs as root on first boot). Placeholders
# (__FOO__) are substituted by launch_cpu_spot.sh before launch. Installs
# Docker + Python 3.12 + uv, lays down the repo/data/creds, and installs a
# systemd unit that runs the benchmark job on every boot (so a spot stop->start
# auto-resumes). Secrets come from SSM SecureString params, never from here.
set -uo pipefail
exec >>/var/log/cooperagents-boot.log 2>&1
echo "=== user-data bootstrap $(date -u) ==="

S3_BUCKET="__S3_BUCKET__"
S3_PREFIX="__S3_PREFIX__"
BENCH="__BENCH__"
AICORE_HOST="__AICORE_HOST__"
SSM_AICORE="__SSM_AICORE__"
SSM_ENV="__SSM_ENV__"
REPO_S3="__REPO_S3__"
DATA_S3="__DATA_S3__"
SWEB_INSTANCES="__SWEB_INSTANCES__"
TB_TASKS="__TB_TASKS__"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io git curl unzip python3.12 python3.12-venv
systemctl enable --now docker

# Use the m6id local NVMe for Docker data-root + scratch (fast layer unpack).
NVME=$(lsblk -dpno NAME,MODEL | awk '/Instance Storage|EC2 NVMe/{print $1; exit}')
if [ -n "$NVME" ]; then
  mkfs.ext4 -F "$NVME" && mkdir -p /mnt/nvme && mount "$NVME" /mnt/nvme
  systemctl stop docker
  mkdir -p /mnt/nvme/docker
  echo '{"data-root":"/mnt/nvme/docker"}' > /etc/docker/daemon.json
  systemctl start docker
fi

# AWS CLI v2
curl -sS "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install || true

# uv (installs for root)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"

# Repo from a private S3 tarball (no git creds needed on the box).
cd /opt
aws s3 cp "$REPO_S3" /tmp/repo.tar.gz
mkdir -p /opt/cooperagents
tar xzf /tmp/repo.tar.gz -C /opt/cooperagents --strip-components=1
cd /opt/cooperagents
uv venv --python 3.12
uv pip install -e '.[llm,mini,benchmarks,dev]' swebench terminal-bench ddgs

# Data files (swebench_verified.jsonl, terminal-bench-core-0.1.1/, finance csv, ...)
if [ -n "$DATA_S3" ]; then aws s3 sync "$DATA_S3" /opt/cooperagents/data/ --only-show-errors || true; fi

# Credentials from SSM SecureString (decrypted via the instance IAM role).
aws ssm get-parameter --name "$SSM_AICORE" --with-decryption --query 'Parameter.Value' --output text > /opt/cooperagents/aicore.json
aws ssm get-parameter --name "$SSM_ENV"    --with-decryption --query 'Parameter.Value' --output text > /opt/cooperagents/.env

cat > /opt/cooperagents/deploy/job.env <<EOF
export S3_BUCKET="$S3_BUCKET"
export S3_PREFIX="$S3_PREFIX"
export BENCH="$BENCH"
export AICORE_HOST="$AICORE_HOST"
export RUNS_DIR="runs/remote"
export SWEB_INSTANCES="$SWEB_INSTANCES"
export TB_TASKS="$TB_TASKS"
EOF

cat > /etc/systemd/system/cooperagents-job.service <<'UNIT'
[Unit]
Description=cooperagents benchmark job
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/cooperagents
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/bin/bash /opt/cooperagents/deploy/boot_run.sh
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now cooperagents-job.service
echo "=== bootstrap complete; job service started $(date -u) ==="
