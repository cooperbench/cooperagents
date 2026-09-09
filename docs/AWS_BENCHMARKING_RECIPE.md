# CooperBench on AWS — Benchmarking Recipe

End-to-end instructions for running the CooperBench solo evaluation with Qwen3.8-27B on AWS EC2.

Covers: instance selection → spot price check → region setup → instance launch → driver setup → model download → vLLM setup → benchmark run → result collection → cleanup.

---

## Prerequisites

- AWS CLI configured with a profile that has EC2 permissions (`<YOUR_AWS_PROFILE>` below — substitute your profile name).
- SSH key pair registered in AWS and the `.pem` file at `~/.ssh/<YOUR_KEY_PAIR>.pem` on your local machine.
- Access to the `cooperagents` and `CooperBench` GitHub repos.
- A HuggingFace account with access to `Qwen/Qwen3.8-27B`.

---

## 1. Find the Cheapest Instance and Region

### Instance options

| Instance | GPU | Spot (typical) | On-demand | Est. Run Time | Est. Total (spot) |
|---|---|---|---|---|---|
| p4d.24xlarge | 8× A100 40GB | $6.50–$19/hr | $21.96/hr | ~50h | ~$325–$950 |
| p5.48xlarge | 8× H100 80GB | $20–$22/hr | $55.04/hr | ~20h | ~$400–$440 |

H100 and A100 come out to roughly the same total cost. H100 finishes 2.5× faster.

### Check spot prices across regions

Run this to find the cheapest AZ for your chosen instance type:

```bash
# us-east-1
aws ec2 describe-spot-price-history \
  --instance-types p4d.24xlarge p5.48xlarge \
  --product-descriptions "Linux/UNIX" \
  --start-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'SpotPriceHistory[*].{AZ:AvailabilityZone,Instance:InstanceType,Price:SpotPrice}' \
  --output table --profile <YOUR_AWS_PROFILE>

# Other regions (repeat with --region us-west-2, eu-central-1, ap-northeast-1 etc.)
aws ec2 describe-spot-price-history \
  --region ap-northeast-1 \
  --instance-types p4d.24xlarge p5.48xlarge \
  --product-descriptions "Linux/UNIX" \
  --start-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'SpotPriceHistory[*].{AZ:AvailabilityZone,Instance:InstanceType,Price:SpotPrice}' \
  --output table --profile <YOUR_AWS_PROFILE>
```

Pick the cheapest AZ. Note: a low spot price does not guarantee capacity — see Step 2.

### Set up a new region (skip if using us-east-1)

If your cheapest AZ is in a different region, you need to set up a key pair and security group there first.

```bash
# Import your existing key pair
aws ec2 import-key-pair \
  --region <REGION> \
  --key-name <YOUR_KEY_PAIR> \
  --public-key-material fileb://<(ssh-keygen -y -f ~/.ssh/<YOUR_KEY_PAIR>.pem) \
  --profile <YOUR_AWS_PROFILE>

# Create security group
SG=$(aws ec2 create-security-group \
  --region <REGION> \
  --group-name cooperbench-vllm \
  --description "SSH and vLLM port for cooperbench" \
  --profile <YOUR_AWS_PROFILE> \
  --query 'GroupId' --output text)

# Add SSH and vLLM rules from your current IP
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress \
  --region <REGION> \
  --group-id $SG \
  --ip-permissions \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${MY_IP}/32}]" \
    "IpProtocol=tcp,FromPort=8000,ToPort=8000,IpRanges=[{CidrIp=${MY_IP}/32}]" \
  --profile <YOUR_AWS_PROFILE>

echo "Security group: $SG"
```

Get the AMI for the new region (AMI IDs are region-specific):

```bash
aws ec2 describe-images \
  --region <REGION> \
  --owners amazon \
  --filters "Name=name,Values=*Deep Learning*PyTorch*Ubuntu*20.04*" \
  --query 'sort_by(Images, &CreationDate)[-1].{ImageId:ImageId,Name:Name}' \
  --output table --profile <YOUR_AWS_PROFILE>
```

Get the subnet for your target AZ:

```bash
aws ec2 describe-subnets \
  --region <REGION> \
  --filters "Name=availabilityZone,Values=<AZ>" "Name=defaultForAz,Values=true" \
  --query 'Subnets[0].SubnetId' --output text --profile <YOUR_AWS_PROFILE>
```

---

## 2. Launch the Instance

```bash
aws ec2 run-instances \
  --region <REGION> \
  --image-id <AMI_ID> \
  --instance-type <INSTANCE_TYPE> \
  --key-name <YOUR_KEY_PAIR> \
  --security-group-ids <YOUR_SECURITY_GROUP_ID> \
  --subnet-id <YOUR_SUBNET_ID> \
  --instance-market-options '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"persistent","InstanceInterruptionBehavior":"stop"}}' \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":300,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cooperbench-gpu-spot}]' \
  --profile <YOUR_AWS_PROFILE>
```

**If you get `InsufficientInstanceCapacity`**: spot capacity is frequently exhausted for p4d/p5 instances. Options:

1. **Retry loop** — leave running until capacity opens (often within a few hours, best overnight):
```bash
while true; do
  echo "$(date): trying..."
  RESULT=$(aws ec2 run-instances \
    --region <REGION> \
    --image-id <AMI_ID> \
    --instance-type <INSTANCE_TYPE> \
    --key-name <YOUR_KEY_PAIR> \
    --security-group-ids <YOUR_SECURITY_GROUP_ID> \
    --subnet-id <YOUR_SUBNET_ID> \
    --instance-market-options '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"persistent","InstanceInterruptionBehavior":"stop"}}' \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":300,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cooperbench-gpu-spot}]' \
    --profile <YOUR_AWS_PROFILE> 2>&1)
  if echo "$RESULT" | grep -q "InstanceId"; then
    echo "SUCCESS:"
    echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print('InstanceId:', d['Instances'][0]['InstanceId'])"
    break
  else
    echo "No capacity. Retrying in 2 min..."
    sleep 120
  fi
done
```

2. **Try a different AZ or region** — re-run the spot price check from Step 1 and pick the next cheapest option.

**Spot type notes**:
- `persistent` + `stop` — if AWS reclaims the instance it stops (not terminates), preserving the EBS volume. Restart with `aws ec2 start-instances`. If you manually stop the instance, the spot request goes to `disabled` state and will NOT auto-restart — this is safe for pausing work.
- Storage: 300 GB gp3 — covers model (~54 GB), Docker images, and logs.
- When done, cancel the spot request before terminating (see Cleanup).

Note the `InstanceId` from the output. Wait until running:

```bash
aws ec2 wait instance-running \
  --region <REGION> \
  --instance-ids i-XXXXXXXX --profile <YOUR_AWS_PROFILE>
```

Get the public IP:

```bash
aws ec2 describe-instances \
  --region <REGION> \
  --instance-ids i-XXXXXXXX \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text --profile <YOUR_AWS_PROFILE>
```

**Note**: the public IP changes each time the instance starts. Retrieve it fresh after every restart.

---

## 3. SSH In

```bash
ssh -i ~/.ssh/<YOUR_KEY_PAIR>.pem ubuntu@<PUBLIC_IP>
```

All subsequent commands run on the instance. Start a tmux session immediately so work survives SSH disconnects:

```bash
tmux new -s main
```

---

## 4. Fix the NVIDIA Driver and Fabric Manager

The DLAMI ships with driver 550 (CUDA 12.4). The p5.48xlarge uses NVLink which requires the NVIDIA Fabric Manager to match the driver exactly. This section upgrades both to driver 570 (CUDA 12.8) — the highest version for which the Fabric Manager package is available.

### Add the NVIDIA CUDA repo and graphics PPA

```bash
# CUDA repo (for fabricmanager packages)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb

# Graphics drivers PPA (for newer driver versions)
sudo add-apt-repository ppa:graphics-drivers/ppa
sudo apt-get update
```

### Remove the old fabric manager (it is held and blocks the upgrade)

```bash
sudo apt-get purge -y --allow-change-held-packages nvidia-fabricmanager-550
```

### Install driver 570 and the matching fabric manager

The driver and fabric manager versions must be identical down to the patch number. Check what version of driver 570 apt will install:

```bash
apt-cache policy nvidia-driver-570 | grep Candidate
```

Note the version string (e.g. `570.211.01`). Then install both packages pinning the fabricmanager to the same version:

```bash
sudo apt-get install -y nvidia-driver-570 "nvidia-fabricmanager-570=570.211.01-1"
sudo systemctl enable nvidia-fabricmanager
sudo reboot
```

**Note on DKMS**: The install process compiles the GPU kernel module from source. The step `Preparing to unpack .../nvidia-dkms-570_...deb` can take 5–15 minutes with no visible progress output. This is normal — do not interrupt it.

### Verify after reboot

```bash
nvidia-smi | head -3
# Expected: Driver Version: 570.211.01   CUDA Version: 12.8

sudo systemctl status nvidia-fabricmanager
# Expected: Active: active (running)
# Log line: Successfully configured all the available NVSwitches to route GPU NVLink traffic
```

---

## 5. Install Dependencies

```bash
# uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Clone repos
git clone --branch sagemaker https://github.com/cooperbench/cooperagents.git ~/cooperagents
git clone https://github.com/cooperbench/CooperBench.git ~/CooperBench

# Install cooperagents with mini-swe deps
cd ~/cooperagents
uv pip install --system -e ".[mini]"
```

---

## 6. Download the Model

```bash
uv pip install --system "huggingface_hub[cli]"

# Log in if the repo requires authentication
hf auth login

hf download Qwen/Qwen3.8-27B \
  --local-dir ~/models/Qwen3.8-27B
```

This takes 30–60 minutes. Detach from tmux (`Ctrl+B, D`) and reattach later (`tmux attach -t main`).

---

## 7. Create the Environment File

```bash
cat > ~/cooperagents/.env.qwen38aws << 'EOF'
OPENAI_API_KEY=dummy
OPENAI_BASE_URL=http://localhost:8000/v1
AZURE_OPENAI_DEPLOYMENT=openai/Qwen3.8-27B
AZURE_OPENAI_BASE_URL=
AZURE_OPENAI_API_KEY=
MSWEA_COST_TRACKING=ignore_errors
COOPER_TEMPERATURE_FORCE=1.0
COOPER_TOP_P=0.95
COOPER_MAX_TOKENS=32768
COOPER_HARD_TIMEOUT_S=1200
COOPER_LLM_TIMEOUT_S=1100
COOPER_HEARTBEAT_DIR=/tmp/cooper_hb
COOPER_CONN_CLOSE=1
MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=25
COOPERBENCH_DIR=/home/ubuntu/CooperBench
EOF
```

**Key env vars explained**

| Variable | Value | Reason |
|---|---|---|
| `AZURE_OPENAI_DEPLOYMENT` | `openai/Qwen3.8-27B` | litellm prefix routes to the local vLLM endpoint |
| `AZURE_OPENAI_BASE_URL` | _(empty)_ | Empty prevents a stale Azure key from overriding `OPENAI_API_KEY` |
| `COOPER_MAX_TOKENS` | `32768` | Fixed for research paper — do not change |
| `COOPER_HARD_TIMEOUT_S` | `1200` | 32K tokens at ~100 tok/s ≈ 320s; 1200s covers vLLM load spikes |
| `MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT` | `25` | Default 10 (~7 min backoff) can expire during transient server load |

---

## 8. Start vLLM

vLLM is served via Docker. The Docker image bundles the right CUDA runtime and transformers version to avoid conflicts with the host's conda environment.

### 8a. Patch the model config for 256K context (YaRN RoPE scaling)

The base model has a 65,536-token context window, which is insufficient for many tasks (prompt + 32K output tokens exceeds 65K). Patch the model config to enable YaRN RoPE scaling to 262,144 tokens — matching the Tinker 256K PEFT setup used in Factory-23:

```bash
cd ~/models/Qwen3.8-27B
python3 -c "
import json
cfg = json.load(open('config.json'))
cfg['max_position_embeddings'] = 65536
cfg['rope_scaling'] = {'type': 'yarn', 'factor': 4.0, 'original_max_position_embeddings': 65536}
json.dump(cfg, open('config.json', 'w'), indent=2)
print('Done')
"
```

**Note**: YaRN is an approximation of the fine-tuned 256K PEFT variant. Quality at very long contexts may differ slightly from the Tinker setup. Note this in the paper.

### 8b. Start the server

Open a new tmux window:

```bash
tmux new-window -n vllm
```

Stop any previously running vLLM container first:

```bash
docker stop $(docker ps -q --filter "publish=8000") 2>/dev/null || true
```

Run the server. The `--entrypoint bash` wrapper upgrades transformers to support the `qwen3_5` architecture before starting (required because vllm:v0.19.0 ships with transformers 4.x which does not recognize this model type):

```bash
docker run --gpus all \
  -v ~/models:/models \
  -p 8000:8000 \
  --ipc=host \
  -e CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
  --entrypoint bash \
  vllm/vllm-openai:v0.19.0 \
  -c "pip install 'transformers>=5.0' --force-reinstall -q && \
      python3 -m vllm.entrypoints.openai.api_server \
        --model /models/Qwen3.8-27B \
        --served-model-name Qwen3.8-27B \
        --tensor-parallel-size 8 \
        --enable-auto-tool-choice \
        --tool-call-parser qwen3_xml \
        --max-model-len 262144 \
        --port 8000"
```

Wait for `Application startup complete.` (3–5 minutes including the transformers install), then verify:

```bash
curl http://localhost:8000/v1/models
```

**Why this image and these flags**

| Item | Reason |
|---|---|
| `vllm/vllm-openai:v0.19.0` | Last version using CUDA 12.9 (compatible with driver 570); v0.20.0+ requires CUDA 13.0 |
| `transformers>=5.0` force-installed | The `qwen3_5` model architecture is only recognized in transformers 5.x; v0.19.0 ships with 4.57.6 |
| `--tensor-parallel-size 8` | Splits model across all 8 GPUs |
| `--enable-auto-tool-choice` | Required for function-calling (tool use) |
| `--tool-call-parser qwen3_xml` | Qwen3 emits `<tool_call>` XML; this converts it to OpenAI tool_calls format |
| `--max-model-len 262144` | YaRN-extended context matching the Factory-23 256K setup |

### 8c. Set up automatic disk cleanup

Each Docker container run leaves behind a writable layer (~1–2 GB including the `pip install transformers` step). Over a 652-task run these accumulate and will fill the 300 GB volume. Set up a cron job to prune stopped containers and dangling layers every hour:

```bash
crontab -e
```

Add this line (then save and exit):

```
0 * * * * docker system prune -f >> /tmp/docker_prune.log 2>&1
```

Verify:

```bash
crontab -l
```

`docker system prune -f` (without `-a`) only removes stopped containers, dangling images, and unused build cache — it does **not** touch the running vLLM container or its image. Safe to run while the server is up.

If the disk fills anyway, run manually:

```bash
docker system prune -f
df -h /
```

---

## 9. Run the Benchmark

Open another tmux window:

```bash
tmux new-window -n bench
conda activate pytorch
cd ~/cooperagents
```

Run solo evaluation on all 652 CooperBench pairs:

```bash
ENV_FILE=.env.qwen38aws uv run python scripts/bench_compare.py \
  --solo-only \
  --subset all \
  --step-limit 1000 \
  --concurrency 8 \
  --resume \
  --solo-name qwen3-27b-solo
```

Detach (`Ctrl+B, D`). Check progress at any time:

```bash
# Count completed pairs (target: 652)
find ~/cooperagents/logs/qwen3-27b-solo -name "result.json" | wc -l
```

**Flags explained**

| Flag | Value | Reason |
|---|---|---|
| `--solo-only` | — | Runs only the solo arm (single agent implements both features) |
| `--subset all` | all | All 652 CooperBench pairs across 30 tasks and 12 repos |
| `--step-limit` | 1000 | Matches the Factory-23 reproduction protocol |
| `--concurrency` | 8 | One parallel worker per GPU stream |
| `--resume` | — | Skips pairs that already have a `result.json`; safe to restart after interruption |
| `--solo-name` | qwen3-27b-solo | Output directory under `logs/` |

**Expected runtime**: ~20 hours on H100. Estimated cost at ~$22/hr spot: ~$440.

If the instance is interrupted by AWS, it stops (not terminates) due to the persistent spot configuration. Restart:
```bash
aws ec2 start-instances --region <REGION> --instance-ids i-XXXXXXXX --profile <YOUR_AWS_PROFILE>
```
Then SSH back in, retrieve the new IP, restart the Fabric Manager check, relaunch the vLLM Docker container, reattach to the bench tmux window, and re-run the same command — `--resume` ensures no work is repeated.

---

## 10. Copy Results Back

Run from your local machine once the benchmark is complete:

```bash
rsync -avz -e "ssh -i ~/.ssh/<YOUR_KEY_PAIR>.pem" \
  ubuntu@<PUBLIC_IP>:~/cooperagents/logs/qwen3-27b-solo \
  ~/Desktop/cooperator/cooperagents/logs/
```

---

## 11. Cleanup

**Important**: with a persistent spot request, you must cancel the request before terminating the instance, otherwise AWS will relaunch it.

```bash
# Step 1: find and cancel the spot request
aws ec2 describe-spot-instance-requests \
  --region <REGION> \
  --filters "Name=state,Values=active,open" \
  --query 'SpotInstanceRequests[*].{ID:SpotInstanceRequestId,Instance:InstanceId}' \
  --output table --profile <YOUR_AWS_PROFILE>

aws ec2 cancel-spot-instance-requests \
  --region <REGION> \
  --spot-instance-request-ids sir-XXXXXXXX \
  --profile <YOUR_AWS_PROFILE>

# Step 2: terminate the instance
aws ec2 terminate-instances \
  --region <REGION> \
  --instance-ids i-XXXXXXXX --profile <YOUR_AWS_PROFILE>
```

---

## 12. Reference: tmux Cheatsheet

| Command | Action |
|---|---|
| `tmux new -s <name>` | New named session |
| `tmux new-window -n <name>` | New window in current session |
| `Ctrl+B, D` | Detach from session |
| `tmux attach -t <name>` | Reattach to session |
| `Ctrl+B, N` | Next window |
| `Ctrl+B, P` | Previous window |
| `Ctrl+B, [` | Enter scroll mode |
| `q` | Exit scroll mode |
| `Ctrl+B, 0/1/2` | Jump to window by number |
