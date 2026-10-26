"""Public failures for a planning-only interaction."""


class PlanningInfrastructureError(RuntimeError):
    """The service or environment failed; no model reward can be inferred."""

    category = "infrastructure"


class PlanningProtocolError(ValueError):
    """The model response cannot complete a planning episode."""

    category = "model_protocol"


class PlanningCleanupError(RuntimeError):
    """Owned planning resources could not be closed; the training run must stop."""

    category = "cleanup"
