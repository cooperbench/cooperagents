#!/bin/bash
# 10-task ProgramBench benchmark: solo + coopgitc2 (the Pareto-front arms) on
# the first 10 alphabetical instances with available cleanroom images, k=1
# per cell, averaged per arm across tasks. Two waves over the 12-node fleet.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
NODES=($(cat $F/nodes.txt))

# instance_id short_tag
JOBS=$(cat <<'EOF'
abishekvashok__cmatrix.5c082c6 cmatrix
agourlay__zip-password-finder.704700d zipfinder
ajeetdsouza__zoxide.67ca1bc zoxide
alecthomas__chroma.8d04def chroma
alexpovel__srgn.89f943b srgn
altdesktop__i3-style.f93821b i3style
ammarabouzor__tui-journal.2b4540d tuijournal
anordal__shellharden.6a6ffd4 shellharden
antonmedv__fx.86d0d34 fx
antonmedv__walk.bf802ef walk
EOF
)

dispatch_wave() {  # args: start_task_idx end_task_idx
  local n=0 i=0
  while read -r inst tag; do
    [ -z "$inst" ] && continue
    if [ $i -ge $1 ] && [ $i -lt $2 ]; then
      for arm in solo coopgitc2; do
        ip=${NODES[$((n % ${#NODES[@]}))]}
        $F/pbrun.sh "$ip" "$inst" "$arm" "${tag}-m1"
        n=$((n+1))
      done
    fi
    i=$((i+1))
  done <<< "$JOBS"
}

wait_wave() {  # arg: expected number of local DONE markers for -m1 runs
  until [ $(ls runs/pb-*-m1.DONE 2>/dev/null | wc -l) -ge $1 ]; do
    $F/collect.sh >/dev/null 2>&1
    sleep 300
  done
}

echo "=== wave 1: tasks 0-5 ($(date -u +%H:%M)) ==="
dispatch_wave 0 6
wait_wave 12
echo "=== wave 2: tasks 6-9 ($(date -u +%H:%M)) ==="
dispatch_wave 6 10
wait_wave 20
echo "=== all 20 collected ($(date -u +%H:%M)) ==="
$F/collect.sh 2>/dev/null | grep -E -- "-m1:" | sort
python3 - <<'PYEOF'
import glob, json, re
from collections import defaultdict
scores = defaultdict(dict)
for f in glob.glob("runs/pb-*-m1/*/*.eval.json"):
    m = re.match(r"runs/pb-(solo|coopgitc2)-(.+)-m1/", f)
    if not m:
        continue
    arm, tag = m.groups()
    tr = json.load(open(f))["test_results"]
    p = sum(1 for t in tr if t["status"] == "passed")
    scores[arm][tag] = 100 * p / len(tr)
tags = sorted(set(t for a in scores.values() for t in a))
print(f"{'task':14s} {'solo':>6s} {'coopgitc2':>10s}")
for t in tags:
    s = scores['solo'].get(t); c = scores['coopgitc2'].get(t)
    print(f"{t:14s} {s if s is None else round(s,1)!s:>6s} {c if c is None else round(c,1)!s:>10s}")
for arm in ("solo", "coopgitc2"):
    v = [scores[arm].get(t, 0.0) for t in tags]  # missing eval counts 0
    print(f"MEAN {arm}: {sum(v)/len(v):.1f} over {len(v)} tasks ({sum(1 for t in tags if t in scores[arm])} graded)")
PYEOF
echo "MBENCH10 DONE"
