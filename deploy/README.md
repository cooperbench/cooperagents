# deploy — run SWE-bench / Terminal-Bench as a self-terminating AWS job

Fire a benchmark sweep on an x86_64 spot instance co-located with the SAP AI Core
endpoint. The box bootstraps itself, runs the job (resume-safe across spot
interruptions), streams results to S3, and **terminates itself when done** — no
instances to babysit, nothing left running.

Why a real EC2 box (not Fargate/SageMaker/Lambda): SWE-bench and Terminal-Bench
build and run a Docker container **per task**, which those serverless options
cannot do (no privileged / Docker-in-Docker).

## Files

- `launch_cpu_spot.sh` — launches the instance (edit the CONFIG block first).
- `user-data.sh` — one-time bootstrap (Docker, Python 3.12, uv, repo, data, creds); installs the job as a systemd unit.
- `boot_run.sh` — per-boot job wrapper (reachability check, run, S3 sync, self-terminate on success). Runs on every boot, so a spot stop then start auto-resumes.
- `run_benchmarks.sh` — the actual benchmark commands (SWE-bench and/or TB) on x86_64.
- `build_grade_set.py` — builds the x86_64 SWE-bench grade JSONL from the Verified dataset.

## One-time setup

1. S3 bucket for inputs + results. Replace `BUCKET` below with yours.

2. Upload a repo tarball and the data directory. Run from the repo root:

```
git archive --format=tar.gz -o /tmp/cooperagents.tar.gz --prefix=cooperagents/ HEAD
aws s3 cp /tmp/cooperagents.tar.gz s3://BUCKET/cooperagents/repo.tar.gz
aws s3 sync data/ s3://BUCKET/cooperagents/data/ --exclude ".git/*"
```

The data sync includes `swebench_verified.jsonl` and `terminal-bench-core-0.1.1/`. The 3.3 GB `browsecomp_corpus.jsonl` is only needed for BrowseComp (a state benchmark), so you can exclude it for a SWE/TB job.

3. Store credentials as SSM SecureString parameters (decrypted on the box via its IAM role; never baked into the AMI or user-data):

```
aws ssm put-parameter --name /cooperagents/aicore_json --type SecureString --value file://aicore.json
aws ssm put-parameter --name /cooperagents/dotenv --type SecureString --value file://.env
```

4. An IAM instance profile whose role has the policy in the next section.

5. A key pair (`KEY_NAME`) and a security group allowing outbound 443 (add inbound 22 only if you want to SSH in to watch).

## IAM policy for the instance role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"], "Resource": ["arn:aws:s3:::BUCKET", "arn:aws:s3:::BUCKET/*"]},
    {"Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": "arn:aws:ssm:*:*:parameter/cooperagents/*"},
    {"Effect": "Allow", "Action": ["ec2:DescribeInstances", "ec2:TerminateInstances", "ec2:CancelSpotInstanceRequests"], "Resource": "*"}
  ]
}
```

## Launch

Edit the CONFIG block at the top of `launch_cpu_spot.sh` (bucket, IAM profile name, `AICORE_HOST`, the SSM param names, and your eu-central-1 security-group + subnet ids), then:

```
./deploy/launch_cpu_spot.sh
```

It tries the eu-central-1 AZs in order and prints the instance id on success. `BENCH` selects the workload: `swebench`, `terminalbench`, or `all`. Leave `SWEB_INSTANCES` empty to grade all 500; set it to a space-separated id list for a subset.

## Monitor and collect

```
aws s3 ls s3://BUCKET/cooperagents/remote1/runs/
aws s3 cp s3://BUCKET/cooperagents/remote1/DONE.txt -
```

`DONE.txt` appears when the job finishes and the box has self-terminated. To watch live, SSH in and `tail -f /var/log/cooperagents-boot.log /var/log/cooperagents-job.log`.

## Lifecycle

- **Interruption**: persistent spot with `stop` behavior. A reclaim stops the box; capacity returning starts it; the systemd unit re-runs the job, which resumes from the S3-synced state (the runners skip finished work).
- **Completion**: on a clean run the wrapper writes `DONE.txt`, disables the unit, cancels the spot request, and terminates the instance.
- **Failure**: a non-zero run leaves the box up for inspection (and it would resume on the next boot).

## Caveats

- **Throughput is model-endpoint-bound.** A bigger box speeds Docker build/grade, but the agent loop's pace is set by SAP AI Core latency / rate limits. Putting the box in eu-central-1 (same region as the endpoint) is the main latency win; more vCPUs will not make the agent loop faster.
- **QEMU/kernel TB tasks need KVM**, which only EC2 `.metal` instances expose. On `m6id.4xlarge` those tasks run under slow emulation or should be skipped; switch `INSTANCE_TYPE` to a `.metal` if you need them.
- **TB full-77 still needs a code fix** for the 5 nested-layout tasks (the adapter cannot build a Dockerfile under `client/`); hardware does not solve that.
- **SWE-bench full 500** needs ~0.5–1 TB of image pulls; the `m6id` NVMe (`/mnt/nvme/docker`) holds the working set, but raise `ROOT_GB` or prune between instances if you cache aggressively.
