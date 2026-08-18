#!/bin/bash
# REAL team-vs-solo comparison on healthy infrastructure.
# 10 mbench tasks x {coopgitc2 team-size 3, solo}, standard limits
# (1000 steps, 3600s/agent), full mechanism stack. Staggered <=3 concurrent.
# Every completed cell is validated by validate_run.py; a resource-FAIL cell
# is re-dispatched once (suffix b). Scores of failed cells do not count.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
NODES=($(cat $F/nodes.txt))
TEAM_FLAGS="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief --presub-merge --team-size 3"
SOLO_FLAGS="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief"

TASKS="abishekvashok__cmatrix.5c082c6 cmatrix
agourlay__zip-password-finder.704700d zipfinder
ajeetdsouza__zoxide.67ca1bc zoxide
alecthomas__chroma.8d04def chroma
alexpovel__srgn.89f943b srgn
altdesktop__i3-style.f93821b i3style
ammarabouzor__tui-journal.2b4540d tuijournal
anordal__shellharden.6a6ffd4 shellharden
antonmedv__fx.86d0d34 fx
antonmedv__walk.bf802ef walk"

# job list: team cell then solo cell per task (team first: heavier)
JOBS=""
while read -r inst tag; do
  JOBS+="$inst coopgitc2 ${tag}-cmp3 team\n$inst solo ${tag}-cmps solo\n"
done <<< "$TASKS"

marker_count() { ls runs/pb-*-cmp*.DONE 2>/dev/null | wc -l; }
BASE=$(marker_count)
i=0
printf "%b" "$JOBS" | while read -r inst arm rep kind; do
  [ -z "$inst" ] && continue
  while :; do
    outstanding=$(( i - ($(marker_count) - BASE) ))
    [ "$outstanding" -lt 3 ] && break
    $F/collect.sh </dev/null >/dev/null 2>&1
    sleep 180
  done
  flags=$([ "$kind" = team ] && echo "$TEAM_FLAGS" || echo "$SOLO_FLAGS")
  $F/pbrun.sh "${NODES[$((i % ${#NODES[@]}))]}" "$inst" "$arm" "$rep" $flags
  i=$((i+1))
done

# NOTE: the while-read subshell loses $i; recompute expected total
TOTAL=20
until [ "$(marker_count)" -ge "$((BASE + TOTAL))" ]; do
  $F/collect.sh </dev/null >/dev/null 2>&1
  sleep 300
done

echo "=== ALL CELLS COLLECTED — VALIDATING ==="
RETRY=""
n=0
for d in runs/pb-*-cmp3 runs/pb-*-cmps; do
  [ -d "$d" ] || continue
  if .venv/bin/python $F/validate_run.py "$d"; then :; else RETRY+="$d\n"; fi
  n=$((n+1))
done

if [ -n "$RETRY" ]; then
  echo "=== RE-DISPATCHING RESOURCE-FAILED CELLS ==="
  j=0
  printf "%b" "$RETRY" | while read -r d; do
    [ -z "$d" ] && continue
    name=$(basename "$d")            # pb-<arm>-<tag>-cmp3|cmps
    arm=$(echo "$name" | cut -d- -f2)
    rep=$(echo "$name" | cut -d- -f3-)
    inst=$(ls "$d" | head -1)
    flags=$([ "$arm" = coopgitc2 ] && echo "$TEAM_FLAGS" || echo "$SOLO_FLAGS")
    $F/pbrun.sh "${NODES[$((j % ${#NODES[@]}))]}" "$inst" "$arm" "${rep}b" $flags
    j=$((j+1))
    sleep 600   # crude stagger for retries
  done
fi

echo "=== REALCMP SWEEP DONE ==="
$F/collect.sh </dev/null 2>/dev/null | grep -E -- "-cmp" | sort
