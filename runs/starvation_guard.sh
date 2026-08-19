#!/bin/bash
# Starvation auto-guard: polls the heartbeat-based live checker on every
# fleet node; any harness whose agents have NO completed LLM call past the
# grace window (STARVING) is killed on the spot and its task re-queued to
# runs/q27_r4_queue.txt, which the rolling controller redispatches under
# the 3-cell concurrency cap. This turns starvation from a lost 2h cell
# into a bounded ~12min detect-kill-requeue cycle.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
SSH="ssh -o ConnectTimeout=8 -i $HOME/.ssh/fleet_key"
while true; do
  for ip in $(cat $F/nodes.txt); do
    out=$(timeout 25 $SSH ubuntu@$ip \
      'cd CooperAgents && .venv/bin/python scripts/fleet/live_starvation_check.py --grace 720 --stale 99999 2>/dev/null' \
      </dev/null 2>/dev/null | grep "^STARVING")
    [ -z "$out" ] && continue
    echo "$out" | while read -r _ repc rest; do
      rep=${repc%:}
      pid=$(timeout 15 $SSH ubuntu@$ip "pgrep -f 'rep $rep '" </dev/null 2>/dev/null | head -1)
      [ -z "$pid" ] && continue
      inst=$(timeout 15 $SSH ubuntu@$ip "tr '\0' ' ' < /proc/$pid/cmdline" </dev/null 2>/dev/null \
             | grep -oE "instance [^ ]+" | cut -d" " -f2)
      timeout 15 $SSH ubuntu@$ip "kill $pid" </dev/null 2>/dev/null
      base=$(echo "$rep" | sed -E "s/-q27t3(r[0-9]+)?$//")
      if echo "$rep" | grep -q "q27t3"; then
        echo "$base $inst" >> runs/q27_r4_queue.txt
        echo "$(date -u +%H:%M) GUARD: killed starving $rep on $ip; requeued $base"
      else
        echo "$(date -u +%H:%M) GUARD: killed starving $rep on $ip (non-q27; manual requeue)"
      fi
    done
  done
  sleep 300
done
