# CooperBench on AWS — Benchmarking Recipe

End-to-end instructions for running the CooperBench solo evaluation with Qwen3.8-27B on a p4d.24xlarge spot instance (8× A100 40 GB, us-east-1b, ~$6.50/hr Linux spot).

Covers: instance launch → model download → vLLM setup → benchmark run → result collection → cleanup.

---

## Prerequisites

- AWS CLI configured with a profile that has EC2 permissions (`<YOUR_AWS_PROFILE>` below — substitute your profile name).
- SSH key pair registered in AWS and the `.pem` file at `~/.ssh/<YOUR_KEY_PAIR>.pem` on your local machine.
- Security group in the default VPC that allows inbound SSH (22) and port 8000 from your IP. Note your security group ID (`<YOUR_SECURITY_GROUP_ID>`).
- Access to the `cooperagents` and `CooperBench` GitHub repos.
- A HuggingFace account with access to `Qwen/Qwen3.8-27B`.

---

## 1. Launch the Spot Instance

```bash
aws ec2 run-instances \
  --image-id ami-098d1191f6a25d157 \
  --instance-type p4d.24xlarge \
  --key-name <YOUR_KEY_PAIR> \
  --security-group-ids <YOUR_SECURITY_GROUP_ID> \
  --subnet-id <YOUR_SUBNET_ID_US_EAST_1B> \
  --instance-market-options '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"persistent","InstanceInterruptionBehavior":"stop"}}' \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":300,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cooperbench-a100-spot}]' \
  --profile <YOUR_AWS_PROFILE>
```

Note the `InstanceId` from the output. Wait until running:

```bash
aws ec2 wait instance-running --instance-ids i-XXXXXXXX --profile <YOUR_AWS_PROFILE>
```

Get the public IP:

```bash
aws ec2 describe-instances \
  --instance-ids i-XXXXXXXX \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text --profile <YOUR_AWS_PROFILE>
```

**Instance details**
- AMI: Deep Learning AMI GPU PyTorch 2.3.1 (Ubuntu 20.04) — CUDA drivers, Docker, and conda pre-installed.
- AZ: us-east-1b — cheapest AZ for p4d spot (~$6.50/hr Linux vs ~$21/hr on-demand).
- Spot type: `persistent` + `stop` — if AWS reclaims the instance it stops rather than terminates, preserving the EBS volume. Resume with `aws ec2 start-instances`.
- Storage: 300 GB gp3 — covers model (~54 GB), Docker images, logs.

---

## 2. SSH In

```bash
ssh -i ~/.ssh/<YOUR_KEY_PAIR>.pem ubuntu@<PUBLIC_IP>
```

All subsequent commands run on the instance. Start a tmux session immediately so work survives SSH disconnects:

```bash
tmux new -s main
```

---

## 3. Install Dependencies

```bash
# uv (fast Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Clone repos
git clone https://github.com/cooperbench/cooperagents.git ~/cooperagents
git clone https://github.com/cooperbench/CooperBench.git ~/CooperBench

# Install cooperagents with mini-swe deps
cd ~/cooperagents
uv pip install -e ".[mini]"
```

---

## 4. Download the Model

```bash
uv pip install "huggingface_hub[cli]"

# Log in if the repo requires authentication
huggingface-cli login

huggingface-cli download Qwen/Qwen3.8-27B \
  --local-dir ~/models/Qwen3.8-27B \
  --local-dir-use-symlinks False
```

This takes 30–60 minutes. Detach from tmux (`Ctrl+B, D`) and reattach later (`tmux attach -t main`).

---

## 5. Create the Environment File

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

## 6. Start vLLM

Open a new tmux window:

```bash
tmux new-window -n vllm
```

Start the server:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model ~/models/Qwen3.8-27B \
  --served-model-name Qwen3.8-27B \
  --tensor-parallel-size 8 \
  --enforce-eager \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --max-model-len 65536 \
  --port 8000
```

Wait for `Application startup complete.` (1–3 minutes), then verify:

```bash
curl http://localhost:8000/v1/models
```

**vLLM flags explained**

| Flag | Reason |
|---|---|
| `--tensor-parallel-size 8` | Required — model weights split across all 8 A100s |
| `--enforce-eager` | Disables CUDA graph capture; avoids OOM during warmup on p4d |
| `--enable-auto-tool-choice` | Required for function-calling (tool use) to work |
| `--tool-call-parser qwen3_xml` | Qwen3 emits `<tool_call>` XML; this parser converts it to OpenAI tool_calls format |
| `--served-model-name Qwen3.8-27B` | Without this the model ID becomes the full filesystem path |
| `--max-model-len 65536` | Base model context window |

---

## 7. Run the Benchmark

Open another tmux window:

```bash
tmux new-window -n bench
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
| `--concurrency` | 8 | One parallel worker per GPU stream; saturates the A100 server |
| `--resume` | — | Skips pairs that already have a `result.json`; safe to restart after interruption |
| `--solo-name` | qwen3-27b-solo | Output directory under `logs/` |

**Expected runtime**: ~35–50 hours. Estimated cost at $6.50/hr: ~$230–$325.

If the instance is interrupted by AWS, it will stop (not terminate) due to the persistent spot configuration. Restart with:
```bash
aws ec2 start-instances --instance-ids i-XXXXXXXX --profile <YOUR_AWS_PROFILE>
```
Then SSH back in, reattach to the bench tmux window, and re-run the same command — `--resume` ensures no work is repeated.

---

## 8. Copy Results Back

Run from your local machine once the benchmark is complete:

```bash
rsync -avz -e "ssh -i ~/.ssh/<YOUR_KEY_PAIR>.pem" \
  ubuntu@<PUBLIC_IP>:~/cooperagents/logs/qwen3-27b-solo \
  ~/Desktop/cooperator/cooperagents/logs/
```

---

## 9. Cleanup

**Important**: with a persistent spot request, you must cancel the request before terminating the instance, otherwise AWS will relaunch it.

```bash
# Step 1: find and cancel the spot request
aws ec2 describe-spot-instance-requests \
  --filters "Name=state,Values=active,open" \
  --query 'SpotInstanceRequests[*].{ID:SpotInstanceRequestId,Instance:InstanceId}' \
  --output table --profile <YOUR_AWS_PROFILE>

aws ec2 cancel-spot-instance-requests \
  --spot-instance-request-ids sir-XXXXXXXX \
  --profile <YOUR_AWS_PROFILE>

# Step 2: terminate the instance
aws ec2 terminate-instances --instance-ids i-XXXXXXXX --profile <YOUR_AWS_PROFILE>
```

---

## Reference: tmux Cheatsheet

| Command | Action |
|---|---|
| `tmux new -s <name>` | New named session |
| `tmux new-window -n <name>` | New window in current session |
| `Ctrl+B, D` | Detach from session |
| `tmux attach -t <name>` | Reattach to session |
| `Ctrl+B, N` | Next window |
| `Ctrl+B, P` | Previous window |
