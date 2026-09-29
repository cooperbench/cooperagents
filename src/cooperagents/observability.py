"""Opt-in Langfuse export of the existing worker/coordinator I/O events."""

from __future__ import annotations

import json
import logging
import os
import threading

from langfuse import Langfuse, propagate_attributes
from opentelemetry.sdk.trace import TracerProvider

from cooperagents.trajectory import _json_default

logger = logging.getLogger(__name__)


class LangfuseTrace:
    """Per-run roots and explicit call parents, safe across completion threads.

    A local journal, if supplied, keeps its own error semantics and call IDs.
    Export failures are best-effort; model calls are never retried by tracing.
    """

    def __init__(self, spec, journal=None, *, client=None):
        if client is None:
            if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
                raise ValueError("Langfuse requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
            client = Langfuse(
                tracer_provider=TracerProvider(),
                base_url=(
                    os.getenv("LANGFUSE_BASE_URL")
                    or os.getenv("LANGFUSE_OTEL_HOST")
                    or os.getenv("LANGFUSE_HOST")
                    or "https://us.cloud.langfuse.com"
                ),
            )
        self.client = client
        self.journal = journal
        self.session_id = Langfuse.create_trace_id(seed=json.dumps(["cooperagents", "session", spec.run_id]))
        self.metadata = dict(run_id=spec.run_id, repo=spec.repo, task_id=spec.task_id)
        self._roots = {}
        self._calls = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self._secrets = [
            v for k, v in os.environ.items() if any(s in k.upper() for s in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")) and len(v) >= 8
        ]

    def _clean(self, value):
        if isinstance(value, dict):
            return {
                k: "[REDACTED]"
                if k.lower() in {"api_key", "authorization", "x-api-key", "cookie", "password", "secret"}
                else self._clean(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
        return value

    def emit(self, actor: str, event: str, **data) -> int:
        with self._lock:
            self._seq += 1
            seq = self.journal.emit(actor, event, **data) if self.journal else self._seq
            if self._closed:
                return seq
            try:
                with propagate_attributes(session_id=self.session_id, tags=["cooperagents"], trace_name=actor):
                    self._emit(actor, event, seq, self._clean(json.loads(json.dumps(data, default=_json_default))))
            except Exception as exc:
                logger.warning("Langfuse event export failed (%s)", type(exc).__name__)
            return seq

    def _emit(self, actor: str, event: str, seq: int, data: dict) -> None:
        root = self._roots.get(actor)
        if root is None:
            root = self.client.start_observation(
                name=actor,
                as_type="agent",
                metadata={**self.metadata, "actor": actor},
                trace_context={"trace_id": Langfuse.create_trace_id(seed=json.dumps([self.session_id, actor]))},
            )
            self._roots[actor] = root
        if event == "request":
            request = data["request"]
            generation = "messages" in request and "model" in request
            # Explicit parent objects survive both worker and per-completion threads.
            self._calls[seq] = root.start_observation(
                name="completion" if generation else "execute",
                as_type="generation" if generation else "tool",
                input={k: request[k] for k in ("messages", "tools", "command", "timeout") if k in request},
                **(
                    {
                        "model": request["model"],
                        "model_parameters": {
                            k: request[k]
                            for k in ("temperature", "top_p", "max_tokens", "max_completion_tokens", "tool_choice")
                            if k in request
                        },
                    }
                    if generation
                    else {}
                ),
            )
        elif event in ("response", "error"):
            span = self._calls.pop(data["call_id"], None)
            if span is None:
                return
            try:
                if event == "error":
                    span.update(level="ERROR", status_message=data["error_type"], output=data["error"])
                else:
                    response = data["response"]
                    fields = {"output": response}
                    if isinstance(response, dict) and response.get("usage"):
                        usage = response["usage"]
                        fields["usage_details"] = {
                            target: usage[source]
                            for source, target in (("prompt_tokens", "input"), ("completion_tokens", "output"), ("total_tokens", "total"))
                            if isinstance(usage.get(source), int)
                        }
                    span.update(**fields)
            finally:
                span.end()
        elif event in ("agent_start", "observation"):
            root.update(input=data)
        elif event == "agent_end":
            root.update(output=data, **({"level": "ERROR", "status_message": "Worker failed"} if data.get("status") == "error" else {}))
        elif event not in ("context", "messages"):
            root.start_observation(name=event, as_type="span", input=data).end()
            if event == "decision":
                root.update(output=data)

    def close(self, *, failed: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for span in [*self._calls.values(), *self._roots.values()]:
                try:
                    if failed or span in self._calls.values():
                        span.update(level="ERROR", status_message="Run interrupted or call incomplete")
                    span.end()
                except Exception as exc:
                    logger.warning("Langfuse span close failed (%s)", type(exc).__name__)
            self._calls.clear()
        try:
            self.client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush failed (%s)", type(exc).__name__)
