import copy

import pytest

from cooperagents.published import LEGACY, V3, PublicationError
from cooperagents.published_profiles import CAPTURED, PRODUCERS, validate_capture, validate_producer


@pytest.mark.parametrize("fmt", [LEGACY, V3])
def test_known_producer_identity_and_unknown_source_rejected(fmt):
    r = copy.deepcopy(PRODUCERS[fmt])
    validate_producer(r, fmt)
    r["source_identity"]["harness.py"] = "0" * 64
    with pytest.raises(PublicationError):
        validate_producer(r, fmt)


@pytest.mark.parametrize("profile", list(CAPTURED))
def test_capture_profiles_require_exact_known_identity(profile):
    if profile == "captured-post-gate-v1":
        validate_capture(CAPTURED[profile], profile)
    else:
        with pytest.raises(PublicationError, match="not qualified"):
            validate_capture(CAPTURED[profile], profile)
    with pytest.raises(PublicationError):
        validate_capture({**CAPTURED[profile], "harness.py": "0" * 64}, profile)
    with pytest.raises(PublicationError):
        validate_capture({**CAPTURED[profile], "../secret": "a" * 64}, profile)
    with pytest.raises(PublicationError):
        validate_capture(CAPTURED[profile], "unknown")
