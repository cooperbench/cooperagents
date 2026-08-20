"""Benchmark adapters: the harness is benchmark-agnostic; each benchmark
answers six questions (environment, task, gates, fitness, repair,
submission) through one BenchmarkAdapter. See base.py for the contract.
"""

from cooperagents.adapters.base import BenchmarkAdapter


def get_adapter(name: str) -> BenchmarkAdapter:
    """Adapter registry (imports are lazy so one benchmark's deps do not
    burden another's runs)."""
    if name == "programbench":
        from cooperagents.adapters.programbench import ProgramBenchAdapter
        return ProgramBenchAdapter()
    if name == "terminalbench":
        from cooperagents.adapters.terminalbench import TerminalBenchAdapter
        return TerminalBenchAdapter()
    if name == "cooperbench":
        from cooperagents.adapters.cooperbench import CooperBenchAdapter
        return CooperBenchAdapter()
    raise KeyError(f"unknown benchmark adapter: {name!r}")


__all__ = ["BenchmarkAdapter", "get_adapter"]
