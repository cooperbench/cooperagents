import json

import pytest

from cooperagents.env.published_audit import inventory


@pytest.fixture(autouse=True)
def host_xattrs(monkeypatch):
    import os

    if not hasattr(os, "listxattr"):
        monkeypatch.setattr("cooperagents.env.published_audit._xattrs", lambda _: {})


def test_layer_selection_keeps_hardlinks_and_rejects_external_deletion(tmp_path):
    from cooperagents.env.capture_layers import select_layer

    base = tmp_path / "base"
    base.mkdir()
    (base / "repo").mkdir()
    (base / "library").write_text("old")
    before = inventory(base)
    (base / "library").write_text("new")
    (base / "alias").hardlink_to(base / "library")
    layer = select_layer(before, inventory(base), "repo")
    assert {"library", "alias"} <= layer["entries"].keys()
    (base / "library").unlink()
    with pytest.raises(ValueError, match="deletion"):
        select_layer(before, inventory(base), "repo")


def test_repo_deletion_is_represented_by_private_repo(tmp_path):
    from cooperagents.env.capture_layers import select_layer

    base = tmp_path / "base"
    (base / "repo").mkdir(parents=True)
    (base / "repo/deleted").write_text("x")
    before = inventory(base)
    (base / "repo/deleted").unlink()
    assert not select_layer(before, inventory(base), "repo")["entries"]


def test_special_state_is_not_silently_discarded(tmp_path):
    import os

    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ValueError, match="Unsupported filesystem object"):
        inventory(tmp_path)


def test_direct_metadata_has_no_legacy_rootfs_requirement(tmp_path):
    from cooperagents.checkpoint import sha256, verify_checkpoint

    state = {"version": 3, "runtime": {"backend": "apptainer", "format": "repair-overlay-v3", "mounts": []}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    for name in ["raw.patch", "integration.patch", "submission.patch"]:
        (tmp_path / name).write_text("")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": 3, "files": {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in tmp_path.iterdir()}})
    )
    assert verify_checkpoint(tmp_path) == state
    state["runtime"]["format"] = "unknown"
    (tmp_path / "state.json").write_text(json.dumps(state))
    m = json.loads((tmp_path / "manifest.json").read_text())
    m["files"]["state.json"] = {"bytes": (tmp_path / "state.json").stat().st_size, "sha256": sha256(tmp_path / "state.json")}
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ValueError, match="direct V3"):
        verify_checkpoint(tmp_path)


def test_repair_capture_dispatch_preserves_worker_capture(tmp_path):
    from cooperagents.checkpoint import save_checkpoint

    class DirectEnvironment:
        def capture_repair_checkpoint(self, destination, **kwargs):
            assert not destination.exists()
            assert kwargs["repair_input"] == "input"
            return "patch"

    assert (
        save_checkpoint(
            DirectEnvironment(),
            tmp_path / "capture",
            metadata={"boundary": "repair_agent_start"},
            repair_input="input",
            capture_format="v3",
        )
        == "patch"
    )
    with pytest.raises(ValueError, match="mutate"):
        save_checkpoint(
            DirectEnvironment(),
            tmp_path / "capture",
            metadata={"boundary": "repair_agent_start"},
            repair_input="input",
            capture_format="v3",
            collect_patch=lambda: None,
        )


def test_direct_producer_rejects_unknown_identity_and_legacy_format():
    from cooperagents.published import LEGACY, V3, PublicationError
    from cooperagents.published_profiles import direct_qualification, validate_producer

    receipt = {"producer": "direct-capture-v1", "producer_identity": direct_qualification()["producer_identity"]}
    validate_producer(receipt, V3)
    with pytest.raises(PublicationError):
        validate_producer(receipt, LEGACY)
    with pytest.raises(PublicationError):
        validate_producer({**receipt, "producer_identity": {}}, V3)


def test_archive_roundtrip_preserves_hardlinks_symlinks_and_mode(tmp_path):
    import os

    from cooperagents.checkpoint import extract_tree
    from cooperagents.env.capture_archives import archive

    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"payload")
    (source / "a").chmod(0o751)
    (source / "b").hardlink_to(source / "a")
    (source / "link").symlink_to("a")
    archive(source, tmp_path / "snapshot.tar", inventory(source))
    target = tmp_path / "target"
    extract_tree(tmp_path / "snapshot.tar", target)
    assert (target / "a").read_bytes() == b"payload"
    assert (target / "a").stat().st_mode & 0o777 == 0o751
    assert os.path.samefile(target / "a", target / "b")
    assert os.readlink(target / "link") == "a"


def test_composition_audit_detects_semantic_changes(tmp_path):
    import copy

    from cooperagents.env.capture_audit import compare
    from cooperagents.published import PublicationError

    (tmp_path / "file").write_text("content")
    expected = inventory(tmp_path)
    compare(expected, expected)
    changed = copy.deepcopy(expected)
    changed["entries"]["file"]["mode"] = 0
    with pytest.raises(PublicationError, match="mode"):
        compare(expected, changed)


def test_capture_failure_removes_staging_and_never_publishes(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from cooperagents.env import published_capture

    monkeypatch.setattr(published_capture, "validate_reader", lambda: None)

    def local_root(scratch):
        root = scratch / "publications"
        root.mkdir()
        return root

    monkeypatch.setattr(published_capture, "publication_root", local_root)

    def fail(env, destination, metadata, repair_input, trace, staging):
        (staging / "partial.tar").write_bytes(b"partial")
        staging.chmod(0o500)
        raise ValueError("source changed")

    monkeypatch.setattr(published_capture, "_save", fail)
    root = tmp_path / "sandbox"
    root.mkdir()
    env = SimpleNamespace(root=root, _lock=threading.RLock(), _closed=False)
    destination = tmp_path / "checkpoint"
    with pytest.raises(ValueError, match="source changed"):
        published_capture.save(env, destination, metadata={"boundary": "repair_agent_start"}, repair_input=object())
    assert not (destination / "manifest.json").exists()
    assert not list((tmp_path / "publications").iterdir())
    assert json.loads((destination / "capture-failure.json").read_text())["complete"] is False


def test_publication_root_rejects_disposable_and_shared_storage(tmp_path, monkeypatch):
    from cooperagents.env.capture_archives import publication_root

    monkeypatch.setenv("SLURM_TMPDIR", str(tmp_path))
    monkeypatch.delenv("COOPER_PUBLICATION_ROOT", raising=False)
    with pytest.raises(ValueError, match="disposable"):
        publication_root(tmp_path)
    monkeypatch.delenv("SLURM_TMPDIR")
    monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: "nfs4\n")
    with pytest.raises(ValueError, match="node-local"):
        publication_root(tmp_path)
    assert not (tmp_path / "publications").exists()


def test_direct_profile_checks_training_selection_hash():
    from cooperagents.published import PublicationError
    from cooperagents.published_profiles import direct_qualification, validate_capture

    sources = {**direct_qualification()["capture_sources"], "training_selection.py": "0" * 64}
    with pytest.raises(PublicationError, match="transitive source difference"):
        validate_capture(sources, "captured-direct-v3-v1")
