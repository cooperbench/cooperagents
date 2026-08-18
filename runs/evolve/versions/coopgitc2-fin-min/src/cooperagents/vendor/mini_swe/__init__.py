"""Vendored subset of mini-swe-agent v2 (tool-calling).

Source: https://github.com/SWE-agent/mini-swe-agent (v2.1.0, commit 56613dd)
License: MIT — Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez.

Copied (via CooperBench's vendored copy) and adapted for CooperAgents:
- import paths -> ``cooperagents.vendor.mini_swe``
- dropped the global ~/.config dotenv bootstrap (platformdirs/dotenv) and the
  messaging/modal/git connectors we don't use here
- kept the DefaultAgent loop, LitellmModel (tool-calling), and DockerEnvironment

This is the agent loop the CooperAgents unified harness runs as its worker
(see ``cooperagents.workers.mini_swe_worker``), so "team vs solo" is measured
with the same agent class CooperBench benchmarks.
"""

from __future__ import annotations

from typing import Any, Protocol

from cooperagents.vendor.mini_swe.utils.log import logger

__version__ = "2.1.0"


class Model(Protocol):
    config: Any

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict: ...
    def format_message(self, **kwargs) -> dict: ...
    def format_observation_messages(self, message: dict, outputs: list[dict], template_vars: dict | None = None) -> list[dict]: ...
    def get_template_vars(self, **kwargs) -> dict[str, Any]: ...
    def serialize(self) -> dict: ...


class Environment(Protocol):
    config: Any

    def execute(self, action: dict, cwd: str = "") -> dict[str, Any]: ...
    def get_template_vars(self, **kwargs) -> dict[str, Any]: ...
    def serialize(self) -> dict: ...


class Agent(Protocol):
    config: Any

    def run(self, task: str, **kwargs) -> dict: ...


__all__ = ["Agent", "Model", "Environment", "__version__", "logger"]
