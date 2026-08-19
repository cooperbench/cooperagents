#!/usr/bin/env python3
"""Live agent-starvation check: are agents starving RIGHT NOW?

Reads the heartbeat files written by the model wrapper when
COOPER_HEARTBEAT_DIR is set (one line per completed LLM call, named
<harness_pid>_<agent_id>.hb). For every LIVE harness process it reports,
per agent:

  STARVING - the harness has been up longer than --grace seconds and this
             agent has no completed LLM call yet (this is the failure that
             the post-hoc validator would later flag as "first call delayed").
  STALE    - the agent completed calls before, but none in --stale seconds
             (starvation/stall after admission; threshold must exceed the
             endpoint's longest legitimate generation).
  OK       - a call completed within --stale seconds.

Heartbeat files whose harness pid is dead are ignored (finished cells are
the post-hoc validator's job). Exit: 0 all OK, 3 any STARVING/STALE.

Run on a fleet node:   .venv/bin/python live_starvation_check.py
Across the fleet:      for ip in $(cat nodes.txt); do ssh $ip ...; done
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time


def _proc_alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


def _proc_started(pid: int) -> float | None:
    """Process start as unix time, from /proc (jiffies since boot)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        start_jiffies = float(fields[19])  # field 22 overall
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
        return time.time() - uptime + start_jiffies / hz
    except Exception:  # noqa: BLE001
        return None


def _proc_label(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode("utf-8", "replace").replace("\0", " ")
        m = re.search(r"--rep (\S+)", cmd)
        return m.group(1) if m else f"pid{pid}"
    except Exception:  # noqa: BLE001
        return f"pid{pid}"


def _team_size(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode("utf-8", "replace").replace("\0", " ")
        m = re.search(r"--team-size (\d+)", cmd)
        return int(m.group(1)) if m else 1
    except Exception:  # noqa: BLE001
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hb-dir", default=os.getenv("COOPER_HEARTBEAT_DIR", "/tmp/cooper_hb"))
    ap.add_argument("--grace", type=float, default=600.0,
                    help="seconds after harness start before a missing first "
                         "call counts as STARVING (covers container setup)")
    ap.add_argument("--stale", type=float, default=1200.0,
                    help="seconds without a completed call before STALE; set "
                         "above the endpoint's longest legitimate generation")
    args = ap.parse_args()

    now = time.time()
    by_pid: dict[int, dict[str, str]] = {}
    for path in glob.glob(os.path.join(args.hb_dir, "*.hb")):
        name = os.path.basename(path)[:-3]
        pid_s, _, rest = name.partition("_")
        # new format pid_starttime_agent; old format pid_agent
        head, _, tail = rest.partition("_")
        agent = tail if (head.isdigit() and tail) else rest
        if pid_s.isdigit():
            by_pid.setdefault(int(pid_s), {})[agent or "?"] = path

    # live harness processes may have agents with NO heartbeat file yet —
    # discover them via any live pid that has a dir entry, plus scan for
    # harness processes that produced no files at all (fully starved cell).
    for pid_dir in glob.glob("/proc/[0-9]*/cmdline"):
        pid = int(pid_dir.split("/")[2])
        try:
            with open(pid_dir, "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        if "bench_programbench.py" in cmd and pid not in by_pid:
            by_pid[pid] = {}

    # Processes started before the heartbeat-instrumented wrapper was deployed
    # run old code and can never write heartbeats — never call them STARVING.
    wrapper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "../../src/cooperagents/vendor/mini_swe/models/litellm_model.py")
    deploy_ts = os.path.getmtime(wrapper) if os.path.exists(wrapper) else 0.0

    bad = 0
    for pid, agents in sorted(by_pid.items()):
        if not _proc_alive(pid):
            continue
        label = _proc_label(pid)
        started = _proc_started(pid)
        if started is not None and started < deploy_ts and not agents:
            print(f"PRE-TELEMETRY {label}: started before heartbeat deploy; skipped")
            continue
        age = now - started if started else None
        expected = _team_size(pid)
        seen = 0
        for agent, path in sorted(agents.items()):
            try:
                lines = open(path).read().strip().splitlines()
            except Exception:  # noqa: BLE001
                lines = []
            if not lines:
                continue
            seen += 1
            if "end=" in lines[-1]:
                final = lines[-1].split("end=")[1]
                print(f"FINISHED {label}/{agent}: {final} after {len(lines) - 1} calls")
                continue
            last_ts = float(lines[-1].split()[0])
            gap = now - last_ts
            if gap > args.stale:
                print(f"STALE    {label}/{agent}: last completed call {gap:.0f}s ago "
                      f"({len(lines)} calls total)")
                bad += 1
            else:
                print(f"OK       {label}/{agent}: {len(lines)} calls, last {gap:.0f}s ago")
        missing = expected - seen
        if missing > 0 and age is not None and age > args.grace:
            print(f"STARVING {label}: {missing}/{expected} agents with NO completed "
                  f"LLM call {age:.0f}s after harness start")
            bad += missing
    if not by_pid:
        print(f"no live harness processes / heartbeats under {args.hb_dir}")
    return 3 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
