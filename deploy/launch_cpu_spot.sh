#!/bin/zsh
# Launch a self-terminating benchmark job on an x86_64 spot instance, co-located
# with the SAP AI Core endpoint (eu-central-1). Fork of launch_p4d_spot.sh:
# CPU instance (no GPU), dynamic Ubuntu 24.04 AMI, user-data bootstrap, IAM role
# for S3/SSM/self-terminate. The box runs the job (resume-safe), syncs results to
# S3, and terminates itself on completion. Fill in the CONFIG block.
#
# Prereqs (see deploy/README.md): an S3 bucket; SSM SecureString params holding
# aicore.json and .env; a repo tarball + data/ in S3; an IAM instance profile
# with s3:*Object/ListBucket, ssm:GetParameter, ec2:TerminateInstances,
# ec2:CancelSpotInstanceRequests, ec2:DescribeInstances; a key pair; a security
# group allowing outbound 443 (and SSH in if you want to inspect).
set -u

# ---- CONFIG (fill these in) -------------------------------------------------
INSTANCE_TYPE="m6id.4xlarge"          # 16 vCPU / 64 GiB / local NVMe; use *.metal if you need KVM for TB qemu tasks
KEY_NAME="cooperbench-key"
IAM_PROFILE="__IAM_INSTANCE_PROFILE__"  # instance profile NAME
S3_BUCKET="__S3_BUCKET__"
S3_PREFIX="cooperagents/remote1"
BENCH="all"                           # swebench | terminalbench | all
AICORE_HOST="__AICORE_HOST__"         # e.g. api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com
SSM_AICORE="/cooperagents/aicore_json"
SSM_ENV="/cooperagents/dotenv"
REPO_S3="s3://__S3_BUCKET__/cooperagents/repo.tar.gz"
DATA_S3="s3://__S3_BUCKET__/cooperagents/data/"
SWEB_INSTANCES=""                     # space-separated ids; empty = all 500
TB_TASKS=""                           # comma-separated TB tasks; empty = light subset
ROOT_GB=200                           # EBS root; Docker lives on the NVMe

# region  security-group       subnet            az            (eu-central first: co-located w/ endpoint)
TARGETS=(
  "eu-central-1 __SG_EUC1__ __SUBNET_EUC1A__ eu-central-1a"
  "eu-central-1 __SG_EUC1__ __SUBNET_EUC1B__ eu-central-1b"
  "eu-central-1 __SG_EUC1__ __SUBNET_EUC1C__ eu-central-1c"
)
# -----------------------------------------------------------------------------

UBUNTU_SSM="/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
MARKET='{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"persistent","InstanceInterruptionBehavior":"stop"}}'
TAGS='ResourceType=instance,Tags=[{Key=Name,Value=cooperagents-bench-job}]'

SRC="$(cd "$(dirname "$0")" && pwd)/user-data.sh"
UD="$(mktemp -t cooper-userdata).sh"
sed -e "s#__S3_BUCKET__#${S3_BUCKET}#g" \
    -e "s#__S3_PREFIX__#${S3_PREFIX}#g" \
    -e "s#__BENCH__#${BENCH}#g" \
    -e "s#__AICORE_HOST__#${AICORE_HOST}#g" \
    -e "s#__SSM_AICORE__#${SSM_AICORE}#g" \
    -e "s#__SSM_ENV__#${SSM_ENV}#g" \
    -e "s#__REPO_S3__#${REPO_S3}#g" \
    -e "s#__DATA_S3__#${DATA_S3}#g" \
    -e "s#__SWEB_INSTANCES__#${SWEB_INSTANCES}#g" \
    -e "s#__TB_TASKS__#${TB_TASKS}#g" \
    "$SRC" > "$UD"

VOLUME="[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":${ROOT_GB},\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]"

echo "Launching $INSTANCE_TYPE (job=$BENCH) across eu-central-1 AZs..."
for target in "${TARGETS[@]}"; do
  region=$(echo $target | awk '{print $1}')
  sg=$(echo $target | awk '{print $2}')
  subnet=$(echo $target | awk '{print $3}')
  az=$(echo $target | awk '{print $4}')

  ami=$(aws ec2 describe-images --region "$region" \
        --owners 099720109477 \
        --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
        --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)
  [[ "$ami" == "None" || -z "$ami" ]] && ami=$(aws ssm get-parameters --region "$region" --names "$UBUNTU_SSM" --query 'Parameters[0].Value' --output text 2>/dev/null)

  echo -n "  [$az ami=$ami] ... "
  result=$(aws ec2 run-instances \
    --region "$region" \
    --image-id "$ami" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --iam-instance-profile "Name=$IAM_PROFILE" \
    --security-group-ids "$sg" \
    --subnet-id "$subnet" \
    --instance-market-options "$MARKET" \
    --block-device-mappings "$VOLUME" \
    --user-data "file://$UD" \
    --tag-specifications "$TAGS" \
    --query 'Instances[0].{InstanceId:InstanceId,AZ:Placement.AvailabilityZone}' \
    --output table 2>&1) && rc=0 || rc=$?

  if [[ $rc -eq 0 ]]; then
    echo "SUCCESS"
    echo "$result"
    echo ""
    echo "Logs:    aws ssm start-session / ssh then: tail -f /var/log/cooperagents-boot.log /var/log/cooperagents-job.log"
    echo "Results: aws s3 ls s3://$S3_BUCKET/$S3_PREFIX/runs/   (DONE.txt appears on completion)"
    rm -f "$UD"
    exit 0
  else
    echo "$(echo "$result" | grep -oE 'InsufficientInstanceCapacity|InvalidKeyPair|ValidationException|AuthFailure|UnauthorizedOperation' | head -1 || echo FAILED)"
  fi
done
rm -f "$UD"
echo "All eu-central-1 AZs exhausted."
exit 1
