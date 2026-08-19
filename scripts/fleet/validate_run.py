"""Resource validation for measurement runs.

Two modes:

  preflight    Check the serving endpoint and fleet BEFORE a batch:
                 - warm single-completion latency
                 - 3 concurrent near-max-context completions in parallel
                 - fleet nodes reachable
               Exit 0 = safe to dispatch.

  <run_dir>    Validate a completed run's trajectories AFTER the fact:
               a cell's score should only count if the run was not
               resource-limited. Checks per agent:
                 - first-call delay        <= 300s   (starvation)
                 - median call latency     <= 10s    (sustained serving)
                 - stall calls (>60s, <50 chars/s)  <= 3
                 - format-error retries    <= 3     (parser health)
                 - terminal status is not an infrastructure error
               Prints PASS/FAIL with reasons; exit 0 on PASS.

Usage:
  .venv/bin/python scripts/fleet/validate_run.py preflight
  .venv/bin/python scripts/fleet/validate_run.py runs/pb-coopgitc2-cmatrix-r1t3
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

LIMITS = {
    "first_call_delay_s": 300,
    "median_latency_s": 10,
    "max_stall_calls": 3,
    "max_format_errors": 3,
}


def preflight() -> int:
    url = os.environ["OPENAI_BASE_URL"] + "/chat/completions"

    def req(prompt: str, timeout: int = 300) -> float:
        body = json.dumps({"model": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Qwen/Qwen3.5-9B"),
                           "max_tokens": 64,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        r = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer dummy"})
        t0 = time.time()
        urllib.request.urlopen(r, timeout=timeout).read()
        return time.time() - t0

    ok = True
    # 1. warm-up + single latency (first call may absorb a cold boot)
    warm = req("say ok", timeout=400)
    single = req("say ok")
    print(f"single completion: warm-up {warm:.1f}s, then {single:.1f}s "
          f"{'OK' if single < 10 else 'FAIL'}")
    ok &= single < 10
    # 2. concurrent near-max-context (the realistic agent shape)
    big = "Context:\n" + ("value_x = compute_function(input_y)\n" * 2800) + "\nOne short sentence."
    res: dict[int, float] = {}

    def w(i: int) -> None:
        res[i] = req(big, timeout=400)

    ths = [threading.Thread(target=w, args=(i,)) for i in range(3)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    spread = f"{min(res.values()):.1f}-{max(res.values()):.1f}s"
    par = max(res.values()) < 60 and max(res.values()) < 3 * min(res.values()) + 10
    print(f"3x concurrent ~22k-token: {spread} {'OK (parallel)' if par else 'FAIL (serialized/slow)'}")
    ok &= par
    # 3. fleet reachability
    ssh = ["ssh", "-o", "ConnectTimeout=8", "-i", os.path.expanduser("~/.ssh/fleet_key")]
    nodes = open(os.path.join(os.path.dirname(__file__), "nodes.txt")).read().split()
    up = sum(subprocess.run([*ssh, f"ubuntu@{ip}", "true"], capture_output=True,
                            stdin=subprocess.DEVNULL, timeout=15).returncode == 0
             for ip in nodes)
    print(f"fleet: {up}/{len(nodes)} nodes reachable {'OK' if up >= 3 else 'FAIL'}")
    ok &= up >= 3
    print("PREFLIGHT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def validate(run_dir: str) -> int:
    trajs = glob.glob(os.path.join(run_dir, "*", "trajectories", "*.json"))
    if not trajs:
        print(f"FAIL {run_dir}: no trajectories")
        return 1
    problems = []
    all_ts = []
    per_agent = {}
    for t in sorted(trajs):
        a = json.loads(open(t).read())
        ts = [m["extra"]["timestamp"] for m in a["messages"] if (m.get("extra") or {}).get("timestamp")]
        per_agent[os.path.basename(t)[:-5]] = (a, ts)
        all_ts += ts
    t0 = min(all_ts) if all_ts else 0
    for aid, (a, ts) in per_agent.items():
        if aid.startswith("repair"):
            continue
        if a.get("error") and "LimitsExceeded" not in str(a["error"]):
            problems.append(f"{aid}: terminal error {str(a['error'])[:80]}")
        fmt = sum(1 for m in a["messages"]
                  if isinstance(m.get("content"), str) and "No tool calls found" in m["content"])
        if fmt > LIMITS["max_format_errors"]:
            problems.append(f"{aid}: {fmt} format-error retries")
        prev, lats, first, stalls = None, [], None, 0
        for m in a["messages"]:
            ex = m.get("extra") or {}
            mts = ex.get("timestamp")
            if mts is None:
                continue
            if ex.get("summary"):
                # compaction summaries are inserted out of chronological
                # order; pairing across them fabricates giant/negative
                # latencies (observed: healthy 2s/step run flagged at 62.8s)
                prev = None
                continue
            if prev is not None and mts < prev:
                prev = None
                continue
            if m["role"] == "assistant":
                if prev is None:
                    first = mts - t0
                else:
                    lat = mts - prev
                    out = len(str(m.get("content") or ""))
                    for tc in m.get("tool_calls") or []:
                        out += len(str(tc.get("function", {}).get("arguments", "")))
                    # WAIT-latency sample: only calls with small outputs —
                    # a 30s call that generated a whole file is healthy
                    # throughput, not queueing. Guard against the earlier
                    # bug (tiny sample -> outlier reported as median) with a
                    # minimum sample size at the check site.
                    if out < 500:
                        lats.append(lat)
                    if lat > 60 and out / max(lat, 1) < 50:
                        stalls += 1  # pathological calls counted separately
            prev = mts
        # Compaction discards early messages, so the earliest SURVIVING
        # timestamp of a long-running agent lands late in the run and mimics
        # a delayed first call. Real starvation leaves the agent with few
        # completed steps; require both signals.
        # Heartbeat telemetry, when present, is ground truth and overrides
        # both trajectory heuristics: hb filename embeds the harness start
        # (pid_start_agent.hb) and line 1 is the first COMPLETED call.
        hb_delay = None
        for hb in glob.glob(os.path.join(run_dir, "heartbeats", f"*_{aid}.hb")):
            parts = os.path.basename(hb)[:-3].split("_")
            try:
                hb_start = float(parts[1])
                with open(hb) as fh:
                    hb_delay = float(fh.readline().split()[0]) - hb_start
            except (IndexError, ValueError):
                pass
        if hb_delay is not None:
            if hb_delay > LIMITS["first_call_delay_s"]:
                problems.append(f"{aid}: first call completed {hb_delay:.0f}s after "
                                f"harness start (starved; heartbeat evidence)")
        elif (first is not None and first > LIMITS["first_call_delay_s"]
                and a.get("steps", 0) < 100):
            problems.append(f"{aid}: first call delayed {first:.0f}s with only "
                            f"{a.get('steps', 0)} steps (starved)")
        if len(lats) >= 8:  # need a real sample of quick calls
            med = sorted(lats)[len(lats) // 2]
            if med > LIMITS["median_latency_s"]:
                problems.append(f"{aid}: median wait-latency {med:.1f}s over {len(lats)} quick calls")
        if stalls > LIMITS["max_stall_calls"]:
            problems.append(f"{aid}: {stalls} stalled calls")
    if problems:
        print(f"FAIL {run_dir}:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"PASS {run_dir} ({len(per_agent)} agents; resource-clean)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "preflight"
    sys.exit(preflight() if arg == "preflight" else validate(arg))
