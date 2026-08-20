#!/bin/bash
# TB3 batch shepherd: first 10 single-container Terminal-Bench tasks x
# solo/t2/t3/t4 with the 27B on the held B200. GATED: waits until the q27b
# shepherd logs "Q27B BATCH COMPLETE" so the two batches never race for
# nodes. Scoring = the task's own verifier via TerminalBenchAdapter.evaluate
# (run locally on collect); resource validation identical to other batches.
# Invalid cells are requeued once. No endpoint stop (B200 held by user).
cd /home/ubuntu/CooperAgents
F=scripts/fleet
SSH="ssh -o ConnectTimeout=8 -i $HOME/.ssh/fleet_key"
export COOPER_ENV_FILE=.env.qwen38b200tb
BASEFLAGS="--step-limit 1000 --agent-time-limit 14400 --repair"
TEAMFLAGS="--completion-gate --env-brief --presub-merge"
log() { echo "$(date -u +%H:%M) $*"; }

log "starting TB3 fin-config batch"

score_new() {
  $F/collect.sh </dev/null >/dev/null 2>&1
  for d in runs/pb-coopgitc2-*-tb3f* runs/pb-solo-*-tb3f*; do
    [ -d "$d" ] && [ -f "$d.DONE" ] && [ ! -f "$d/.val" ] || continue
    name=$(basename "$d" | sed -E "s/^pb-(coopgitc2|solo)-//")
    reward=$(COOPER_BENCHMARK=terminalbench .venv/bin/python -c "
import sys, json; sys.path.insert(0, 'src')
from cooperagents.adapters.terminalbench import TerminalBenchAdapter
print(json.dumps(TerminalBenchAdapter().evaluate('$d')))" 2>/dev/null)
    if .venv/bin/python $F/validate_run.py "$d" > "$d/.valout" 2>&1; then
      log "CELL VALID $name reward=$reward"
    else
      log "CELL INVALID $name reward=$reward $(tail -2 "$d/.valout" | head -1)"
      base=$(echo "$name" | sed -E 's/r[0-9]+$//')
      if ! echo "$name" | grep -qE "r[0-9]+$"; then
        line=$(awk -v b="$base" '$1==b {print; exit}' runs/tb3_queue_all.txt)
        [ -n "$line" ] && { echo "${base}r1 $(echo "$line" | cut -d' ' -f2-)" >> runs/tb3_queue.txt; log "REQUEUED ${base}r1"; }
      else
        log "GIVING UP on $name"
      fi
    fi
    touch "$d/.val"
  done
}

cp -n runs/tb3_queue.txt runs/tb3_queue_all.txt 2>/dev/null || true
while :; do
  score_new
  if [ -s runs/tb3_queue.txt ]; then
    freenode=""
    for ip in $(cat $F/nodes.txt); do
      n=$($SSH ubuntu@$ip 'pgrep -cf "bench_programbench.p[y]"' </dev/null 2>/dev/null)
      [ "$n" = "0" ] || continue
      $SSH ubuntu@$ip 'test -d ~/terminalbench/terminal-bench' </dev/null 2>/dev/null || continue
      freenode=$ip; break
    done
    if [ -n "$freenode" ]; then
      line=$(head -1 runs/tb3_queue.txt)
      rep=$(echo "$line" | cut -d' ' -f1); inst=$(echo "$line" | cut -d' ' -f2)
      size=$(echo "$line" | cut -d' ' -f3); arm=$(echo "$line" | cut -d' ' -f4)
      if [ "$arm" = "solo" ]; then FL="$BASEFLAGS"; else FL="$BASEFLAGS $TEAMFLAGS --team-size $size"; fi
      if $F/pbrun.sh "$freenode" "$inst" "$arm" "$rep" $FL >/dev/null 2>&1; then
        sed -i 1d runs/tb3_queue.txt
        log "DISPATCHED $rep ($arm t$size) -> $freenode [queue=$(wc -l < runs/tb3_queue.txt)]"
      fi
    fi
  else
    live=0
    for ip in $(cat $F/nodes.txt); do
      n=$($SSH ubuntu@$ip 'pgrep -cf "tb3[f]"' </dev/null 2>/dev/null)
      live=$((live + ${n:-0}))
    done
    if [ "$live" -eq 0 ]; then
      score_new
      log "TB3 BATCH COMPLETE"
      break
    fi
  fi
  sleep 120
done
