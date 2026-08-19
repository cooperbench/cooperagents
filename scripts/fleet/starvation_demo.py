#!/usr/bin/env python3
"""Reproduce agent starvation with a mock LLM server — no GPU needed.

The mock server has `capacity` concurrent generation slots and a FIFO
admission queue (a stand-in for vLLM's continuous-batching capacity).
Each request occupies a slot for `gen_time` seconds. Simulated agents
send a request, wait for it, then loop — like a mini-swe agent step loop.

Time is scaled 100x (1s here = 100s real) so each scenario runs in seconds.

Scenarios (defaults mirror the 2026-08-19 q27 incident):
  burst    30 agents dispatched at once into 8 slots. The 4th admission
           wave waits >3 "gen rounds" for its FIRST response — the exact
           signature the run validator flags as "first call delayed >300s".
  storm    client timeout SHORTER than generation time. Every request
           times out and is retried; abandoned requests keep burning slots;
           NOBODY ever completes although the server is 100% busy.
           (Our case: 180s litellm timeout vs 27B thinking generations.)
  capped   dispatch concurrency capped at 3 cells (9 agents) into the same
           8 slots: mild queueing, bounded first-call delay, no starvation.
           NOTE a mere start-stagger of all 30 agents does NOT fix it —
           30 looping agents on 8 slots are oversubscribed in steady state;
           only capping concurrency or scaling capacity does.
  scaled   same burst but capacity 32 (autoscaler already scaled out):
           everyone admitted immediately.

Run:  python3 scripts/fleet/starvation_demo.py            # all scenarios
      python3 scripts/fleet/starvation_demo.py burst      # just one
Against a real endpoint, the equivalent measurement is
scripts/fleet/starvation_test.py --concurrency <agents>.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time

SCALE = 100  # 1 simulated second = 100 real seconds
STARVED_REAL_S = 300  # validator: first call delayed >300s = starved


class MockLLMServer:
    """FIFO queue in front of `capacity` generation slots."""

    def __init__(self, capacity: int, gen_time: float) -> None:
        self.q: queue.Queue = queue.Queue()
        self.gen_time = gen_time
        self.busy = 0
        self._lock = threading.Lock()
        for _ in range(capacity):
            threading.Thread(target=self._slot, daemon=True).start()

    def _slot(self) -> None:
        while True:
            req = self.q.get()
            with self._lock:
                self.busy += 1
            time.sleep(self.gen_time)  # the "generation" — slot is occupied
            req["done"].set()          # even if the client already gave up
            with self._lock:
                self.busy -= 1

    def submit(self) -> dict:
        req = {"done": threading.Event()}
        self.q.put(req)
        return req


class Agent(threading.Thread):
    """Step loop: request -> wait (with optional client timeout) -> repeat."""

    def __init__(self, name: str, server: MockLLMServer, deadline: float,
                 client_timeout: float | None) -> None:
        super().__init__(daemon=True)
        self.name = name
        self.server = server
        self.deadline = deadline
        self.client_timeout = client_timeout
        self.t0 = time.monotonic()
        self.first_ok: float | None = None
        self.completions = 0
        self.timeouts = 0

    def run(self) -> None:
        while time.monotonic() < self.deadline:
            req = self.server.submit()
            ok = req["done"].wait(
                timeout=min(self.client_timeout or 1e9,
                            max(0.0, self.deadline - time.monotonic())))
            if ok:
                if self.first_ok is None:
                    self.first_ok = time.monotonic() - self.t0
                self.completions += 1
            elif time.monotonic() < self.deadline:
                self.timeouts += 1  # tenacity-style retry: loop resubmits


def run_scenario(title: str, n_agents: int, capacity: int, gen_time: float,
                 client_timeout: float | None, window: float,
                 stagger: float = 0.0) -> None:
    real = lambda s: s * SCALE  # noqa: E731
    print(f"\n=== {title} ===")
    print(f"agents={n_agents} slots={capacity} generation={real(gen_time):.0f}s(real) "
          f"client_timeout={real(client_timeout):.0f}s(real)" if client_timeout
          else f"agents={n_agents} slots={capacity} generation={real(gen_time):.0f}s(real) "
               f"client_timeout=none")
    server = MockLLMServer(capacity, gen_time)
    deadline = time.monotonic() + window
    agents = []
    for i in range(n_agents):
        a = Agent(f"a{i+1:02d}", server, deadline, client_timeout)
        a.start()
        agents.append(a)
        if stagger:
            time.sleep(stagger)
    for a in agents:
        a.join()

    starved = [a for a in agents if a.first_ok is None
               or real(a.first_ok) > STARVED_REAL_S]
    delays = sorted(real(a.first_ok) for a in agents if a.first_ok is not None)
    total_completions = sum(a.completions for a in agents)
    total_timeouts = sum(a.timeouts for a in agents)
    if delays:
        print(f"first-response delay (real): min={delays[0]:.0f}s "
              f"median={delays[len(delays)//2]:.0f}s max={delays[-1]:.0f}s")
    never = sum(1 for a in agents if a.first_ok is None)
    if never:
        print(f"{never}/{n_agents} agents NEVER got a response in "
              f"{real(window):.0f}s(real)")
    print(f"completions={total_completions} client_timeouts/retries={total_timeouts}")
    if starved:
        print(f"VERDICT: {len(starved)}/{n_agents} agents STARVED "
              f"(first response later than {STARVED_REAL_S}s real, or never)")
    else:
        print("VERDICT: no starvation")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scenario", nargs="?", default="all",
                    choices=["all", "burst", "storm", "capped", "scaled"])
    args = ap.parse_args()

    # 8 slots, generations ~120s real, watched for ~10 simulated minutes real.
    if args.scenario in ("all", "burst"):
        run_scenario(
            "burst: 30 agents at once into 8 slots (the q27 launch surge)",
            n_agents=30, capacity=8, gen_time=1.2, client_timeout=None,
            window=6.0)
    if args.scenario in ("all", "storm"):
        run_scenario(
            "storm: client timeout (180s) < generation (240s) — retry storm",
            n_agents=12, capacity=8, gen_time=2.4, client_timeout=1.8,
            window=10.0)
    if args.scenario in ("all", "capped"):
        run_scenario(
            "capped: dispatch concurrency 3 cells (9 agents) into 8 slots",
            n_agents=9, capacity=8, gen_time=1.2, client_timeout=None,
            window=6.0)
    if args.scenario in ("all", "scaled"):
        run_scenario(
            "scaled: 30 agents at once, capacity already scaled to 32",
            n_agents=30, capacity=32, gen_time=1.2, client_timeout=None,
            window=6.0)


if __name__ == "__main__":
    sys.exit(main())
