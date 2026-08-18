"""ProgramBench-only adapter registry (minimal extraction)."""
from cooperagents.adapters.base import BenchmarkAdapter


def get_adapter(name: str) -> BenchmarkAdapter:
    if name == "programbench":
        from cooperagents.adapters.programbench import ProgramBenchAdapter
        return ProgramBenchAdapter()
    if name == "cooperbench":
        from cooperagents.adapters.cooperbench import CooperBenchAdapter
        return CooperBenchAdapter()
    raise KeyError(f"unknown benchmark adapter: {name!r}")
