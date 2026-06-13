"""Coordination + spawn metrics derived from the bus audit logs.

Computed post-run from the same event streams agents wrote while working,
so no agent has to emit metrics itself.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def coordination_metrics(events: list[dict[str, Any]], *, final_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize task-list activity (creates/claims/updates + completion)."""
    creates = [e for e in events if e.get("kind") == "create"]
    claims = [e for e in events if e.get("kind") == "claim"]
    updates = [e for e in events if e.get("kind") == "update"]
    first_create = min((c.get("ts", 0.0) for c in creates), default=None)
    first_claim = min((c.get("ts", 0.0) for c in claims), default=None)
    time_to_first_claim = None
    if first_create is not None and first_claim is not None:
        time_to_first_claim = round(first_claim - first_create, 3)
    return {
        "tasks_total": len(final_tasks),
        "tasks_done": sum(1 for t in final_tasks if t.get("status") == "done"),
        "unowned_at_end": sum(1 for t in final_tasks if not t.get("owner")),
        "time_to_first_claim_seconds": time_to_first_claim,
        "claims_per_agent": dict(Counter(c.get("by", "?") for c in claims)),
        "updates_per_agent": dict(Counter(u.get("by", "?") for u in updates)),
    }


def spawn_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize how much the team grew itself at runtime."""
    requests = [e for e in events if e.get("kind") == "request"]
    outcomes = [e for e in events if e.get("kind") == "outcome"]
    return {
        "requests_total": len(requests),
        "granted": sum(1 for o in outcomes if o.get("outcome") == "granted"),
        "capped": sum(1 for o in outcomes if o.get("outcome") == "capped"),
        "requests_per_agent": dict(Counter(r.get("by", "?") for r in requests)),
    }


__all__ = ["coordination_metrics", "spawn_metrics"]
