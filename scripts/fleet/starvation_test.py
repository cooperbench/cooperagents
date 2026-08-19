#!/usr/bin/env python3
"""Deployment starvation test: does the serving endpoint admit a burst of
N concurrent clients without starving any of them?

Starvation (matches the post-hoc run validator): a request waits >300s
before its first token. This test measures it directly against the live
deployment BEFORE a batch is dispatched, at the batch's real concurrency.

Phases:
  1. baseline   - 1 stream: TTFT + single-stream tok/s (also JIT warm-up).
  2. burst      - N simultaneous streams: per-stream TTFT (admission).
  3. sustained  - N simultaneous longer generations: per-stream rate
                  fairness + mid-stream stall detection.
  While phases 2-3 run, /metrics is sampled for queue depth if reachable.

Verdict:
  FAIL - any TTFT > --ttft-hard (default 300s), or a stream receives no
         token for > --stall-gap seconds mid-generation, or a request errors.
  WARN - p95 TTFT > --ttft-soft (default 60s), or min/median per-stream
         rate < 0.3 in the sustained phase.
  PASS - otherwise.
Exit codes: 0 PASS, 1 WARN, 2 FAIL (drivers should abort dispatch on 2).

Usage (preflight for a 10-cell x 3-agent batch):
  set -a; source .env.qwen38; set +a
  .venv/bin/python scripts/fleet/starvation_test.py --concurrency 30
Do NOT run at full concurrency while a live batch is using the endpoint;
the test load is real load.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.request

RESULTS: list[dict] = []
QUEUE_SAMPLES: list[float] = []


def _base_url() -> str:
    return (os.getenv("OPENAI_BASE_URL") or "http://localhost:8000/v1").rstrip("/")


def _api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


def _model() -> str:
    return os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("COOPER_MODEL") or "qwen3.8-27b"


def _stream_one(idx: int, phase: str, max_tokens: int, stall_gap: float, prompt: str,
                _template_kwargs: bool = True) -> dict:
    """One streaming completion; returns timing facts. Never raises."""
    url = _base_url() + "/chat/completions"
    payload = {
        "model": _model(),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    if _template_kwargs:
        # thinking off where supported; some deployments 400 on unknown kwargs
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    })
    t0 = time.monotonic()
    ttft = None
    last_tok = t0
    max_gap = 0.0
    n_chunks = 0
    err = None
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            for raw in resp:
                now = time.monotonic()
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                try:
                    delta = json.loads(line[5:])["choices"][0]["delta"]
                except Exception:  # noqa: BLE001
                    continue
                if delta.get("content") or delta.get("reasoning"):
                    if ttft is None:
                        ttft = now - t0
                    max_gap = max(max_gap, now - last_tok)
                    last_tok = now
                    n_chunks += 1
    except Exception as exc:  # noqa: BLE001
        if _template_kwargs and "400" in str(exc):
            return _stream_one(idx, phase, max_tokens, stall_gap, prompt,
                               _template_kwargs=False)
        err = f"{type(exc).__name__}: {exc}"[:200]
    dur = time.monotonic() - t0
    return {
        "phase": phase, "idx": idx, "ttft_s": ttft, "duration_s": round(dur, 2),
        "chunks": n_chunks, "rate_cps": round(n_chunks / dur, 2) if dur > 0 else 0.0,
        "max_gap_s": round(max_gap, 2), "stalled": max_gap > stall_gap,
        "error": err,
    }


def _sample_queue(stop: threading.Event) -> None:
    url = _base_url().rsplit("/v1", 1)[0] + "/metrics"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_api_key()}"})
    while not stop.wait(10):
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                for line in resp.read().decode().splitlines():
                    if line.startswith("vllm:num_requests_waiting{"):
                        QUEUE_SAMPLES.append(float(line.rsplit(" ", 1)[1]))
        except Exception:  # noqa: BLE001
            pass  # metrics behind a per-connection LB are best-effort


def _run_phase(phase: str, n: int, max_tokens: int, stall_gap: float, prompt: str) -> list[dict]:
    out: list[dict] = [None] * n  # type: ignore[list-item]
    def work(i: int) -> None:
        out[i] = _stream_one(i, phase, max_tokens, stall_gap, prompt)
    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [r for r in out if r]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--concurrency", type=int, default=30,
                    help="burst size = planned concurrent agents (cells x team size)")
    ap.add_argument("--burst-tokens", type=int, default=256)
    ap.add_argument("--sustained-tokens", type=int, default=2048)
    ap.add_argument("--ttft-hard", type=float, default=300.0,
                    help="FAIL bound: matches the run validator's starvation threshold")
    ap.add_argument("--ttft-soft", type=float, default=60.0)
    ap.add_argument("--stall-gap", type=float, default=120.0)
    ap.add_argument("--skip-sustained", action="store_true")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    print(f"target={_base_url()} model={_model()} concurrency={args.concurrency}")

    base = _run_phase("baseline", 1, args.burst_tokens, args.stall_gap, "Count from 1 to 50.")
    RESULTS.extend(base)
    b = base[0]
    if b["error"] or b["ttft_s"] is None:
        print(f"FAIL baseline request unsuccessful: {b['error']}")
        return 2
    print(f"baseline: ttft={b['ttft_s']:.2f}s rate={b['rate_cps']}chunk/s")

    stop = threading.Event()
    sampler = threading.Thread(target=_sample_queue, args=(stop,), daemon=True)
    sampler.start()

    burst = _run_phase("burst", args.concurrency, args.burst_tokens, args.stall_gap,
                       "Explain what a mutex is in about 100 words.")
    RESULTS.extend(burst)

    sustained: list[dict] = []
    if not args.skip_sustained:
        sustained = _run_phase("sustained", args.concurrency, args.sustained_tokens,
                               args.stall_gap, "Write a detailed design doc for a rate limiter.")
        RESULTS.extend(sustained)
    stop.set()

    failures: list[str] = []
    warnings: list[str] = []
    for phase, rows in (("burst", burst), ("sustained", sustained)):
        if not rows:
            continue
        errs = [r for r in rows if r["error"]]
        no_tok = [r for r in rows if r["ttft_s"] is None and not r["error"]]
        ttfts = sorted(r["ttft_s"] for r in rows if r["ttft_s"] is not None)
        stalls = [r for r in rows if r["stalled"]]
        if errs:
            failures.append(f"{phase}: {len(errs)}/{len(rows)} requests errored "
                            f"(first: {errs[0]['error']})")
        if no_tok:
            failures.append(f"{phase}: {len(no_tok)} requests produced no token")
        if ttfts:
            p95 = ttfts[min(len(ttfts) - 1, int(0.95 * len(ttfts)))]
            print(f"{phase}: n={len(rows)} ttft p50={statistics.median(ttfts):.1f}s "
                  f"p95={p95:.1f}s max={ttfts[-1]:.1f}s stalls={len(stalls)}")
            if ttfts[-1] > args.ttft_hard:
                failures.append(f"{phase}: max TTFT {ttfts[-1]:.0f}s > {args.ttft_hard:.0f}s "
                                f"(STARVED by the run-validator definition)")
            elif p95 > args.ttft_soft:
                warnings.append(f"{phase}: p95 TTFT {p95:.0f}s > {args.ttft_soft:.0f}s")
        if stalls:
            failures.append(f"{phase}: {len(stalls)} streams had a mid-generation gap "
                            f"> {args.stall_gap:.0f}s")
    if sustained:
        rates = sorted(r["rate_cps"] for r in sustained if r["rate_cps"] > 0)
        if rates:
            fairness = rates[0] / statistics.median(rates) if statistics.median(rates) else 0
            print(f"sustained fairness: min/median stream rate = {fairness:.2f}")
            if fairness < 0.3:
                warnings.append(f"sustained: slowest stream gets {fairness:.0%} of median rate")
    if QUEUE_SAMPLES:
        print(f"server queue depth: max={max(QUEUE_SAMPLES):.0f} "
              f"mean={statistics.mean(QUEUE_SAMPLES):.1f} (n={len(QUEUE_SAMPLES)} samples)")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"results": RESULTS, "queue_samples": QUEUE_SAMPLES,
                       "failures": failures, "warnings": warnings}, fh, indent=1)

    for w in warnings:
        print(f"WARN {w}")
    for f in failures:
        print(f"FAIL {f}")
    if failures:
        print("VERDICT: FAIL — deployment starves at this concurrency; do not dispatch")
        return 2
    if warnings:
        print("VERDICT: WARN — degraded but within the starvation bound")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
