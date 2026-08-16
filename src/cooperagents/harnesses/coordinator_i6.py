"""The current best-known configuration: coordinator arm with every
accepted mechanism through iteration 6."""

HARNESS = {
    "name": "coordinator-i6",
    "flags": {
        "arm": "coopgitc2",
        "step_limit": 1000,
        "repair": True,
        "agent_time_limit": 3600,
        "completion_gate": True,
        "env_brief": True,
    },
}
