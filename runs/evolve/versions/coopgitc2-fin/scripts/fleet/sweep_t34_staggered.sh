#!/bin/bash
# Team-size 3/4 scalability sweep, STAGGERED: at most 3 concurrent runs so
# the shared endpoint (max_inputs=16/container) stays healthy. This launch
# covers the 7 cells still missing after (a) the saturated first sweep was
# killed and (b) the first staggered launch lost 5 jobs to a collect.sh
# stdin leak (now fixed) and 2 cells to the scale-out transient.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
NODES=($(cat $F/nodes.txt))
FLAGS="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief --presub-merge"

JOBS="abishekvashok__cmatrix.5c082c6 cmatrix 3 c
ammarabouzor__tui-journal.2b4540d tuijournal 3 c
altdesktop__i3-style.f93821b i3style 4 b
abishekvashok__cmatrix.5c082c6 cmatrix 4 b
ammarabouzor__tui-journal.2b4540d tuijournal 4 b
antonmedv__fx.86d0d34 fx 4 b
ajeetdsouza__zoxide.67ca1bc zoxide 4 b"

TOTAL=7
BASE=$(ls runs/pb-*-t[34]i7[bc].DONE 2>/dev/null | wc -l)  # markers that existed before this launch

i=0
while read -r inst tag size suf; do
  [ -z "$inst" ] && continue
  while :; do
    done_now=$(ls runs/pb-*-t[34]i7[bc].DONE 2>/dev/null | wc -l)
    outstanding=$((i - (done_now - BASE)))
    [ "$outstanding" -lt 3 ] && break
    $F/collect.sh </dev/null >/dev/null 2>&1
    sleep 180
  done
  $F/pbrun.sh "${NODES[$((i % ${#NODES[@]}))]}" "$inst" coopgitc2 "${tag}-t${size}i7${suf}" $FLAGS --team-size "$size"
  i=$((i+1))
done <<< "$JOBS"

echo "dispatched $i/$TOTAL jobs"
until [ "$(ls runs/pb-*-t[34]i7[bc].DONE 2>/dev/null | wc -l)" -ge "$((BASE + TOTAL))" ]; do
  $F/collect.sh </dev/null >/dev/null 2>&1
  sleep 300
done
echo "=== STAGGERED SWEEP DONE ==="
$F/collect.sh </dev/null 2>/dev/null | grep -E -- "-t[34]i7" | sort
