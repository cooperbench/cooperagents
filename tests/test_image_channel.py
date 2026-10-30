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
