#!/bin/bash
# Fleet autoscaler: scale EC2 worker nodes with batch demand.
#
# Demand = queued cells (runs/*_queue.txt) + busy nodes; capacity = nodes in
# scripts/fleet/nodes.txt. Scale up when queued > free nodes; scale down
# ONLY instances tagged cooper-autoscale=1 (the base fleet is never touched)
# after they sit idle with empty queues for IDLE_MIN consecutive checks.
#
# Requires: authenticated aws cli; REF_IP (a base node) to clone config from.
# Usage:  fleet_autoscaler.sh [--dry-run]     (runs as a daemon, 120s loop)
set -u
cd /home/ubuntu/CooperAgents
F=scripts/fleet
SSH="ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -i $HOME/.ssh/fleet_key"
DRY=${1:-}
MAX_EXTRA=15          # hard cap on autoscaled nodes
IDLE_MIN=10           # consecutive idle checks (x120s) before terminating
REF_IP=$(head -1 $F/nodes.txt)
log() { echo "$(date -u +%H:%M) $*"; }

ref_json=""
ref() {  # lazily resolve the reference node's launch config
  [ -n "$ref_json" ] && { echo "$ref_json"; return; }
  ref_json=$(aws ec2 describe-instances --filters "Name=ip-address,Values=$REF_IP" \
    --query "Reservations[0].Instances[0].{ami:ImageId,type:InstanceType,sg:SecurityGroups[0].GroupId,subnet:SubnetId,key:KeyName}" \
    --output json 2>/dev/null)
  echo "$ref_json"
}

queued() { cat runs/*_queue.txt 2>/dev/null | grep -c . || true; }

node_busy() {  # 0 = busy/unreachable, 1 = idle
  n=$(timeout 15 $SSH ubuntu@$1 'pgrep -cf "bench_programbench.p[y]"' </dev/null 2>/dev/null)
  [ "$n" = "0" ] && return 1 || return 0
}

bootstrap() {  # rsync workload onto a fresh node
  ip=$1
  for i in $(seq 1 60); do timeout 15 $SSH ubuntu@$ip true </dev/null 2>/dev/null && break; sleep 10; done
  timeout 600 rsync -az -e "$SSH" --exclude runs --exclude .git ~/CooperAgents ubuntu@$ip: 2>/dev/null
  timeout 600 rsync -az -e "$SSH" ~/terminalbench ubuntu@$ip: 2>/dev/null
  timeout 300 rsync -az -e "$SSH" ~/ProgramBench ubuntu@$ip: 2>/dev/null || true
  $SSH ubuntu@$ip 'mkdir -p CooperAgents/runs; command -v docker >/dev/null || echo NODOCKER' </dev/null 2>/dev/null
}

declare -A idle_count
while :; do
  q=$(queued)
  free=0; busy=0
  extra_ids=$(aws ec2 describe-instances \
    --filters "Name=tag:cooper-autoscale,Values=1" "Name=instance-state-name,Values=running" \
    --query "Reservations[].Instances[].[InstanceId,PublicIpAddress]" --output text 2>/dev/null)
  n_extra=$(echo "$extra_ids" | grep -c . || true)
  for ip in $(cat $F/nodes.txt); do
    if node_busy "$ip"; then busy=$((busy+1)); else free=$((free+1)); fi
  done
  log "demand: queued=$q busy=$busy free=$free extra_nodes=$n_extra"

  # ---- scale up: queued work beyond what free nodes can absorb
  want=$(( q - free ))
  if [ "$want" -gt 0 ] && [ "$n_extra" -lt "$MAX_EXTRA" ]; then
    add=$(( want < (MAX_EXTRA - n_extra) ? want : (MAX_EXTRA - n_extra) ))
    [ "$add" -gt 3 ] && add=3   # ramp gently, 3 per cycle
    cfg=$(ref)
    ami=$(echo "$cfg" | python3 -c "import json,sys; print(json.load(sys.stdin)['ami'])" 2>/dev/null)
    if [ -n "$ami" ]; then
      if [ "$DRY" = "--dry-run" ]; then
        log "DRY: would launch $add nodes ($ami)"
      else
        type=$(echo "$cfg" | python3 -c "import json,sys; print(json.load(sys.stdin)['type'])")
        sg=$(echo "$cfg" | python3 -c "import json,sys; print(json.load(sys.stdin)['sg'])")
        subnet=$(echo "$cfg" | python3 -c "import json,sys; print(json.load(sys.stdin)['subnet'])")
        key=$(echo "$cfg" | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")
        out=$(aws ec2 run-instances --image-id "$ami" --instance-type "$type" \
          --security-group-ids "$sg" --subnet-id "$subnet" --key-name "$key" \
          --count "$add" --associate-public-ip-address \
          --tag-specifications 'ResourceType=instance,Tags=[{Key=cooper-autoscale,Value=1},{Key=Name,Value=cooper-fleet-auto}]' \
          --query "Instances[].InstanceId" --output text 2>&1)
        log "SCALE-UP launched: $out"
        sleep 45
        for id in $out; do
          nip=$(aws ec2 describe-instances --instance-ids "$id" \
            --query "Reservations[0].Instances[0].PublicIpAddress" --output text 2>/dev/null)
          [ -n "$nip" ] && [ "$nip" != "None" ] || continue
          bootstrap "$nip" && { echo "$nip" >> $F/nodes.txt; log "NODE JOINED $nip ($id)"; }
        done
      fi
    else
      log "SCALE-UP blocked: cannot resolve reference config (aws auth?)"
    fi
  fi

  # ---- scale down: idle tagged nodes when nothing is queued
  if [ "$q" -eq 0 ] && [ -n "$extra_ids" ]; then
    echo "$extra_ids" | while read -r id nip; do
      [ -n "${nip:-}" ] && [ "$nip" != "None" ] || continue
      grep -q "^$nip$" $F/nodes.txt || continue
      if node_busy "$nip"; then idle_count[$nip]=0; continue; fi
      c=$(( ${idle_count[$nip]:-0} + 1 )); idle_count[$nip]=$c
      if [ "$c" -ge "$IDLE_MIN" ]; then
        if [ "$DRY" = "--dry-run" ]; then
          log "DRY: would terminate $id ($nip)"
        else
          grep -v "^$nip$" $F/nodes.txt > $F/.nodes.tmp && mv $F/.nodes.tmp $F/nodes.txt
          aws ec2 terminate-instances --instance-ids "$id" >/dev/null 2>&1
          log "SCALE-DOWN terminated $id ($nip)"
        fi
      fi
    done
  fi
  sleep 120
done
