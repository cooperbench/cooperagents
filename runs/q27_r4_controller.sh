#!/bin/bash
# Rolling r4 dispatcher: keep TOTAL live q27 cells <= 3 (the concurrency this
# endpoint sustains for heavy thinking streams). Pops runs/q27_r4_queue.txt
# (lines: "<tag> <instance>"; append lines to add work) and dispatches each
# as <tag>-q27t3r4 to a node with no runner. Exits when the queue is empty.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
SSH="ssh -o ConnectTimeout=8 -i $HOME/.ssh/fleet_key"
export COOPER_ENV_FILE=.env.qwen38
FLAGS="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief --presub-merge --team-size 3"
while [ -s runs/q27_r4_queue.txt ]; do
  live=0; freenode=""
  for ip in $(cat $F/nodes.txt); do
    n=$($SSH ubuntu@$ip 'pgrep -cf "bench_programbench.p[y].*q27t[3]"' </dev/null 2>/dev/null)
    n=${n:-1}
    live=$((live + n))
    [ "$n" = "0" ] && [ -z "$freenode" ] && \
      [ "$($SSH ubuntu@$ip 'pgrep -cf "bench_programbench.p[y]"' </dev/null 2>/dev/null)" = "0" ] && freenode=$ip
  done
  if [ "$live" -lt 3 ] && [ -n "$freenode" ]; then
    line=$(head -1 runs/q27_r4_queue.txt)
    tag=${line%% *}; inst=${line#* }
    $F/pbrun.sh "$freenode" "$inst" coopgitc2 "${tag}-q27t3r4" $FLAGS \
      && sed -i 1d runs/q27_r4_queue.txt
    echo "$(date -u +%H:%M) dispatched ${tag}-q27t3r4 to $freenode (live_was=$live)"
  fi
  sleep 180
done
echo "R4 QUEUE DRAINED"
