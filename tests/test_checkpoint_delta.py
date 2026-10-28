"""Lossless filesystem deltas, using the promoted historical archive algorithm."""

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile

import pytest

from cooperagents.checkpoint import archive_tree, extract_tree
from cooperagents.checkpoint_delta import cached_base, pack_stream, pack_tree, restore


def test_delta_preserves_exact_tar_and_all_filesystem_changes(tmp_path):
    base, target = tmp_path / "base", tmp_path / "target"
    base.mkdir()
    (base / "unchanged").write_bytes(os.urandom(2 * 1024 * 1024))
    (base / "modified").write_bytes(b"a" * (1024 * 1024) + b"old")
    (base / "deleted").write_bytes(b"deleted")
    (base / "mode").write_bytes(b"unchanged content")
    shutil.copytree(base, target)
    (base / "replaced-link").symlink_to("/usr/bin")
    (target / "replaced-link").mkdir()
    (target / "replaced-link/new-tool").write_bytes(b"new tool")
    (target / "deleted").unlink()
    (target / "modified").write_bytes(b"a" * (1024 * 1024) + b"new")
    (target / "empty").touch()
    (target / "binary").write_bytes(bytes(range(256)))
    (target / "mode").chmod(0o640)
    (target / "symlink").symlink_to("mode")
    (target / "system-link").symlink_to("/usr/bin")
    os.link(target / "mode", target / "hardlink")
    (target / "read-only").mkdir()
    (target / "read-only").chmod(0o555)
    original = tmp_path / "original.tar.gz"
    archive_tree(target, original)
    delta = tmp_path / "delta"
    manifest = pack_tree(target, base, delta)
    assert (delta / "payload.tar.gz").stat().st_size < 10000
    assert next(r for r in manifest["entries"] if r["tar"]["name"] == "./unchanged")["payload"] is False
    restored = tmp_path / "restored.tar.gz"
    restore(delta, base, restored)
    with gzip.open(original, "rb") as left, gzip.open(restored, "rb") as right:
        assert left.read() == right.read()
    fs = tmp_path / "fs"
    extract_tree(restored, fs)
    assert not (fs / "deleted").exists()
    assert (fs / "empty").read_bytes() == b""
    assert (fs / "binary").read_bytes() == bytes(range(256))
    assert (fs / "mode").stat().st_mode & 0o7777 == 0o640
    assert (fs / "read-only").stat().st_mode & 0o7777 == 0o555
    assert os.readlink(fs / "system-link") == "/usr/bin"
    assert os.path.samefile(fs / "hardlink", fs / "mode")
    (base / "unchanged").write_bytes(b"bad base")
    with pytest.raises(ValueError, match="base file"):
        restore(delta, base, tmp_path / "failed.tar.gz")
    assert not (tmp_path / "failed.tar.gz.partial").exists()
    # The old offline payload/metadata format can be read by the formal module.
    (base / "unchanged").write_bytes((target / "unchanged").read_bytes())
    with gzip.open(delta / "filesystem.json.gz", "wt") as stream:
        json.dump({**manifest, "source_archive_sha256": "old-format-provenance"}, stream)
    assert restore(delta, base, tmp_path / "unused", verify_only=True)["verified"]


def test_cache_publishes_one_complete_base_and_retains_no_failed_build(tmp_path):
    calls = []

    def build(path):
        calls.append(path)
        path.mkdir()
        (path / "base").write_bytes(b"fixed image")

    first = cached_base(tmp_path, "a" * 64, build)
    assert cached_base(tmp_path, "a" * 64, build) == first
    assert len(calls) == 1

    def fail(path):
        path.mkdir(mode=0o555)
        raise RuntimeError("incomplete")

    with pytest.raises(RuntimeError, match="incomplete"):
        cached_base(tmp_path, "b" * 64, fail)
    assert not (tmp_path / ("b" * 64)).exists()
    assert not list(tmp_path.glob(".base-*"))


def test_stream_capture_propagates_producer_failure(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def fail(*args, **kwargs):
        raise OSError("read failed")

    monkeypatch.setattr(tarfile.TarFile, "add", fail)
    with pytest.raises((OSError, tarfile.ReadError)):
        pack_tree(source, source, tmp_path / "delta")


def test_docker_tar_headers_and_short_end_padding_are_canonicalized(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    member = tarfile.TarInfo("file")
    member.size = 4
    member.mode = 0o640
    raw = member.tobuf(format=tarfile.USTAR_FORMAT) + b"hint" + bytes(508) + bytes(1024)
    assert len(raw) == 2048  # Docker/Go tar need not pad to Python's 10240-byte record.
    manifest = pack_stream(io.BytesIO(raw), base, tmp_path / "delta", canonicalize=True)
    assert manifest["source_uncompressed_tar_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["target_uncompressed_tar_sha256"] != manifest["source_uncompressed_tar_sha256"]
    restore(tmp_path / "delta", base, tmp_path / "restored.tar.gz")
    with tarfile.open(tmp_path / "restored.tar.gz") as archive:
        assert archive.extractfile("file").read() == b"hint"
        assert archive.getmember("file").mode == 0o640


def test_legacy_apptainer_definition_restores_after_image_move(tmp_path):
    from cooperagents.checkpoint import restore_tree

    old_image = "/old storage/task.sif"
    original = f"bootstrap: localimage\nfrom: {old_image}\n\n".encode()
    base = tmp_path / "base"
    (base / ".singularity.d").mkdir(parents=True)
    definition = base / ".singularity.d/Singularity"
    definition.write_bytes(original)
    (base / "ordinary").write_bytes(b"unchanged")
    target = tmp_path / "target"
    shutil.copytree(base, target)
    delta = tmp_path / "rootfs"
    manifest = pack_tree(target, base, delta)  # Old producer omitted equal generated files.
    assert not next(r for r in manifest["entries"] if r["tar"]["name"].endswith("Singularity"))["payload"]
    definition.write_bytes(b"bootstrap: localimage\nfrom: /new/task.sif\n\n")
    new_definition = definition.read_bytes()
    saved = dict(backend="apptainer", image=old_image, format=manifest["format"], archive="rootfs/filesystem.json.gz")
    restore_tree(tmp_path, saved, base, tmp_path / "restored")
    assert (tmp_path / "restored/.singularity.d/Singularity").read_bytes() == original
    assert definition.read_bytes() == new_definition
    # Same base cache must also serve a different old source path without mutation.
    second = tmp_path / "second"
    shutil.copytree(target, second)
    second_image = "/another/task.sif"
    (second / ".singularity.d/Singularity").write_text(f"bootstrap: localimage\nfrom: {second_image}\n\n")
    pack_tree(second, second, tmp_path / "second-delta")
    assert restore(tmp_path / "second-delta", base, tmp_path / "second.tar.gz", original_apptainer_image=second_image)["verified"]
    assert definition.read_bytes() == new_definition
    for image in (None, "/wrong/task.sif"):
        with pytest.raises(ValueError, match="base file"):
            restore(delta, base, tmp_path / "failed.tar.gz", original_apptainer_image=image)
        assert not (tmp_path / "failed.tar.gz.partial").exists()
    (base / "ordinary").write_bytes(b"corrupted")  # Same length still requires exact hash.
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore(delta, base, tmp_path / "bad-base.tar.gz", original_apptainer_image=old_image)
    assert not (tmp_path / "bad-base.tar.gz.partial").exists()
    (base / "ordinary").write_bytes(b"unchanged")
    with gzip.open(delta / "filesystem.json.gz", "wt") as f:
        json.dump({**manifest, "target_uncompressed_tar_sha256": "0" * 64}, f)
    with pytest.raises(ValueError, match="exactly match"):
        restore(delta, base, tmp_path / "bad-tar.tar.gz", original_apptainer_image=old_image)
    assert not (tmp_path / "bad-tar.tar.gz.partial").exists()


def test_legacy_custom_definition_is_not_replaced_by_generated_text(tmp_path):
    base = tmp_path / "base"
    (base / ".singularity.d").mkdir(parents=True)
    definition = base / ".singularity.d/Singularity"
    definition.write_bytes(b"custom definition\n")
    pack_tree(base, base, tmp_path / "delta")
    assert restore(tmp_path / "delta", base, tmp_path / "valid.tar.gz", original_apptainer_image="/old.sif")["verified"]
    definition.write_bytes(b"changed definition\n")
    with pytest.raises(ValueError, match="base file"):
        restore(tmp_path / "delta", base, tmp_path / "invalid.tar.gz", original_apptainer_image="/old.sif")
