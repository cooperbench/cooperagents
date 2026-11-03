import hashlib
from pathlib import Path

import pytest

from cooperagents import installed_artifacts as ia
from cooperagents.env.published_layers import private_archive


def test_protected_archive_requires_independent_receipt_and_avoids_disposable_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(ia, "STORE", tmp_path)
    root = tmp_path / "pub" / ("a" * 64)
    root.mkdir(parents=True)
    archive = root / "repo.tar"
    archive.write_bytes(b"real tar bytes")
    archive.chmod(0o444)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = ia.InstalledArtifacts(root, {"repo.tar": {"sha256": sha, "bytes": 14}}, [])
    monkeypatch.setattr(ia, "publication", lambda path, binding=None: receipt)
    with ia.admission(root):
        assert private_archive(archive, root / "copy", sha) == archive
        assert not (root / "copy").exists()
        with pytest.raises(ValueError, match="identity"):
            private_archive(archive, root / "copy", "b" * 64)
        archive.chmod(0o644)
        with pytest.raises(ValueError, match="invalid installed"):
            private_archive(archive, root / "copy", sha)


def test_nested_namespace_cannot_admit_store(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda self: "0 1000 1")
    with pytest.raises(ValueError, match="original user namespace"):
        ia.open_installed("pub", "0" * 64)


def test_ordinary_archive_keeps_copy_verification(tmp_path):
    archive = tmp_path / "input"
    archive.write_bytes(b"untrusted")
    with pytest.raises(ValueError, match="copy identity"):
        private_archive(archive, tmp_path / "copy", "b" * 64)


def test_small_manifest_is_rehashed_even_in_guarded_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ia, "STORE", tmp_path)
    path = tmp_path / "manifest.json"
    path.write_bytes(b"current bytes")
    assert ia.measured(path, lambda p: hashlib.sha256(p.read_bytes()).hexdigest()) == hashlib.sha256(b"current bytes").hexdigest()


def test_holder_receipt_cannot_change_after_parent_admission(tmp_path, monkeypatch):
    import json
    import stat
    from types import SimpleNamespace

    monkeypatch.setattr(ia, "STORE", tmp_path)
    pin = "a" * 64
    root = tmp_path / "pub" / pin
    root.mkdir(parents=True)
    receipt = root / ia.RECEIPT
    receipt.write_text(json.dumps(dict(schema=1, kind="pub", manifest_sha256=pin, files={})))
    original = Path.lstat

    def trusted(path):
        info = original(path)
        return SimpleNamespace(st_mode=(stat.S_IFREG | 0o444 if path == receipt else stat.S_IFDIR | 0o555),
                               st_uid=0, st_ino=info.st_ino, st_dev=info.st_dev)
    monkeypatch.setattr(Path, "lstat", trusted)
    st = root.stat()
    binding = [st.st_dev, st.st_ino, hashlib.sha256(receipt.read_bytes()).hexdigest()]
    assert ia.publication(root, binding).binding == binding
    receipt.write_text(receipt.read_text() + " ")
    with pytest.raises(ValueError, match="holder binding mismatch"):
        ia.publication(root, binding)


def test_verified_v3_and_prepared_mount_hash_only_source_anchors(tmp_path, monkeypatch):
    import io
    import json
    import tarfile

    from cooperagents.env.published_layers import _prepared_mount
    from cooperagents.published import PublishedCheckpoint, _inventory
    from test_publication_integrity import publication

    root, ref, _ = publication(tmp_path, monkeypatch, "v3")
    root.chmod(0o755)
    mounts = root / "mounts"
    mounts.mkdir()
    payload = b"mount data"
    archive = mounts / "mount-0.tar"
    with tarfile.open(archive, "w") as tar:
        member = tarfile.TarInfo("workspace")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    tree = mounts / "mount-0.tree.json.gz"
    tree.write_bytes(b"tree metadata")
    row = dict(target="/coordination", readonly=True, archive="mounts/mount-0.tar", tree="mounts/mount-0.tree.json.gz",
               archive_bytes=archive.stat().st_size, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
               tree_sha256=hashlib.sha256(tree.read_bytes()).hexdigest())
    header = root / "publication.json"
    header.chmod(0o644)
    header.write_text(json.dumps(json.loads(header.read_text()) | {"files": _inventory(root)}))
    pin = hashlib.sha256(header.read_bytes()).hexdigest()
    store = tmp_path / "store"
    installed_root = store / "pub" / pin
    installed_root.mkdir(parents=True)
    moved = root.rename(installed_root / root.name)
    files = {p.relative_to(installed_root).as_posix(): dict(bytes=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
             for p in moved.rglob("*") if p.is_file()}
    for p in [*moved.rglob("*"), moved]:
        p.chmod(0o555 if p.is_dir() else 0o444)
    receipt = ia.InstalledArtifacts(installed_root, files, [])
    record = ref.model_dump()
    record["publication"].update(path=str(moved), sha256=pin)
    ref = PublishedCheckpoint.from_dict(record)
    monkeypatch.setattr(ia, "STORE", store)
    monkeypatch.setattr(ia, "publication", lambda path, binding=None: receipt)
    seen = []
    original = hashlib.file_digest
    def count(stream, algorithm):
        seen.append((Path(stream.name), Path(stream.name).stat().st_size))
        return original(stream, algorithm)
    monkeypatch.setattr(hashlib, "file_digest", count)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with ia.admission(moved):
        checked = ref.verify(purpose="training")
        assert checked.root == moved
        _prepared_mount(moved, scratch, 0, row, dict(target="/coordination", readonly=True))
    assert (scratch / "mount-0/workspace").read_bytes() == payload
    assert {p.name for p, n in seen if p.is_relative_to(store) and n > 0} == {"publication.json", "manifest.json"}
