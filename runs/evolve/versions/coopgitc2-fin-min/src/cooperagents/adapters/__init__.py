"""ProgramBench-only adapter registry (minimal extraction)."""
from cooperagents.adapters.base import BenchmarkAdapter


def get_adapter(name: str) -> BenchmarkAdapter:
    if name == "programbench":
        from cooperagents.adapters.programbench import ProgramBenchAdapter
        return ProgramBenchAdapter()
    raise KeyError(f"unknown benchmark adapter: {name!r}")
