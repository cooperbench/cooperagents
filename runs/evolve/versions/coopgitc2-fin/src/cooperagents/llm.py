"""LLM clients that drive the agent loop.

The agent loop is a thin tool-calling loop: at each step it hands the LLM
the running transcript and the tool catalog, and the LLM returns exactly
one :class:`Action`.  Decoupling "what to do next" behind this interface
means the same agent/harness code runs three ways:

  * :class:`ScriptedLLM` — a fixed action sequence.  Deterministic, needs
    no API key, and is what the whole test suite and the offline flash
    validation use.
  * :class:`CallbackLLM` — an arbitrary Python function as the policy
    (useful for heuristic agents or simulation).
  * :class:`LiteLLMClient` — a real model via litellm; emits a JSON action.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Action:
    """One decision from the policy: call a tool with arguments."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""
    cost: float = 0.0


def parse_actions(text: str, *, cost: float = 0.0) -> list[Action]:
    """Parse ALL JSON action objects out of a model reply, in order.

    Models (especially reasoning models) routinely emit several
    newline-separated action objects in one reply — a whole mini-plan like
    ``explore → write_file → run tests``.  The agent executes them in order
    within the turn; if it only ran the first, the model would assume the
    later writes happened and ``finish`` with an empty diff.  Cost is
    attributed to the first action only (it's per-call).  An unparseable
    reply yields a single ``finish`` so the agent always terminates.
    """
    actions: list[Action] = []
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        brace = text.find("{", idx)
        if brace == -1:
            break
        try:
            data, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        idx = end
        if isinstance(data, dict) and "tool" in data:
            actions.append(
                Action(
                    tool=str(data.get("tool", "finish")),
                    args=dict(data.get("args", {})),
                    thought=str(data.get("thought", "")),
                    cost=cost if not actions else 0.0,
                )
            )
    if not actions:
        return [Action(tool="finish", thought=text[:200], cost=cost)]
    return actions


def parse_action(text: str, *, cost: float = 0.0) -> Action:
    """Parse just the first JSON action (kept for callers that want one)."""
    return parse_actions(text, cost=cost)[0]


class LLMClient(Protocol):
    def decide(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Action:
        """Return the next action for ``agent_id`` given the transcript."""
        ...


class ScriptedLLM:
    """Replays a fixed list of actions, optionally per agent id/role.

    ``script`` maps a selector (an exact ``agent_id``, or a ``role`` like
    ``"lead"``, or ``"*"`` for the default) to a list of actions.  Each
    ``decide`` pops the next action for the matching selector; when a
    selector's list is exhausted it falls back to a ``finish`` action so
    agents terminate cleanly.
    """

    def __init__(self, script: dict[str, list[Action]]) -> None:
        self._script = {k: list(v) for k, v in script.items()}

    def _queue_for(self, agent_id: str, role: str) -> list[Action]:
        for key in (agent_id, role, "*"):
            if key in self._script:
                return self._script[key]
        return []

    def decide(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Action:
        queue = self._queue_for(agent_id, role)
        if queue:
            return queue.pop(0)
        return Action(tool="finish", thought="script exhausted")


class CallbackLLM:
    """Wraps a plain function ``(agent_id, role, messages, tools) -> Action``."""

    def __init__(self, fn: Callable[[str, str, list[dict[str, Any]], list[dict[str, Any]]], Action]) -> None:
        self._fn = fn

    def decide(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Action:
        return self._fn(agent_id, role, messages, tools)


class LiteLLMClient:
    """Real LLM policy via litellm.  Asks the model for a single JSON action.

    Lazy-imports litellm so the package works without it installed.  The
    model is instructed to reply with ``{"thought": ..., "tool": ...,
    "args": {...}}``; malformed replies degrade to a ``finish`` action.
    """

    def __init__(self, model: str, *, temperature: float = 0.0, max_tokens: int = 2048) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _system_prompt(self, tools: list[dict[str, Any]]) -> str:
        catalog = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
        return (
            "You are an agent on a software team. At each step respond with a SINGLE JSON object "
            'of the form {"thought": "...", "tool": "<name>", "args": {...}} and nothing else.\n'
            f"Available tools:\n{catalog}"
        )

    def _complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[str, float]:
        import litellm

        chat = [{"role": "system", "content": self._system_prompt(tools)}, *messages]
        resp = litellm.completion(model=self.model, messages=chat, temperature=self.temperature, max_tokens=self.max_tokens)
        text = resp["choices"][0]["message"]["content"] or ""
        try:
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:  # noqa: BLE001 - cost is best-effort
            cost = 0.0
        return text, cost

    def decide(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Action:
        text, cost = self._complete(messages, tools)
        return parse_action(text, cost=cost)

    def decide_batch(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[Action]:
        text, cost = self._complete(messages, tools)
        return parse_actions(text, cost=cost)


class OpenAIClient:
    """Policy via an OpenAI-compatible Chat Completions endpoint.

    Works with the OpenAI API and Azure's OpenAI-v1 compatibility endpoint
    (``…/openai/v1/``).  Reasoning models (e.g. GPT-5.x) are supported: by
    default we send only ``model`` + ``messages`` (no ``temperature`` /
    ``max_tokens``, which some reasoning deployments reject).  The model is
    asked for a single JSON action, same protocol as :class:`LiteLLMClient`.

    Token usage is accumulated on the instance; ``Action.cost`` is left at 0
    (we budget by ``step_limit``) unless ``price_per_1k`` rates are given.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_completion_tokens: int | None = None,
        price_per_1k: tuple[float, float] | None = None,
    ) -> None:
        from openai import OpenAI  # lazy

        self.model = model
        self.max_completion_tokens = max_completion_tokens
        self._price = price_per_1k
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def _system_prompt(self, tools: list[dict[str, Any]]) -> str:
        catalog = "\n".join(f"- {t['name']}: {t['description']}" for t in tools)
        return (
            "You are an autonomous software engineer working in a real git repo at the current "
            "directory. Implement the requested feature by editing files and verifying with the "
            'shell. Reply with one JSON object per action: {"thought": "...", "tool": "<name>", '
            '"args": {...}}. You MAY emit a short ordered sequence of such objects (one per line) '
            "to be executed in order this turn — but ALWAYS include the `write_file` actions that "
            "make your edits; do not just describe them. After each turn you see every result, then "
            "continue. Explore briefly (a few ls/cat/grep), then START EDITING — do not spend every "
            "turn exploring. Use `bash` to explore and build, and `write_file` to edit files. "
            "IMPORTANT RULES: (1) ACTUALLY WRITE THE CODE — an empty diff scores zero. (2) Do NOT "
            "create or modify ANY test files (e.g. *_test.go, test_*.py) — the grader supplies its "
            "own tests; your test files will COLLIDE with the grader and fail the run. (3) Before "
            "`finish`, make sure the project still builds/compiles (e.g. `go build ./...` or import "
            "the package) and fix any errors. Call `finish` only when the feature is implemented and "
            "the code builds.\n"
            f"Tools:\n{catalog}"
        )

    def _complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[str, float]:
        chat = [{"role": "system", "content": self._system_prompt(tools)}, *messages]
        kwargs: dict[str, Any] = {"model": self.model, "messages": chat}
        if self.max_completion_tokens is not None:
            kwargs["max_completion_tokens"] = self.max_completion_tokens
        resp = self._client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
        cost = 0.0
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            if self._price is not None:
                pin, pout = self._price
                cost = (getattr(usage, "prompt_tokens", 0) * pin + getattr(usage, "completion_tokens", 0) * pout) / 1000.0
        return text, cost

    def decide(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Action:
        text, cost = self._complete(messages, tools)
        return parse_action(text, cost=cost)

    def decide_batch(self, *, agent_id: str, role: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[Action]:
        text, cost = self._complete(messages, tools)
        return parse_actions(text, cost=cost)


__all__ = [
    "Action",
    "parse_action",
    "parse_actions",
    "LLMClient",
    "ScriptedLLM",
    "CallbackLLM",
    "LiteLLMClient",
    "OpenAIClient",
]
