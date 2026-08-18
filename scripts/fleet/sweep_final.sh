#!/bin/bash
# DEFINITIVE team-vs-solo measurement on the fully hardened stack:
# clean summaries (think-strip), overflow-proof compaction, segments
# preserved, 180s client timeouts, min_containers=2, staggered dispatch.
# 10 tasks x {coopgitc2 t3 full stack, solo}. Rep suffix: fin3 / fins.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
NODES=($(cat $F/nodes.txt))
TEAM="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief --presub-merge --team-size 3"
SOLO="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief"
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
JOBS=""
while read -r inst tag; do
  JOBS+="$inst coopgitc2 ${tag}-fin3 team\n$inst solo ${tag}-fins solo\n"
done <<< "$TASKS"
count() { ls runs/pb-*-fin*.DONE 2>/dev/null | wc -l; }
BASE=$(count); i=0
printf "%b" "$JOBS" | while read -r inst arm rep kind; do
  [ -z "$inst" ] && continue
  while :; do
    out=$(( i - ($(count) - BASE) ))
    [ "$out" -lt 5 ] && break
    $F/collect.sh </dev/null >/dev/null 2>&1
    sleep 120
  done
  flags=$([ "$kind" = team ] && echo "$TEAM" || echo "$SOLO")
  $F/pbrun.sh "${NODES[$((i % ${#NODES[@]}))]}" "$inst" "$arm" "$rep" $flags
  i=$((i+1))
done
until [ "$(count)" -ge "$((BASE + 20))" ]; do
  $F/collect.sh </dev/null >/dev/null 2>&1
  sleep 300
done
echo "=== FINAL SWEEP DONE ==="
$F/collect.sh </dev/null 2>/dev/null | grep -E -- "-fin" | sort
for d in runs/pb-*-fin3 runs/pb-*-fins; do
  [ -d "$d" ] && .venv/bin/python $F/validate_run.py "$d"
done
