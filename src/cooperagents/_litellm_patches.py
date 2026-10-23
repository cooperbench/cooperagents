"""Runtime patches for third-party litellm bugs.

SAP GenAI Hub + Gemini 2.5: when thinking is enabled, the orchestration
response carries ``reasoning_content`` as a LIST of thinking-block dicts
(``{"content": ..., "signature": ...}``), but litellm's ``Message`` model
types ``reasoning_content`` as ``Optional[str]``, so
``GenAIHubOrchestrationConfig.transform_response`` raises a pydantic
``ValidationError`` on essentially every call that returns multi-part
reasoning (observed with litellm 1.102.1 against ``sap/gemini-2.5-flash``).
The agent loop then retries the identical request forever and wedges.

We coerce any list-valued ``reasoning_content`` in the raw response to a
concatenated string before litellm validates it. The actual answer lives in
``message.content`` and is untouched; only the (redacted) thinking blocks are
flattened, so no behaviour is lost.
"""

from __future__ import annotations

import json
from typing import Any

_applied = False


def _coerce_reasoning(obj: Any) -> bool:
    """Recursively replace any list-valued ``reasoning_content`` with a string.

    Returns ``True`` if anything was changed.
    """
    changed = False
    if isinstance(obj, dict):
        rc = obj.get("reasoning_content")
        if isinstance(rc, list):
            parts = [str(it.get("content") or "") if isinstance(it, dict) else str(it) for it in rc]
            obj["reasoning_content"] = "".join(parts)
            changed = True
        for value in obj.values():
            changed = _coerce_reasoning(value) or changed
    elif isinstance(obj, list):
        for item in obj:
            changed = _coerce_reasoning(item) or changed
    return changed


def apply_sap_reasoning_patch() -> None:
    """Idempotently make the SAP ``transform_response`` tolerate list ``reasoning_content``."""
    global _applied
    if _applied:
        return
    try:
        from litellm.llms.sap.chat.transformation import GenAIHubOrchestrationConfig
    except Exception:  # noqa: BLE001 - SAP provider not installed; nothing to patch
        _applied = True
        return

    _orig = GenAIHubOrchestrationConfig.transform_response

    def _patched(self: Any, *args: Any, **kwargs: Any) -> Any:
        raw_response = kwargs.get("raw_response")
        if raw_response is None and len(args) >= 2:
            raw_response = args[1]  # (model, raw_response, model_response, ...)
        if raw_response is not None:
            try:
                payload = raw_response.json()
            except Exception:  # noqa: BLE001 - leave untouched if the body is not JSON yet
                payload = None
            if isinstance(payload, dict) and _coerce_reasoning(payload):
                # httpx.Response.json() re-parses self.content (backed by _content);
                # overwriting it makes the original method see the sanitized payload.
                raw_response._content = json.dumps(payload).encode("utf-8")
        return _orig(self, *args, **kwargs)

    GenAIHubOrchestrationConfig.transform_response = _patched  # type: ignore[method-assign]
    _applied = True


__all__ = ["apply_sap_reasoning_patch"]
