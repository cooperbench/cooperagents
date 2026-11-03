"""Exercise the real installer in a temporary root; emulate host ownership and uid_map."""
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("install_artifacts", Path(__file__).resolve().parents[1] / "scripts/install_artifacts.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
digest = lambda data: hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    stage = root / ".staging" / "unique"
    stage.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(installer, "STORE", root)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **kw: "0 0 4294967295" if str(p) == "/proc/self/uid_map" else read(p, *a, **kw))
    lstat = Path.lstat
    def root_stat(p):
        fields = list(lstat(p))
        fields[4] = 0
        if not p.is_relative_to(root):
            fields[0] &= ~0o022
        return os.stat_result(fields)
    monkeypatch.setattr(Path, "lstat", root_stat)
    if sys.platform == "darwin":
        # Darwin requires a directory write bit to update ".." on cross-parent rename
        # by a non-root test process. The release installer uses Linux host root.
        rename = Path.rename
        def root_rename(path, target):
            path.chmod(0o755)
            result = rename(path, target)
            result.chmod(0o555)
            return result
        monkeypatch.setattr(Path, "rename", root_rename)
    return root, stage


@pytest.mark.parametrize("case", ["good", "exists", "format", "inventory", "symlink", "hardlink"])
def test_publication_install(store, case):
    root, stage = store
    key = "b" * 64
    member = stage / key / "payload"
    member.parent.mkdir()
    member.write_bytes(b"payload bytes")
    row = dict(sha256=digest(member.read_bytes()), bytes=member.stat().st_size)
    doc = dict(key=key, format="repair-overlay-v3", files={"payload": row})
    if case == "format":
        doc["format"] = "legacy"
    if case == "inventory":
        doc["files"] = {}
    manifest = Path(key) / "publication.json"
    data = json.dumps(doc).encode()
    (stage / manifest).write_bytes(data)
    pin = digest(data)
    target = root / "pub" / pin
    if case == "exists":
        target.mkdir(parents=True)
    if case == "symlink":
        (member.parent / "other").symlink_to(member)
    if case == "hardlink":
        (member.parent / "other").hardlink_to(member)
    if case != "good":
        with pytest.raises((ValueError, FileExistsError)):
            installer.install("pub", pin, stage, manifest)
        return
    installer.install("pub", pin, stage, manifest)
    receipt = json.loads((target / ".measured.json").read_bytes())
    assert receipt["files"] == {f"{key}/payload": row, str(manifest): dict(bytes=len(data), sha256=pin)}
    assert not (target / key / ".measured.json").exists()
    assert (target / key / "payload").stat().st_mode & 0o777 == 0o444
    assert target.stat().st_mode & 0o777 == (target / key).stat().st_mode & 0o777 == 0o555


@pytest.mark.parametrize("wrong", ["sqfs_bytes", "sqfs_sha256"])
def test_sqfs_manifest_must_match_measurement(store, wrong):
    _, stage = store
    payload = b"SQFS"
    doc = dict(format="polar-audited-image-base-v1", sqfs_bytes=len(payload), sqfs_sha256=digest(payload))
    doc[wrong] = 0 if wrong == "sqfs_bytes" else "0" * 64
    data = json.dumps(doc).encode()
    (stage / "manifest.json").write_bytes(data)
    (stage / "base.sqfs").write_bytes(payload)
    with pytest.raises(ValueError, match="inventory"):
        installer.install("prepared-sqfs", digest(data), stage, Path("manifest.json"))
