"""Public checkpoint references are authenticated data, never executable loaders."""

import hashlib
import json
from pathlib import Path

import pytest

from cooperagents.published import PublicationError, PublishedCheckpoint, load_publications


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def row(tmp_path, version="v3"):
    return dict(
        id="repo_task-1-f1_f2-sample01",
        node="node.example",
        version=version,
        format={"legacy": "repair-overlay-filesystem-candidate-v1", "v3": "repair-overlay-v3"}[version],
        publication=dict(path=str(tmp_path / "publication"), key="a" * 64, sha256="b" * 64),
        legacy_manifest_sha256="c" * 64,
        image=str(tmp_path / "image.sif"),
        image_sha256="d" * 64,
        training_eligible=False,
        repair_validated=False,
    )


def catalog(tmp_path, entries):
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(dict(supported_versions=["legacy", "v3"], checkpoints=entries)))
    return p


def test_load_mixed_catalog_preserves_identity_and_does_not_read_payload(tmp_path):
    a = row(tmp_path, "legacy")
    b = {**row(tmp_path), "id": "other"}
    p = catalog(tmp_path, [a, b])
    items = load_publications(p, expected_sha256=digest(p))
    assert [x.id for x in items] == [a["id"], "other"]
    assert items[0].version == "legacy" and items[1].version == "v3"
    assert items[1].manifest_sha256 == "c" * 64
    assert not Path(a["publication"]["path"]).exists()


def test_catalog_wrong_identity_duplicate_id_or_retired_format_refused(tmp_path):
    p = catalog(tmp_path, [row(tmp_path)])
    with pytest.raises(PublicationError, match="identity"):
        load_publications(p, expected_sha256="e" * 64)
    for rows in [[row(tmp_path), row(tmp_path)], [{**row(tmp_path), "format": "repair-overlay-filesystem-candidate-v2"}]]:
        p = catalog(tmp_path, rows)
        with pytest.raises(PublicationError):
            load_publications(p, expected_sha256=digest(p))


def test_reference_requires_explicit_two_version_identity_and_false_eligibility(tmp_path):
    for changed in [
        {"version": "v2"},
        {"image": "relative"},
        {"legacy_manifest_sha256": "x"},
        {"training_eligible": True},
        {"publication": {"path": "relative", "sha256": "a" * 64, "key": "a" * 64}},
    ]:
        with pytest.raises(PublicationError):
            PublishedCheckpoint.from_dict({**row(tmp_path), **changed})


@pytest.mark.parametrize("purpose", ["difficulty", "training"])
def test_explicit_purpose_runs_same_verifier_without_qualification_upgrade(tmp_path, monkeypatch, purpose):
    import socket
    from unittest.mock import Mock

    ref = PublishedCheckpoint.from_dict({**row(tmp_path), "node": socket.gethostname()})
    verifier = Mock(return_value=object())
    monkeypatch.setattr("cooperagents.published.verify_publication", verifier)
    before = ref.model_dump()
    assert ref.verify(purpose=purpose) is verifier.return_value
    verifier.assert_called_once_with(ref)
    assert ref.model_dump() == before
    assert ref.training_eligible is False and ref.repair_validated is False


@pytest.mark.parametrize("purpose", ["", "unknown"])
def test_unknown_purpose_never_reads_payload(tmp_path, monkeypatch, purpose):
    from unittest.mock import Mock

    verifier = Mock()
    monkeypatch.setattr("cooperagents.published.verify_publication", verifier)
    with pytest.raises(PublicationError, match="explicit"):
        PublishedCheckpoint.from_dict(row(tmp_path)).verify(purpose=purpose)
    verifier.assert_not_called()


def test_training_still_requires_recorded_node_and_authentic_manifest(tmp_path, monkeypatch):
    import socket

    ref = PublishedCheckpoint.from_dict({**row(tmp_path), "node": "definitely-another-node"})
    with pytest.raises(PublicationError, match="recorded compute node"):
        ref.verify(purpose="training")
    ref = PublishedCheckpoint.from_dict({**row(tmp_path), "node": socket.gethostname()})
    with pytest.raises(PublicationError, match="missing"):
        ref.verify(purpose="training")


@pytest.mark.parametrize("purpose", ["difficulty", "training"])
def test_holder_serializes_explicit_execution_purpose(tmp_path, monkeypatch, purpose):
    from cooperagents.env.published_holder import MountHolder

    ref = PublishedCheckpoint.from_dict(row(tmp_path))
    calls = []
    monkeypatch.setattr(MountHolder, "_initialize", lambda self, *args: calls.append(args))
    MountHolder(ref, tmp_path, None, purpose=purpose)
    assert calls[0][0] == {"reference": ref.model_dump(), "purpose": purpose}
    with pytest.raises(PublicationError, match="explicit"):
        MountHolder(ref, tmp_path, None, purpose="unknown")
    assert len(calls) == 1


@pytest.mark.parametrize("purpose", ["difficulty", "training"])
def test_holder_child_uses_serialized_purpose_before_mounting(tmp_path, monkeypatch, purpose):
    from cooperagents.env.published_holder import _hold

    seen = []

    def verify(self, *, purpose):
        seen.append(purpose)
        raise PublicationError("stop before namespace or payload work")

    monkeypatch.setattr(PublishedCheckpoint, "verify", verify)
    control = tmp_path / "holder.json"
    control.write_text(json.dumps({"reference": row(tmp_path), "purpose": purpose}))
    with pytest.raises(PublicationError, match="stop before"):
        _hold(control)
    assert seen == [purpose]
