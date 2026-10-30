"""Admission reads authenticated metadata; full payload verification stays at execution."""

import pytest

from cooperagents.checkpoint import sha256
from cooperagents.repair import load_repair_input_metadata
from test_repair_replay import make_checkpoint


def test_metadata_matches_full_input_without_reading_rootfs_payload(monkeypatch, tmp_path):
    from cooperagents.repair import load_repair_input

    cp = make_checkpoint(monkeypatch, tmp_path)
    expected = load_repair_input(cp)
    original = sha256

    def checked(path):
        assert path.name not in {"payload.tar.gz", "filesystem.json.gz"}
        return original(path)

    monkeypatch.setattr("cooperagents.checkpoint.sha256", checked)
    actual = load_repair_input_metadata(cp, expected_manifest_sha256=original(cp / "manifest.json"))
    assert actual == expected
    with pytest.raises(ValueError):
        load_repair_input_metadata(cp, expected_manifest_sha256="a" * 64)
    (cp / "repair-input.json").write_text("{}")
    with pytest.raises(ValueError):
        load_repair_input_metadata(cp, expected_manifest_sha256=original(cp / "manifest.json"))
