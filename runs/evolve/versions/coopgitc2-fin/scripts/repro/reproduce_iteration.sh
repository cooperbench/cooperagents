#!/usr/bin/env bash
# Reproduce one iteration of the qwen-9B program (report: docs/TEAM_HARNESS_OPTIMIZATION.md).
# Prints the exact measurement commands for iteration N; add --run to execute
# them sequentially. Job lines are verbatim from the dispatched queues
# (scripts/repro/dispatched_jobs.txt) unless marked "(reconstructed)".
# k-repetition = one command per run label. Grading happens inside each run.
set -euo pipefail
cd "$(dirname "$0")/../.."
N="${1:?usage: reproduce_iteration.sh <1-20> [--run]}"; MODE="${2:-print}"
Q() { echo "ENV_FILE=.env.qwen scripts/measure_qwen.sh $*"; }
D2() { echo "ENV_FILE=.env.qwen scripts/measure_dev2.sh $*"; }
HO() { echo "ENV_FILE=.env.qwen scripts/measure_heldout.sh $*"; }
CMDS=()
add() { CMDS+=("$1"); }
case "$N" in
1) echo "# Iteration 1: fixed-10 baselines (infra fixes are in the code now)."
   add "ENV_FILE=.env.qwen scripts/measure.sh flash10-base";;
2) echo "# Iteration 2: calibration sweep that produced the qwen-14 set."
   echo "# The resulting set is FROZEN in scripts/measure_qwen.sh; rerunning the"
   echo "# sweep is not needed to reproduce later iterations."
   add "$(Q qwen14-solo-cal)";;
3) echo "# Iteration 3: noise floor + baselines under pinned temperature (k=3)."
   for r in a b c; do add "$(Q qwen14-base-$r)"; done;;
4) echo "# Iteration 4: Q1 do-no-harm gate (parked). (reconstructed)"
   for r in a b c; do add "$(Q qwen14-q1-$r --team-only --do-no-harm)"; done;;
5) echo "# Iteration 5: Sequential Best-of-2 (Q2), k=3. (reconstructed)"
   for r in a b c; do add "$(Q qwen14-q2-$r --team-only --best-of-n 2 --select mechanical)"; done;;
6) echo "# Iteration 6: Diversified Best-of-2 (Q3), k=2. (reconstructed)"
   for r in a b; do add "$(Q qwen14-q3-$r --team-only --best-of-n 2 --select mechanical --diversity-temp 0.7)"; done;;
7) echo "# Iteration 7: Coop (Q4), k=2. (reconstructed)"
   for r in a b; do add "$(Q qwen14-q4-$r --team-only --no-seed --coop-tools)"; done;;
8) echo "# Iteration 8: Coop+Repair (Q5), k=3. Apply-chain merge was the default"
   echo "# at measurement time; --apply-merge restores it. (reconstructed)"
   for r in a b c; do add "$(Q qwen14-q5-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --apply-merge)"; done;;
9) echo "# Iteration 9: merge/gate variants. Q5f step-cap, Q5g 3-way (k=5), Q10 gate."
   for r in a b c; do add "$(Q qwen14-q5f-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 25 --apply-merge)"; done
   for r in a b c d e; do add "$(Q qwen14-q5g-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50)"; done
   for r in a2 b2 b3; do add "$(Q qwen14-q10-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --preserve-invariants --behavioral-gate)"; done;;
10) echo "# Iteration 10: selection stacks Q7/Q7g/Q8/Q11 (Q7,Q7g reconstructed)."
    for r in a b c; do add "$(Q qwen14-q7-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 25 --apply-merge --best-of-n 2 --select mechanical)"; done
    for r in a b c; do add "$(Q qwen14-q7g-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --best-of-n 2 --select mechanical)"; done
    for r in a b c; do add "$(Q qwen14-q8-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --preserve-invariants --best-of-n 2 --select mechanical --concurrency 3)"; done
    for r in a b d; do add "$(Q qwen14-q11-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --preserve-invariants --behavioral-gate --best-of-n 2 --select mechanical --concurrency 3)"; done;;
11) echo "# Iteration 11: toolkit with system-prompt billing, TK1-TK5, k=3 (TK4 k=5)."
    for r in a b c; do add "$(Q qwen14-tk1-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --contract-first)"; done
    for r in a b c; do add "$(Q qwen14-tk2-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --live-awareness)"; done
    for r in a b c; do add "$(Q qwen14-tk3-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol)"; done
    for r in a b c d e; do add "$(Q qwen14-tk4-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board)"; done
    for r in a b c; do add "$(Q qwen14-tk5-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --wait-protocol)"; done;;
12) echo "# Iteration 12: allocation tools TK6 (N=2, N=3) and TK7 spawn, k=3."
    for r in a b c; do add "$(Q qwen14-tk6n2-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --claim-mode --agents 2)"; done
    for r in a b c; do add "$(Q qwen14-tk6n3-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --claim-mode --agents 3 --max-agents 3 --concurrency 3)"; done
    for r in a b c; do add "$(Q qwen14-tk7-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --claim-mode --agents 2 --allow-spawn --max-agents 4 --concurrency 3)"; done;;
13) echo "# Iteration 13: Board Best-of-2 (TK9) k=5 + Board+Repair (TK8) k=3."
    for r in a b c d e; do add "$(Q qwen14-tk9-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --best-of-n 2 --select mechanical --concurrency 3)"; done
    for r in a b c; do add "$(Q qwen14-tk8-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge)"; done;;
14) echo "# Iteration 14: TK9f (480s repair cap) k=3 + repair-time sweep 240/900/1800."
    for r in a b c; do add "$(Q qwen14-tk9f-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --best-of-n 2 --select mechanical --concurrency 3 --repair-time 480)"; done
    for t in 240 900 1800; do for r in a b c; do add "$(Q qwen14-rt$t-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --best-of-n 2 --select mechanical --concurrency 3 --repair-time $t)"; done; done;;
15) echo "# Iteration 15: focused repair (R2) k=3, Best-of-3 k=3, Best-of-4 k=5."
    for r in a b c; do add "$(Q qwen14-r2-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --select mechanical --best-of-n 2 --concurrency 3 --focused-repair)"; done
    for r in a b c; do add "$(Q qwen14-bo3-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --select mechanical --best-of-n 3 --concurrency 2)"; done
    for r in a b c d e2; do add "$(Q qwen14-bo4-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --select mechanical --best-of-n 4 --concurrency 2)"; done;;
16) echo "# Iteration 16: held-out certification (seed-7 set), k=3 per configuration."
    for r in a b c; do add "$(HO qwen-ho-q5-$r --no-seed --coop-tools --repair-integrator --repair-steps 50 --apply-merge)"; done
    for r in a b c; do add "$(HO qwen-ho-tk9-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --best-of-n 2 --select mechanical --concurrency 2)"; done
    for r in a b c; do add "$(HO qwen-ho-bo3-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --best-of-n 3 --select mechanical --concurrency 2)"; done
    for r in a b c; do add "$(HO qwen-ho-bo4-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --best-of-n 4 --select mechanical --concurrency 2)"; done
    for r in a b c; do add "$(HO qwen-ho-q2-$r --team-only --best-of-n 2 --select mechanical)"; done;;
17) echo "# Iteration 17: Coordinator (C2) qwen-14 k=3 + dev-set-2 k=5 with control."
    for r in a b c; do add "$(Q qwen14-c2-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --apply-merge --coordinator)"; done
    for r in b c d e f; do add "$(D2 qwen-d2-c2-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --apply-merge --coordinator)"; done
    for r in b c d e f; do add "$(D2 qwen-d2-q5-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --apply-merge)"; done;;
18) echo "# Iteration 18: Board Best-of-2 + Coordinator (TK9C2), k=3."
    for r in a b c; do add "$(Q qwen14-tk9c2-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --tool-protocol --task-board --apply-merge --best-of-n 2 --select mechanical --concurrency 3 --coordinator)"; done;;
19) echo "# Iteration 19: Coop+git (q4git) and Coop+Repair+git (tkgit), k=3 each."
    for r in a b c; do add "$(Q qwen14-q4git-$r --team-only --no-seed --coop-tools --git-share)"; done
    for r in a b c; do add "$(Q qwen14-tkgit-$r --team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --apply-merge --git-share)"; done;;
20) echo "# Iteration 20: complete-Team cell (teamfull), k=3."
    for r in a b c; do add "$(Q qwen14-teamfull-$r --team-only --no-seed --coop-tools --task-board --team-roles)"; done;;
*) echo "unknown iteration: $N" >&2; exit 1;;
esac
for c in "${CMDS[@]}"; do
  echo "$c"
  if [ "$MODE" = "--run" ]; then eval "$c"; fi
done
