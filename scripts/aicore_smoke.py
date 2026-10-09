#!/usr/bin/env python
"""One-call sanity check that cooperagents can reach a model via SAP AI Core.

Attaches through litellm's SAP Generative AI Hub provider (model prefix ``sap/``).
Credentials are loaded from a ``.env`` file in the project root (if present) and
then from the environment. python-dotenv handles quoting so values with special
characters (``!``, ``<``, etc.) work without shell escaping.

Example (zsh):
  uv run python scripts/aicore_smoke.py --model sap/anthropic--claude-3.5-sonnet
  uv run python scripts/aicore_smoke.py --model sap/gemini-2.5-flash
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_ROOT = Path(__file__).resolve().parents[1]
_DOTENV = _ROOT / ".env"
if _DOTENV.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_DOTENV, override=False)
    except ImportError:
        pass

from cooperagents.llm import LiteLLMClient  # noqa: E402

_INDIVIDUAL = ("AICORE_CLIENT_ID", "AICORE_CLIENT_SECRET", "AICORE_AUTH_URL", "AICORE_BASE_URL")


def main() -> int:
    ap = argparse.ArgumentParser(description="SAP AI Core attachment smoke test")
    ap.add_argument("--model", default=os.getenv("COOPER_MODELS", "sap/gemini-2.5-flash").split(",")[0])
    ap.add_argument("--prompt", default='Reply with ONLY this JSON: {"thought": "ok", "tool": "finish", "args": {}}')
    args = ap.parse_args()

    if not os.getenv("AICORE_SERVICE_KEY") and not all(os.getenv(v) for v in _INDIVIDUAL):
        print("No AI Core credentials found in the environment.")
        print("Set AICORE_SERVICE_KEY (the service-key JSON) or all of: " + ", ".join(_INDIVIDUAL))
        print("Optionally set AICORE_RESOURCE_GROUP (defaults to 'default').")
        return 2

    rg = os.getenv("AICORE_RESOURCE_GROUP", "default")
    print(f"Calling {args.model} via SAP AI Core (resource group: {rg}) ...")
    client = LiteLLMClient(args.model)
    try:
        action = client.decide(
            agent_id="smoke",
            role="lead",
            messages=[{"role": "user", "content": args.prompt}],
            tools=[{"name": "finish", "description": "stop working"}],
        )
    except Exception as e:  # noqa: BLE001 - report any attach/auth/model error plainly
        print(f"FAILED: {type(e).__name__}: {str(e)[:400]}")
        return 1

    print(f"OK -- tool={action.tool!r} thought={(action.thought or '')[:120]!r} cost={action.cost}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
