#!/bin/bash
# q27 sweep finisher (post-restart, rerun-aware).
# Completion condition: >=10 base-cell DONE markers AND no bench_programbench
# process on any fleet node for 3 consecutive checks (5 min apart) — this
# covers any number of starvation reruns without a hardcoded count.
# Then: collect, validate everything, STOP the GKE endpoint (mandatory,
# ~$6.64/hr/node), and verify the stop took.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
SSH="ssh -o ConnectTimeout=8 -i $HOME/.ssh/fleet_key"
idle_checks=0
while true; do
  $F/collect.sh </dev/null >/dev/null 2>&1
  base=$(ls runs/pb-*-q27t3.DONE 2>/dev/null | wc -l)
  running=0
  for ip in $(cat $F/nodes.txt); do
    n=$(timeout 15 $SSH ubuntu@$ip 'pgrep -cf "q27t[3]" || true' </dev/null 2>/dev/null)
    running=$((running + ${n:-0}))
  done
  echo "$(date -u +%H:%M) base_done=$base fleet_runners=$running idle_checks=$idle_checks"
  if [ "$base" -ge 10 ] && [ "$running" -eq 0 ]; then
    idle_checks=$((idle_checks + 1))
  else
    idle_checks=0
  fi
  [ "$idle_checks" -ge 3 ] && break
  sleep 300
done
pkill -f "autoscaler.s[h]" 2>/dev/null
echo "=== Q27 SWEEP DONE ==="
$F/collect.sh </dev/null >/dev/null 2>&1
ls runs/pb-*-q27t3*.DONE 2>/dev/null
for d in runs/pb-*-q27t3 runs/pb-*-q27t3r*; do
  [ -d "$d" ] && .venv/bin/python $F/validate_run.py "$d"
done
echo "=== STOPPING 27B ENDPOINT ==="
~/qwen-gke/stop.sh && echo "ENDPOINT STOPPED" || echo "WARNING: stop.sh FAILED — endpoint may still be billing"
sleep 600
code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://34.63.139.125:8000/health)
if [ "$code" = "200" ]; then
  ~/qwen-gke/stop.sh
  echo "GUARD: re-stopped (was still 200)"
else
  echo "GUARD: endpoint confirmed down (health=$code)"
fi
