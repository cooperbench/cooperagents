"""Descriptor transfer must survive a child that inherits no descriptors."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cooperagents.env.image_channel import ImageChannel


def test_private_channel_transfers_descriptor_and_closes(tmp_path):
    path = tmp_path / "bytes"
    path.write_bytes(b"authenticated-base")
    fd = os.open(path, os.O_RDONLY)
    try:
        with (
            tempfile.TemporaryDirectory(dir="/tmp", prefix="ic-") as short,
            ImageChannel(fd, {"identity": "fixture"}, Path(short)) as channel,
        ):
            script = """import os,json,sys
from cooperagents.env.image_channel import receive_image
fd, proof = receive_image(sys.argv[1])
try: print(json.dumps([os.read(fd,18).decode(),proof]))
finally: os.close(fd)
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(channel.path)], capture_output=True, text=True, check=True, close_fds=True
            )
            assert json.loads(result.stdout) == ["authenticated-base", {"identity": "fixture"}]
        assert not channel.path.exists()
    finally:
        os.close(fd)


def test_bad_proof_does_not_leave_channel_directory(tmp_path):
    import pytest

    with pytest.raises(TypeError):
        ImageChannel(0, {"invalid": object()}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_receive_in_same_process_verifies_private_parent(tmp_path):
    import pytest

    from cooperagents.env.image_channel import receive_image

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ic-") as short:
        fd = os.open(tmp_path / "base", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with ImageChannel(fd, {"test": 1}, Path(short)) as channel:
                received, proof = receive_image(str(channel.path))
                try:
                    assert proof == {"test": 1}
                    assert not os.get_inheritable(received)
                    os.write(received, b"shared bytes")
                    assert os.pread(fd, 12, 0) == b"shared bytes"
                finally:
                    os.close(received)
                channel.directory.chmod(0o755)
                with pytest.raises(ValueError, match="private owned"):
                    receive_image(str(channel.path))
        finally:
            os.close(fd)


def test_proof_size_limit_accepts_exact_bound_rejects_oversize():
    import pytest

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ic-") as short:
        proof = {"x": "a" * (1024 * 1024 - len(json.dumps({"x": ""}).encode()))}
        with ImageChannel(0, proof, Path(short)):
            pass
        proof["x"] += "b"
        with pytest.raises(ValueError, match="bounded"):
            ImageChannel(0, proof, Path(short))
        assert not list(Path(short).iterdir())


def test_channel_path_bound_is_rejected_before_bind():
    import pytest

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="ic-") as short:
        parent = Path(short) / ("x" * (78 - len(short) - 1))
        parent.mkdir()
        with pytest.raises(ValueError, match="short node-local"):
            ImageChannel(0, {}, parent)
        assert not list(parent.iterdir())
