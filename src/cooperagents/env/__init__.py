"""Execution environments for the unified harness."""

from __future__ import annotations

from cooperagents.env.base import Environment, ExecResult
from cooperagents.env.local import LocalEnv

__all__ = ["Environment", "ExecResult", "LocalEnv"]
