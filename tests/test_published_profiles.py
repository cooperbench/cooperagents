import copy
import shutil
from pathlib import Path

import pytest

from cooperagents.published import LEGACY, V3, PublicationError
from cooperagents.published_profiles import (
    CAPTURED,
    PRODUCERS,
    direct_identities,
    validate_capture,
    validate_direct_pair,
    validate_producer,
    validate_reader,
)


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


@pytest.fixture
def archived_lstat_capture():
    original = next(
        entry
        for entry in direct_identities()
        if entry["producer_identity"]["published.py"] == "ed4ccf8a90df4cb650937cab89265828ef094a78a7c3e0178a671668da54d414"
        and entry["producer_identity"]["env/published_capture.py"] == "b59501362fc19a3238eeb8721d1adb42a899b7378f812fb07afe2d88d271687d"
    )
    receipt = {"producer": "direct-capture-v1", "producer_identity": copy.deepcopy(original["producer_identity"])}
    receipt["producer_identity"]["env/published_capture.py"] = "870d76ef99e51eb7f7f377fd729ae6350342d00bd281189cc9e44b62cb0668d2"
    return receipt, dict(original["capture_sources"])


def test_archived_lstat_capture_is_admitted(archived_lstat_capture):
    receipt, captured = archived_lstat_capture
    validate_producer(receipt, V3)
    validate_direct_pair(receipt, captured)
    validate_capture(captured, "captured-direct-v3-v1")


@pytest.mark.parametrize("source", ["env/published_capture.py", "harness.py"])
def test_archived_capture_rejects_unknown_producer_sources(archived_lstat_capture, source):
    receipt, _ = archived_lstat_capture
    receipt["producer_identity"][source] = "0" * 64
    with pytest.raises(PublicationError, match="unknown direct capture producer"):
        validate_producer(receipt, V3)


def test_archived_capture_rejects_changed_execution_sources(archived_lstat_capture):
    receipt, captured = archived_lstat_capture
    captured["harness.py"] = "0" * 64
    with pytest.raises(PublicationError, match="not reviewed"):
        validate_direct_pair(receipt, captured)


def test_archived_direct_capture_requires_v3(archived_lstat_capture):
    receipt, _ = archived_lstat_capture
    with pytest.raises(PublicationError, match="unknown direct capture producer"):
        validate_producer(receipt, LEGACY)


def test_producer_allowlist_is_bound_to_reader_qualification(tmp_path, monkeypatch):
    import cooperagents

    root = tmp_path / "cooperagents"
    shutil.copytree(Path(cooperagents.__file__).parent, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(cooperagents, "__file__", str(root / "__init__.py"))
    validate_reader()
    allowlist = root / "direct_capture_qualification.json"
    allowlist.write_bytes(allowlist.read_bytes() + b"\n")
    with pytest.raises(PublicationError, match="unqualified repair runtime source"):
        validate_reader()


def test_image_reader_accepts_current_and_exact_reviewed_offline_builder():
    from cooperagents.published_profiles import qualification_identity, validate_image_reader

    validate_reader()
    validate_image_reader(qualification_identity())
    validate_image_reader("90db555845889be5ba02e7d17b198b48c2b0b7e605aaf3b3c77d50af0a9f1be7")
    for identity in (None, "0" * 64, "91794ac63843f091c24bb8601c0d0fbbcd452496c8185388fee3db3d5df86720"):
        with pytest.raises(ValueError, match="unqualified image reader"):
            validate_image_reader(identity)
