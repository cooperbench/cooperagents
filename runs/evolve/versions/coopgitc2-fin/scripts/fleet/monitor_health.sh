#!/bin/bash
# Live fleet-health monitor used during measurement sweeps (run under a
# Monitor/background watcher; each stdout line is an event). Two probes per poll:
#
#   1. ENDPOINT PROBE — one real (tiny) chat completion against the serving
#      endpoint; warns on non-200 or >=15s latency.
#   2. AGENT ACTIVITY PROBE — on every node with agent containers, sample
#      `docker exec` processes 4x over 8s. A served agent executes commands
#      every few seconds; an agent stuck inside a model call executes
#      nothing. 5 consecutive idle polls (~13min, beyond any legitimate
#      long generation) => STALL SUSPECT with node/container named.
#
# Heartbeat line every 5 polls; exits when the sweep log prints its DONE.
cd /home/ubuntu/CooperAgents
S="ssh -o ConnectTimeout=8 -i $HOME/.ssh/fleet_key"
source .env.qwen 2>/dev/null
declare -A idle
poll=0
while :; do
  poll=$((poll+1))
  # --- probe 1: endpoint ---------------------------------------------
  t0=$(date +%s)
  code=$(curl -sL -m 30 -o /dev/null -w "%{http_code}" "$OPENAI_BASE_URL/chat/completions" \
    -H "Content-Type: application/json" -H "Authorization: Bearer dummy" \
    -d '{"model":"Qwen/Qwen3.5-9B","messages":[{"role":"user","content":"ok"}],"max_tokens":3}' </dev/null)
  lat=$(( $(date +%s) - t0 ))
  [ "$code" != "200" ] && echo "WARN endpoint probe HTTP $code (poll$poll)"
  [ "$lat" -ge 15 ] && echo "WARN endpoint latency ${lat}s (poll$poll)"
  # --- probe 2: per-agent activity -----------------------------------
  summary=""; stalls=""
  for ip in $(cat scripts/fleet/nodes.txt); do
    out=$(timeout 26 $S ubuntu@$ip '
      cs=$(docker ps --format "{{.Names}}" | grep "^ca-")
      [ -z "$cs" ] && exit 0
      declare -A act
      for k in 1 2 3 4; do
        for c in $(ps aux | grep -oE "docker exec[^|]* ca-[a-f0-9]+" | grep -oE "ca-[a-f0-9]+" | sort -u); do act[$c]=1; done
        sleep 2
      done
      for c in $cs; do echo "$c:${act[$c]:-0}"; done' </dev/null 2>/dev/null)
    [ -z "$out" ] && continue
    na=0; nt=0
    for line in $out; do
      c=${line%%:*}; a=${line##*:}; nt=$((nt+1)); key="$ip/$c"
      if [ "$a" = "1" ]; then idle[$key]=0; na=$((na+1))
      else idle[$key]=$(( ${idle[$key]:-0} + 1 ))
        [ "${idle[$key]}" -eq 5 ] && stalls="$stalls $key"
      fi
    done
    summary="$summary $ip:[$na/$nt]"
  done
  [ -n "$stalls" ] && echo "STALL SUSPECT (idle 5 polls ~13min):$stalls (poll$poll)"
  if [ $((poll % 5)) -eq 1 ]; then
    done_n=$(ls runs/pb-*-cmp*.DONE 2>/dev/null | wc -l)
    echo "heartbeat poll$poll: cells done=$done_n/20 active$summary probe=${lat}s"
  fi
  grep -q "REALCMP SWEEP DONE" runs/realcmp.log 2>/dev/null && { echo "SWEEP COMPLETE — monitor exiting"; exit 0; }
  sleep 150
done
