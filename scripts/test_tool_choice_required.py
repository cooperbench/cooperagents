"""Validation test: does tool_choice="required" eliminate no-tool-call responses?

Method: REPLAY real offending contexts. From the exported trajectories, find
cases where the model responded with prose/echo instead of a tool call (the
response that triggered "No tool calls found..."). Rebuild the exact message
prefix up to that point, resend it to the live endpoint twice:
  A) baseline    — exactly as production sends it (tools, temperature 0)
  B) required    — same, plus tool_choice="required"
Score each arm: did the response contain >=1 tool call?

Expected if the flag works: arm A reproduces the failure on a good fraction
of contexts (temperature 0 => the echo should be sticky); arm B produces a
tool call on 100% of contexts.

Usage:  set -a; source .env.qwen; set +a
        .venv/bin/python scripts/test_tool_choice_required.py [n_cases]
"""

from __future__ import annotations

import glob
import json
import os
import random
import sys

sys.path.insert(0, "src")
import litellm  # noqa: E402

from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import BASH_TOOL  # noqa: E402

MODEL = "openai/" + os.environ.get("AZURE_OPENAI_DEPLOYMENT", "Qwen/Qwen3.5-9B")
KW = dict(
    api_base=os.environ.get("OPENAI_BASE_URL"),
    api_key=os.environ.get("OPENAI_API_KEY", "dummy"),
    temperature=0.0,
    max_tokens=1200,
    tools=[BASH_TOOL],
    drop_params=True,
)

API_FIELDS = ("role", "content", "tool_calls", "tool_call_id", "name")


def to_api(m: dict) -> dict:
    out = {k: m[k] for k in API_FIELDS if m.get(k) is not None}
    out.setdefault("content", "")
    return out


def find_cases(n: int) -> list[tuple[str, str, list[dict]]]:
    """(run, agent, message-prefix) ending just BEFORE an offending response."""
    cases = []
    files = sorted(glob.glob("runs/hf_export/runs/*.json"))
    random.Random(7).shuffle(files)
    for f in files:
        d = json.load(open(f))
        for aid, a in d["agents"].items():
            ms = a["messages"]
            for i, m in enumerate(ms):
                c = m.get("content")
                # the offending response is DROPPED by the parser, so the
                # error message directly follows the observation that provoked
                # it: replaying ms[:i] reproduces the exact provoking state.
                if isinstance(c, str) and "No tool calls found in the response" in c and i >= 3:
                    prefix = [to_api(x) for x in ms[:i]]
                    # context must fit the 32k window with output margin
                    if 4_000 < sum(len(str(x.get("content"))) for x in prefix) < 100_000:
                        cases.append((d["meta"]["run"], aid, prefix))
                    break  # at most one case per agent
        if len(cases) >= n:
            break
    return cases[:n]


def has_tool_call(resp) -> tuple[bool, str]:
    msg = resp.choices[0].message
    tcs = getattr(msg, "tool_calls", None) or []
    head = ""
    if tcs:
        head = str(tcs[0].function.arguments)[:90]
    else:
        head = str(msg.content)[:90]
    return bool(tcs), head


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    cases = find_cases(n)
    print(f"replaying {len(cases)} real offending contexts\n")
    results = {"baseline": 0, "required": 0}
    for run, aid, prefix in cases:
        row = f"{run.replace('pb-', '')}/{aid}"
        try:
            a = litellm.completion(model=MODEL, messages=prefix, **KW)
            ok_a, head_a = has_tool_call(a)
        except Exception as e:  # noqa: BLE001
            ok_a, head_a = None, f"ERR {type(e).__name__}: {str(e)[:60]}"
        try:
            b = litellm.completion(model=MODEL, messages=prefix,
                                   tool_choice="required", **KW)
            ok_b, head_b = has_tool_call(b)
        except Exception as e:  # noqa: BLE001
            ok_b, head_b = None, f"ERR {type(e).__name__}: {str(e)[:60]}"
        results["baseline"] += bool(ok_a)
        results["required"] += bool(ok_b)
        print(f"{row:52s} baseline={'TOOL' if ok_a else ('err' if ok_a is None else 'NO-TOOL')}  "
              f"required={'TOOL' if ok_b else ('err' if ok_b is None else 'NO-TOOL')}")
        if not ok_a:
            print(f"    baseline output: {head_a!r}")
        if ok_b:
            print(f"    required call:   {head_b!r}")
    nn = len(cases)
    print(f"\nbaseline tool-call rate: {results['baseline']}/{nn}   "
          f"with tool_choice=required: {results['required']}/{nn}")
    if results["required"] == nn and results["baseline"] < nn:
        print("VERDICT: flag eliminates the failure class on replayed contexts")
    elif results["required"] == nn:
        print("VERDICT: flag safe (100% tool calls) but baseline did not reproduce — weak evidence")
    else:
        print("VERDICT: flag does NOT fully eliminate the failure — investigate")


if __name__ == "__main__":
    main()
