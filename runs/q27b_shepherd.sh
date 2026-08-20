#!/bin/bash
# q27b batch shepherd: drain runs/q27b_queue.txt (lines: "<rep> <inst> <size>")
# across the fleet against the held B200 endpoint. One cell per node.
# On each landing: collect, validate (heartbeat evidence), log PASS/FAIL+score;
# INVALID cells are re-queued automatically (max 2 retries via rep suffix).
# Ends when the queue is empty and no q27b runner remains. NO endpoint stop —
# the B200 is held by the user.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
SSH="ssh -o ConnectTimeout=8 -i $HOME/.ssh/fleet_key"
export COOPER_ENV_FILE=.env.qwen38b200
BASEFLAGS="--step-limit 1000 --repair --agent-time-limit 14400 --completion-gate --env-brief --presub-merge"
log() { echo "$(date -u +%H:%M) $*"; }

validate_new() {
  $F/collect.sh </dev/null >/dev/null 2>&1
  for d in runs/pb-coopgitc2-*-q27bt* runs/pb-solo-*-q27bsolo*; do
    [ -d "$d" ] && [ -f "$d.DONE" ] && [ ! -f "$d/.val" ] || continue
    name=$(basename "$d" | sed -E "s/^pb-(coopgitc2|solo)-//")
    score=$(grep -E "Average" "$d/eval.log" 2>/dev/null | head -1 | grep -oE "[0-9]+" | head -1)
    if .venv/bin/python $F/validate_run.py "$d" > "$d/.valout" 2>&1; then
      log "CELL VALID $name score=${score:-none}"
    else
      log "CELL INVALID $name score=${score:-none} $(tail -2 "$d/.valout" | head -1)"
      base=$(echo "$name" | sed -E 's/r[0-9]+$//')
      tag=$(echo "$base" | sed -E "s/-q27b.*$//"); size=$(echo "$base" | grep -oE "t[0-9]$" | tr -d "t"); size=${size:-1}
      retries=$(echo "$name" | grep -oE "r[0-9]+$" | tr -d r); retries=${retries:-0}
      inst=$(awk -v t="$tag-" 'index($1, t)==1 {print $2; exit}' runs/q27b_queue_all.txt)
      if [ "$retries" -lt 2 ] && [ -n "$inst" ]; then
        a="coopgitc2"; case "$name" in *q27bsolo*) a="solo";; esac
        echo "${base}r$((retries+1)) $inst $size $a" >> runs/q27b_queue.txt
        log "REQUEUED ${base}r$((retries+1))"
      else
        log "GIVING UP on $name (retries=$retries)"
      fi
    fi
    touch "$d/.val"
  done
}

touch runs/q27b_queue_all.txt
cat runs/q27b_queue.txt runs/q27b_queue_all.txt | sort -u > runs/.qall.tmp && mv runs/.qall.tmp runs/q27b_queue_all.txt
while :; do
  validate_new
  if [ -s runs/q27b_queue.txt ]; then
    freenode=""
    for ip in $(cat $F/nodes.txt); do
      n=$($SSH ubuntu@$ip 'pgrep -cf "bench_programbench.p[y]"' </dev/null 2>/dev/null)
      [ "$n" = "0" ] && { freenode=$ip; break; }
    done
    if [ -n "$freenode" ]; then
      line=$(head -1 runs/q27b_queue.txt)
      rep=$(echo "$line" | cut -d' ' -f1)
      inst=$(echo "$line" | cut -d' ' -f2)
      size=$(echo "$line" | cut -d' ' -f3)
      arm=$(echo "$line" | cut -d' ' -f4); arm=${arm:-coopgitc2}
      if [ "$arm" = "solo" ]; then
        FL="--step-limit 1000 --repair --agent-time-limit 14400"
      else
        FL="$BASEFLAGS --team-size $size"
      fi
      if $F/pbrun.sh "$freenode" "$inst" "$arm" "$rep" $FL >/dev/null 2>&1; then
        sed -i 1d runs/q27b_queue.txt
        log "DISPATCHED $rep (t$size) -> $freenode [queue=$(wc -l < runs/q27b_queue.txt)]"
      fi
    fi
  else
    live=0
    for ip in $(cat $F/nodes.txt); do
      n=$($SSH ubuntu@$ip 'pgrep -cf "q27b[ts]"' </dev/null 2>/dev/null)
      live=$((live + ${n:-0}))
    done
    if [ "$live" -eq 0 ]; then
      validate_new
      log "Q27B BATCH COMPLETE"
      break
    fi
  fi
  sleep 120
done
