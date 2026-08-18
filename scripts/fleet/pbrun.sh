#!/bin/bash
# Dispatch one ProgramBench run to a fleet node, detached, with inline eval.
# Usage: pbrun.sh <ip> <instance_id> <arm> <rep> [extra bench_programbench.py flags...]
# Default flags (current program config): --step-limit 1000 --repair --agent-time-limit 3600
# The node writes runs/pb-<arm>-<rep>/ and drops runs/pb-<arm>-<rep>.DONE when
# run + eval are both finished. Make <rep> unique per (instance, rep) — e.g.
# "cmatrix-m1" — since the run dir name is pb-<arm>-<rep>.
# Collect with collect.sh.
set -e
ip=$1; inst=$2; arm=$3; rep=$4; shift 4
extra=${*:---step-limit 1000 --repair --agent-time-limit 3600}
SSH="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -i $HOME/.ssh/fleet_key"

# Idempotency: skip if this run already exists on the node (a relaunched
# driver must not clobber completed or in-flight runs).
if timeout 15 $SSH ubuntu@$ip "test -f CooperAgents/runs/pb-$arm-$rep.launch.log" </dev/null 2>/dev/null; then
  echo "skip: $arm-$rep already present on $ip"
  exit 0
fi

$SSH ubuntu@$ip "cat > /tmp/pbjob_${arm}_${rep}.sh" <<EOF
#!/bin/bash
cd \$HOME/CooperAgents
set -a; source ${COOPER_ENV_FILE:-.env.qwen}; set +a
export PATH="\$HOME/.local/bin:\$PATH"
.venv/bin/python scripts/bench_programbench.py --instance $inst --arm $arm --rep $rep $extra \
  > runs/pb-$arm-$rep.launch.log 2>&1
rc=\$?
d=runs/pb-$arm-$rep
if [ -d "\$d/$inst" ]; then
  (cd \$HOME/ProgramBench && uv run programbench eval \$HOME/CooperAgents/\$d) >> "\$d/eval.log" 2>&1
fi
echo "rc=\$rc inst=$inst \$(date -u +%FT%TZ)" > runs/pb-$arm-$rep.DONE
EOF
# timeout guard: the launch takes effect immediately, but ssh can linger on the
# detached child's inherited fds — don't let that block the caller.
# </dev/null is load-bearing: without it this ssh consumes the stdin of any
# while-read loop calling pbrun.sh, silently truncating the caller's job list.
timeout 20 $SSH ubuntu@$ip "chmod +x /tmp/pbjob_${arm}_${rep}.sh && mkdir -p CooperAgents/runs && setsid nohup /tmp/pbjob_${arm}_${rep}.sh </dev/null >/dev/null 2>&1 & echo dispatched $arm-$rep to $ip" </dev/null || true
echo "dispatch issued: $arm-$rep [$inst] -> $ip"
