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


def test_difficulty_ref_is_not_a_qualification_upgrade(tmp_path):
    ref = PublishedCheckpoint.from_dict(row(tmp_path))
    assert ref.training_eligible is False and ref.repair_validated is False
    with pytest.raises(PublicationError, match="difficulty"):
        ref.verify(purpose="training")
