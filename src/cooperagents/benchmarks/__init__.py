"""cooperagents non-code benchmarks (state substrate).

``get_benchmark(name)`` mirrors ``adapters.get_adapter``: a lazy registry so a
benchmark's optional deps (plancraft, a WorkBench checkout, ...) only load when
that benchmark is requested.
"""

from __future__ import annotations

from cooperagents.benchmarks.base import StateBenchmark


def get_benchmark(name: str) -> StateBenchmark:
    if name == "plancraft":
        from cooperagents.benchmarks.plancraft import PlanCraftBenchmark

        return PlanCraftBenchmark()
    if name == "workbench":
        from cooperagents.benchmarks.workbench import WorkBenchBenchmark

        return WorkBenchBenchmark()
    if name == "browsecomp":
        from cooperagents.benchmarks.browsecomp import BrowseCompBenchmark

        return BrowseCompBenchmark()
    if name == "finance":
        from cooperagents.benchmarks.finance import FinanceAgentBenchmark

        return FinanceAgentBenchmark()
    raise KeyError(f"unknown benchmark: {name!r}")


__all__ = ["StateBenchmark", "get_benchmark"]
