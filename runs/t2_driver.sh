#!/bin/bash
# T2 scaling sweep: 10 ProgramBench tasks, coopgitc2 team-size 2, full fin
# stack, Qwen3.5-9B on Modal (.env.qwen, greedy) — completes solo/t2/t3/t4.
# Runs IN PARALLEL with the q27 sweep (separate endpoints: Modal 9B vs GKE
# 27B). To limit node CPU contention, each cell is dispatched to a fleet node
# with no active runner (q27 nodes free up rolling).
# Steps: raise Modal floor -> health + starvation preflight -> dispatch
# (concurrency 3, free nodes only) -> collect+validate -> revert floor.
set -u
cd /home/ubuntu/CooperAgents
F=scripts/fleet
SERVE=/home/ubuntu/CooperTrain/coopertrain/serve/vllm_modal_qwen35_9b_base.py
MODAL=/home/ubuntu/.local/bin/modal
B9=https://cooperbench--qwen35-9b-bf16-32k-serve.modal.run/v1
SSH="ssh -o ConnectTimeout=8 -i $HOME/.ssh/fleet_key"

free_node() {  # first fleet node with no bench runner; blocks until one exists
  while :; do
    for ip in $(cat $F/nodes.txt); do
      n=$(timeout 15 $SSH ubuntu@$ip 'pgrep -cf "bench_programbenc[h]" || true' </dev/null 2>/dev/null)
      [ "${n:-1}" = "0" ] && { echo "$ip"; return; }
    done
    sleep 120
  done
}

echo "[t2] raising Modal floor for the sweep window"
sed -i 's/    min_containers=0,/    min_containers=2,  # sweep window (t2 batch); revert after/' $SERVE
(cd /home/ubuntu/CooperTrain && $MODAL deploy coopertrain/serve/vllm_modal_qwen35_9b_base.py) \
  || echo "[t2] WARN: modal deploy failed; continuing (endpoint scales on demand)"

echo "[t2] waiting for 9B endpoint health"
ok=0
for i in $(seq 1 60); do
  [ "$(curl -s -m 20 -o /dev/null -w '%{http_code}' $B9/models)" = "200" ] && { ok=1; break; }
  sleep 30
done
[ "$ok" != 1 ] && { echo "[t2] ABORT: 9B endpoint never became healthy"; exit 1; }

echo "[t2] starvation preflight (concurrency 6 = 3 cells x 2 agents)"
set -a; source .env.qwen; set +a
.venv/bin/python $F/starvation_test.py --concurrency 6 --skip-sustained \
  --json-out runs/starv_pre_t2.json
if [ $? -eq 2 ]; then
  echo "[t2] ABORT: deployment starves at dispatch concurrency — not dispatching"
  exit 2
fi

export COOPER_ENV_FILE=.env.qwen
FLAGS="--step-limit 1000 --repair --agent-time-limit 3600 --completion-gate --env-brief --presub-merge --team-size 2"
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
count() { ls runs/pb-*-fin2.DONE 2>/dev/null | wc -l; }
BASE=$(count); i=0
while read -r inst tag; do
  [ -z "$inst" ] && continue
  while :; do
    [ $(( i - ($(count) - BASE) )) -lt 3 ] && break
    $F/collect.sh </dev/null >/dev/null 2>&1; sleep 180
  done
  ip=$(free_node)
  echo "[t2] dispatching ${tag}-fin2 to free node $ip"
  $F/pbrun.sh "$ip" "$inst" coopgitc2 "${tag}-fin2" $FLAGS
  i=$((i+1))
done <<< "$TASKS"
until [ "$(count)" -ge "$((BASE + 10))" ]; do
  $F/collect.sh </dev/null >/dev/null 2>&1; sleep 300
done
echo "=== T2 SWEEP DONE ==="
$F/collect.sh </dev/null 2>/dev/null | grep -- "-fin2" | sort
for d in runs/pb-*-fin2; do [ -d "$d" ] && .venv/bin/python $F/validate_run.py "$d"; done
echo "[t2] reverting Modal floor"
sed -i 's/    min_containers=2,  # sweep window (t2 batch); revert after/    min_containers=0,/' $SERVE
(cd /home/ubuntu/CooperTrain && $MODAL deploy coopertrain/serve/vllm_modal_qwen35_9b_base.py) \
  && echo "[t2] floor reverted" || echo "[t2] WARN: revert deploy failed — floor still 2"
echo "=== T2 ALL COMPLETE ==="
