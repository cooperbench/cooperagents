#!/bin/bash
# Factory-23 plain-solo baseline (NO --repair): the 23 ProgramBench tasks from
# the Factory.ai blog, Qwen3.8-27B via Tinker OpenAI-compatible endpoint.
# Up to 4 solo cells per node across 6 nodes; scores+validates each landing.
cd /home/ubuntu/CooperAgents
F=scripts/fleet
export COOPER_ENV_FILE=.env.tinker27
FLAGS="--step-limit 1000 --agent-time-limit 14400"
log() { echo "$(date -u +%H:%M) $*"; }

INSTANCES=(
  osgeo__gdal.0847f12 ip7z__7zip.839151e duckdb__duckdb.bdb65ec
  jgm__pandoc.5caad90 ffmpeg__ffmpeg.360a402 samtools__samtools.aa823b5
  tree-sitter__tree-sitter.5e23cca universal-ctags__ctags.243595e
  danmar__cppcheck.0a5b103 ast-grep__ast-grep.dde0fe0 arq5x__bedtools2.dd57059
  dandavison__delta.acd758f doxygen__doxygen.966d98e
  jesseduffield__lazygit.1d0db51 tstack__lnav.ee34494 peco__peco.4e58dad
  osgeo__proj.75d455c boyter__scc.515f91c paradigmxyz__solar.5190d0e
  chirlu__sox.42b3557 stacked-git__stgit.430027d ivanceras__svgbob.6d00ad9
  typst__typst.88356d0
)
short() { echo "$1" | sed 's/.*__//;s/\..*//'; }

runningcount() {  # cells dispatched-not-done on node line $1
  local ln=$1 n=0
  for inst in "${INSTANCES[@]}"; do
    local rep="$(short $inst)-f23solo"
    [ -f "runs/.disp_$rep" ] && [ ! -f "runs/pb-solo-$rep.DONE" ] \
      && [ "$(cat runs/.disp_$rep)" = "$ln" ] && n=$((n+1))
  done
  echo $n
}

while :; do
  $F/collect.sh </dev/null >/dev/null 2>&1
  for d in runs/pb-solo-*-f23solo; do
    [ -d "$d" ] && [ -f "$d.DONE" ] && [ ! -f "$d/.val" ] || continue
    score=$(grep -oE "score=[0-9.]+|resolved.*" "$d.launch.log" 2>/dev/null | head -1)
    if .venv/bin/python $F/validate_run.py "$d" > "$d/.valout" 2>&1; then
      log "CELL VALID $(basename $d) $score"
    else
      log "CELL INVALID $(basename $d) $score $(tail -2 "$d/.valout" | head -1)"
    fi
    touch "$d/.val"
  done
  alldone=1
  for inst in "${INSTANCES[@]}"; do
    rep="$(short $inst)-f23solo"
    [ -f "runs/pb-solo-$rep.DONE" ] && continue
    alldone=0
    [ -f "runs/.disp_$rep" ] && continue
    for ln in 1 2 3 4 5 6; do
      if [ "$(runningcount $ln)" -lt 4 ]; then
        ip=$(sed -n "${ln}p" $F/nodes.txt)
        if $F/pbrun.sh "$ip" "$inst" solo "$rep" $FLAGS >/dev/null 2>&1; then
          echo "$ln" > "runs/.disp_$rep"
          log "DISPATCHED solo/$rep -> $ip (node $ln)"
        else
          log "WARNING dispatch failed $rep -> $ip"
        fi
        break
      fi
    done
  done
  if [ "$alldone" = "1" ]; then
    log "FACTORY23 BATCH COMPLETE — stop the EC2 nodes"
    break
  fi
  sleep 180
done
