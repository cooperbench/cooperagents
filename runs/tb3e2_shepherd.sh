#!/bin/bash
# Easy-3 batch extension: solo + t4 arms (+ coq-t3 retry) on the 3 easiest TB3
# tasks. Queue-based: dispatch a cell when its node is free AND total
# concurrent LLM streams stay <= 12 (proven H100 4-pod regime).
# Scores+validates every landing; stops the H100 when ALL cells are done.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
export COOPER_ENV_FILE=.env.qwen38tb
BASE="--step-limit 1000 --agent-time-limit 14400 --repair"
TEAM="$BASE --completion-gate --env-brief --presub-merge"
log() { echo "$(date -u +%H:%M) $*"; }

# rep|instance|arm|streams|node_line|flags   (priority order)
CELLS=(
  "coq-block-bound-tb3et3r1|coq-block-bound|coopgitc2|3|3|$TEAM --team-size 3"
  "react-lead-form-tb3esolo|react-lead-form|solo|1|1|$BASE"
  "gpt2-codegolf-tb3esolo|gpt2-codegolf|solo|1|2|$BASE"
  "react-lead-form-tb3et4|react-lead-form|coopgitc2|4|4|$TEAM --team-size 4"
  "coq-block-bound-tb3et4|coq-block-bound|coopgitc2|4|6|$TEAM --team-size 4"
  "gpt2-codegolf-tb3esolor1|gpt2-codegolf|solo|1|2|$BASE"
  "gpt2-codegolf-tb3et4|gpt2-codegolf|coopgitc2|4|5|$TEAM --team-size 4"
  "coq-block-bound-tb3esolo|coq-block-bound|solo|1|3|$BASE"
  "coq-block-bound-tb3et4r1|coq-block-bound|coopgitc2|4|6|$TEAM --team-size 4"
  "gpt2-codegolf-tb3et3|gpt2-codegolf|coopgitc2|3|5|PRE"  # already running
)

runningp() {  # dispatched && not DONE
  local rep=$1 arm=$2
  [ -f "runs/.disp_$rep" ] && [ ! -f "runs/pb-$arm-$rep.DONE" ]
}

while :; do
  $F/collect.sh </dev/null >/dev/null 2>&1

  # score + validate any new landing (either arm)
  for d in runs/pb-coopgitc2-*-tb3e* runs/pb-solo-*-tb3e*; do
    [ -d "$d" ] && [ -f "$d.DONE" ] && [ ! -f "$d/.val" ] || continue
    name=$(basename "$d" | sed 's/^pb-[a-z0-9]*-//')
    reward=$(COOPER_BENCHMARK=terminalbench .venv/bin/python -c "
import sys, json; sys.path.insert(0, 'src')
from cooperagents.adapters.terminalbench import TerminalBenchAdapter
print(json.dumps(TerminalBenchAdapter().evaluate('$d')))" 2>/dev/null)
    if .venv/bin/python $F/validate_run.py "$d" > "$d/.valout" 2>&1; then
      log "CELL VALID $(basename $d) reward=$reward"
    else
      log "CELL INVALID $(basename $d) reward=$reward $(tail -2 "$d/.valout" | head -1)"
    fi
    touch "$d/.val"
  done

  # stream + node accounting over dispatched-not-done cells
  streams=0; declare -A busy=()
  for c in "${CELLS[@]}"; do
    IFS='|' read -r rep inst arm sz ln flags <<< "$c"
    if runningp "$rep" "$arm"; then
      streams=$((streams + sz)); busy[$ln]=1
    fi
  done

  # dispatch next cells that fit
  for c in "${CELLS[@]}"; do
    IFS='|' read -r rep inst arm sz ln flags <<< "$c"
    [ "$flags" = "PRE" ] && continue
    [ -f "runs/.disp_$rep" ] || [ -f "runs/pb-$arm-$rep.DONE" ] && continue
    [ "${busy[$ln]:-0}" = "1" ] && continue
    [ $((streams + sz)) -le 12 ] || continue
    ip=$(sed -n "${ln}p" $F/nodes.txt)
    if $F/pbrun.sh "$ip" "$inst" "$arm" "$rep" $flags >/dev/null 2>&1; then
      touch "runs/.disp_$rep"; busy[$ln]=1; streams=$((streams + sz))
      log "DISPATCHED $arm/$rep -> $ip (streams=$streams)"
    else
      log "WARNING dispatch failed $arm/$rep -> $ip"
    fi
  done

  # completion: every cell DONE
  alldone=1
  for c in "${CELLS[@]}"; do
    IFS='|' read -r rep inst arm sz ln flags <<< "$c"
    [ -f "runs/pb-$arm-$rep.DONE" ] || { alldone=0; break; }
  done
  if [ "$alldone" = "1" ]; then
    $F/collect.sh </dev/null >/dev/null 2>&1
    for d in runs/pb-coopgitc2-*-tb3e* runs/pb-solo-*-tb3e*; do
      [ -d "$d" ] && [ -f "$d.DONE" ] && [ ! -f "$d/.val" ] || continue
      log "final-validate $(basename $d)"; .venv/bin/python $F/validate_run.py "$d" > "$d/.valout" 2>&1; touch "$d/.val"
    done
    log "TB3E2 BATCH COMPLETE — stopping H100"
    ~/qwen-gke/stop.sh && log "H100 STOPPED" || log "WARNING: H100 stop FAILED — still billing"
    break
  fi
  sleep 120
done
