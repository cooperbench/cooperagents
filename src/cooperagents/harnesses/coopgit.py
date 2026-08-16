"""E0 seed: the simplest team harness — 2 agents, same task, git-based
sharing, mechanical merge. No repair, no coordinator, no gates."""

HARNESS = {
    "name": "coopgit",
    "flags": {
        "arm": "coopgit",
        "step_limit": 1000,
    },
}
