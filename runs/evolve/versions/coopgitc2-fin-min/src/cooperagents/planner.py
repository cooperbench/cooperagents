"""Minimal planner: only the LLM-completion helper the coordinator uses."""

from __future__ import annotations

import os
from collections.abc import Callable


def _default_planner_complete(model, base_url, api_key) -> Callable[[str], str] | None:
    try:
        from openai import OpenAI

        m = str(model or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.5-hao"))
        b = str(base_url or os.getenv("AZURE_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "")
        k = str(api_key or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
        client = OpenAI(base_url=b, api_key=k)

        def complete(prompt: str) -> str:
            resp = client.chat.completions.create(model=m, messages=[{"role": "user", "content": prompt}], max_completion_tokens=2000)
            from cooperagents.vendor.mini_swe.models.litellm_model import _strip_think

            return _strip_think(resp.choices[0].message.content or "")

        return complete
    except Exception:  # noqa: BLE001 - no creds / no lib
        return None


