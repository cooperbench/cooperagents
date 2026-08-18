#!/bin/bash
# Accelerated completion of the team-vs-solo comparison: concurrency 5,
# immediate dispatch of everything not already clean or in flight.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
NODES=($(cat $F/nodes.txt))
TEAM="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief --presub-merge --team-size 3"
SOLO="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief"

# task instance | tag ; cells already clean (cmatrix solo) or in flight
# (chroma x2, srgn team) are skipped below.
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

SKIP="solo-cmatrix coopgitc2-chroma solo-chroma coopgitc2-srgn"
skip() { case " $SKIP " in *" $1 "*) return 0;; *) return 1;; esac; }

JOBS=""
while read -r inst tag; do
  skip "coopgitc2-$tag" || JOBS+="$inst coopgitc2 ${tag}-cmp3r2 team\n"
  skip "solo-$tag"      || JOBS+="$inst solo ${tag}-cmpsr2 solo\n"
done <<< "$TASKS"

count() { ls runs/pb-*-cmp*r2.DONE 2>/dev/null | wc -l; }
BASE=$(count); TOTAL=16; i=0
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

until [ "$(count)" -ge "$((BASE + TOTAL))" ]; do
  $F/collect.sh </dev/null >/dev/null 2>&1
  sleep 300
done
echo "=== REALCMP2 DONE ==="
$F/collect.sh </dev/null 2>/dev/null | grep -E -- "-cmp" | sort
for d in runs/pb-*-cmp3 runs/pb-*-cmps runs/pb-*-cmp3r2 runs/pb-*-cmpsr2; do
  [ -d "$d" ] && .venv/bin/python $F/validate_run.py "$d"
done
