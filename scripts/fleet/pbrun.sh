#!/bin/bash
# Dispatch one ProgramBench run to a fleet node, detached, with inline eval.
# Usage: pbrun.sh <ip> <arm> <rep> [extra bench_programbench.py flags...]
# Default flags (current program config): --step-limit 1000 --repair --agent-time-limit 3600
# The node writes runs/pb-<arm>-<rep>/ and drops runs/pb-<arm>-<rep>.DONE when
# run + eval are both finished. Collect with collect.sh.
set -e
ip=$1; arm=$2; rep=$3; shift 3
extra=${*:---step-limit 1000 --repair --agent-time-limit 3600}
SSH="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -i $HOME/.ssh/fleet_key"

$SSH ubuntu@$ip "cat > /tmp/pbjob_${arm}_${rep}.sh" <<EOF
#!/bin/bash
cd \$HOME/CooperAgents
set -a; source .env.qwen; set +a
export PATH="\$HOME/.local/bin:\$PATH"
.venv/bin/python scripts/bench_programbench.py --arm $arm --rep $rep $extra \
  > runs/pb-$arm-$rep.launch.log 2>&1
rc=\$?
d=runs/pb-$arm-$rep
if [ -d "\$d/abishekvashok__cmatrix.5c082c6" ]; then
  (cd \$HOME/ProgramBench && uv run programbench eval \$HOME/CooperAgents/\$d) >> "\$d/eval.log" 2>&1
fi
echo "rc=\$rc \$(date -u +%FT%TZ)" > runs/pb-$arm-$rep.DONE
EOF
# timeout guard: the launch takes effect immediately, but ssh can linger on the
# detached child's inherited fds — don't let that block the caller.
timeout 20 $SSH ubuntu@$ip "chmod +x /tmp/pbjob_${arm}_${rep}.sh && mkdir -p CooperAgents/runs && setsid nohup /tmp/pbjob_${arm}_${rep}.sh </dev/null >/dev/null 2>&1 & echo dispatched $arm-$rep to $ip" || true
echo "dispatch issued: $arm-$rep -> $ip"
