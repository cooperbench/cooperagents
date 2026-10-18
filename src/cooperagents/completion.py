"""Optional full-response transport for integrators; ordinary workers keep their model."""

from __future__ import annotations

import math
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, cast

from litellm import ModelResponse
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator


def safe_generation(kwargs: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Persist supported generation fields; transport credentials never enter snapshots."""
    ranges = {"temperature": (0, 2), "top_p": (0, 1), "presence_penalty": (-2, 2), "max_tokens": (1, math.inf), "timeout": (1, math.inf)}
    allowed = set(ranges) | {"drop_params", "extra_body"}
    if set(kwargs) - allowed:
        raise ValueError(f"Unsupported repair generation fields: {sorted(set(kwargs) - allowed)}")
    for key, bounds in ranges.items():
        if key in kwargs and (
            type(kwargs[key]) not in (int, float) or not math.isfinite(kwargs[key]) or not bounds[0] <= kwargs[key] <= bounds[1]
        ):
            raise ValueError(f"Invalid repair generation setting: {key}")
    if "max_tokens" in kwargs and type(kwargs["max_tokens"]) is not int:
        raise ValueError("max_tokens must be an integer")
    if "drop_params" in kwargs and type(kwargs["drop_params"]) is not bool:
        raise ValueError("drop_params must be boolean")
    extra = kwargs.get("extra_body", {})
    if not isinstance(extra, dict) or set(extra) - {
        "top_k",
        "min_p",
        "repetition_penalty",
        "chat_template_kwargs",
        "reasoning",
        "provider",
    }:
        raise ValueError("Unsupported repair extra_body")
    for key, bounds in {"top_k": (1, math.inf), "min_p": (0, 1), "repetition_penalty": (0, 2)}.items():
        if key in extra and (
            type(extra[key]) not in (int, float) or not math.isfinite(extra[key]) or not bounds[0] <= extra[key] <= bounds[1]
        ):
            raise ValueError(f"Invalid repair extra_body setting: {key}")
    if "top_k" in extra and type(extra["top_k"]) is not int:
        raise ValueError("top_k must be an integer")
    for key, field in (("chat_template_kwargs", "enable_thinking"), ("reasoning", "enabled")):
        if key in extra and (not isinstance(extra[key], dict) or set(extra[key]) != {field} or type(extra[key][field]) is not bool):
            raise ValueError(f"Invalid repair {key}")
    if "provider" in extra:
        provider = extra["provider"]
        if not isinstance(provider, dict) or set(provider) - {"require_parameters", "only", "allow_fallbacks"}:
            raise ValueError("Invalid repair provider settings")
        for key in ("require_parameters", "allow_fallbacks"):
            if key in provider and type(provider[key]) is not bool:
                raise ValueError("Invalid provider boolean")
        if "only" in provider and (
            not isinstance(provider["only"], list)
            or not provider["only"]
            or any(not isinstance(v, str) or not v or any(c.isspace() for c in v) for v in provider["only"])
        ):
            raise ValueError("Invalid provider selection")
    return kwargs


class CompletionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    generation: dict[str, JsonValue]
    timeout: float = Field(gt=0, le=240)

    @model_validator(mode="after")
    def validate_generation(self) -> CompletionSettings:
        safe_generation(self.generation)
        if "timeout" in self.generation or "drop_params" in self.generation:
            raise ValueError("transport timeout is separate; injected settings cannot drop parameters")
        if "max_tokens" not in self.generation:
            raise ValueError("injected completion requires an explicit token budget")
        return self


@dataclass(frozen=True)
class MiniSweCompletionRequest:
    messages: list[ChatCompletionMessageParam]
    tools: list[ChatCompletionToolParam] | None
    settings: CompletionSettings
    actor_id: str
    call_id: int
    purpose: Literal["action", "summary"]

    @property
    def tool_choice(self) -> Literal["auto"] | None:
        return "auto" if self.tools else None

    @property
    def attempt(self) -> int:
        suffix = self.actor_id.removeprefix("integrator")
        return int(suffix) if suffix in ("1", "2") else 1

    @classmethod
    def build(
        cls,
        *,
        messages: object,
        tools: object,
        settings: CompletionSettings,
        actor_id: str,
        call_id: int,
        purpose: Literal["action", "summary"],
    ) -> MiniSweCompletionRequest:
        if not isinstance(messages, list) or not messages or call_id < 1 or not actor_id:
            raise ValueError("invalid completion identity/history")
        # Validate SDK chat shapes without normalizing away provider fields. dump_json
        # consumes lazy Iterable validators as well (tool_calls is an SDK Iterable).
        adapter = TypeAdapter(list[ChatCompletionMessageParam])
        adapter.dump_json(adapter.validate_python(messages))
        if tools is not None:
            tools_adapter = TypeAdapter(list[ChatCompletionToolParam])
            tools_adapter.dump_json(tools_adapter.validate_python(tools))
        return cls(
            cast(list[ChatCompletionMessageParam], deepcopy(messages)),
            cast(list[ChatCompletionToolParam] | None, deepcopy(tools)),
            settings,
            actor_id,
            call_id,
            purpose,
        )


Completion = Callable[[MiniSweCompletionRequest], ModelResponse]


@dataclass(frozen=True)
class CompletionBinding:
    action: Completion
    action_settings: CompletionSettings
    summary: Completion
    summary_settings: CompletionSettings

    def complete(self, *, messages: object, tools: object, actor_id: str, call_id: int) -> ModelResponse:
        summary = tools is None
        request = MiniSweCompletionRequest.build(
            messages=messages,
            tools=tools,
            settings=self.summary_settings if summary else self.action_settings,
            actor_id=actor_id,
            call_id=call_id,
            purpose="summary" if summary else "action",
        )
        response = (self.summary if summary else self.action)(request)
        if not isinstance(response, ModelResponse) or not response.choices:
            raise ValueError("completion must return a complete ModelResponse")
        return response
