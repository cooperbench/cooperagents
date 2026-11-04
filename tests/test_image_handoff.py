import fcntl
import os
import sys

import pytest

from cooperagents.env.image_base import ImageBase


@pytest.mark.skipif(sys.platform != "linux", reason="Linux sealed image descriptor")
@pytest.mark.parametrize("reader", ["reader", "90db555845889be5ba02e7d17b198b48c2b0b7e605aaf3b3c77d50af0a9f1be7"])
def test_handoff_borrows_independent_sealed_descriptor_and_rejects_bad_proof(tmp_path, monkeypatch, reader):
    from cooperagents.env import image_base

    monkeypatch.setattr(image_base, "_runtime", lambda: {"kernel": "test"})
    monkeypatch.setattr("cooperagents.published_profiles.qualification_identity", lambda: "reader")
    fd = os.memfd_create("test-base", os.MFD_ALLOW_SEALING)
    os.write(fd, b"base")
    seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
    st = os.fstat(fd)
    base = ImageBase(
        fd,
        dict(
            image_sha256="a" * 64,
            fd_identity=[st.st_dev, st.st_ino, st.st_size],
            reader_qualification_sha256=reader,
            runtime_qualification={"kernel": "test"},
            audit={"paths": 1},
        ),
    )
    try:
        handoff = base.handoff(tmp_path / "private")
        assert handoff.pass_fds == (fd,)
        assert handoff.proof_file.exists()
        with ImageBase.from_environment("a" * 64, handoff.environment) as child:
            assert child.descriptor != fd
            assert os.pread(child.descriptor, 4, 0) == b"base"
            with pytest.raises(OSError):
                os.pwrite(child.descriptor, b"x", 0)
        assert os.pread(fd, 4, 0) == b"base"
        assert base.handoff(tmp_path / "private").checksum == handoff.checksum
        with pytest.raises(ValueError, match="different|identity"):
            ImageBase.from_environment("b" * 64, handoff.environment)
        for change in (
            {"reader_qualification_sha256": "0" * 64},
            {"runtime_qualification": {"kernel": "different"}},
            {"audit": {"paths": 0}},
        ):
            with ImageBase(os.dup(fd), {**base.proof, **change}) as invalid:
                with pytest.raises(ValueError, match="unqualified|qualification"):
                    invalid.validate("a" * 64)
        handoff.proof_file.chmod(0o600)
        with pytest.raises(ValueError, match="proof"):
            ImageBase.from_environment("a" * 64, handoff.environment)
    finally:
        base.close()


def test_absent_handoff_is_optional_but_partial_never_falls_back():
    assert ImageBase.from_environment("a" * 64, {}) is None
    with pytest.raises(ValueError, match="incomplete"):
        ImageBase.from_environment("a" * 64, {"COOPER_REPAIR_BASE_FD": "1"})
