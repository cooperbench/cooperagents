#!/bin/bash
# Collect finished fleet runs: for each node, rsync back any run dir that has a
# .DONE marker and is not yet local, then print its score.
# Usage: collect.sh   (idempotent; run any time)
SSH="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -i $HOME/.ssh/fleet_key"
cd /home/ubuntu/CooperAgents
for ip in $(cat scripts/fleet/nodes.txt); do
  for marker in $($SSH ubuntu@$ip "ls CooperAgents/runs/*.DONE 2>/dev/null" 2>/dev/null); do
    name=$(basename "$marker" .DONE)
    if [ ! -f "runs/$name.DONE" ]; then
      rsync -az -e "$SSH" "ubuntu@$ip:CooperAgents/runs/$name" "ubuntu@$ip:CooperAgents/runs/$name.launch.log" "ubuntu@$ip:CooperAgents/runs/$name.DONE" runs/ 2>/dev/null
      echo "collected $name from $ip: $(cat runs/$name.DONE)"
    fi
    f=$(ls "runs/$name"/abishekvashok__cmatrix.5c082c6/*.eval.json 2>/dev/null | head -1)
    if [ -n "$f" ]; then
      python3 - "$name" "$f" <<'PYEOF'
import json, sys
name, f = sys.argv[1], sys.argv[2]
tr = json.load(open(f))["test_results"]
p = sum(1 for t in tr if t["status"] == "passed")
print(f"{name}: {p}/{len(tr)} = {100*p/len(tr):.1f}")
PYEOF
    else
      echo "$name: no eval.json (run failed or eval failed; see runs/$name/eval.log)"
    fi
  done
done
