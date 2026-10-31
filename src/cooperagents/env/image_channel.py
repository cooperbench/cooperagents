"""Private node-local transfer of an immutable image descriptor across Slurm steps.

No commands are transported. Receivers must independently validate the returned
image identity, descriptor seals and qualification before mounting anything.
"""

from __future__ import annotations

import array
import json
import os
import socket
import tempfile
import threading
from pathlib import Path


class ImageChannel:
    def __init__(self, descriptor: int, proof: dict[str, object], directory: Path) -> None:
        self._proof = json.dumps(proof).encode()
        if len(self._proof) > 1024 * 1024:
            raise ValueError("image proof exceeds bounded transfer size")
        self.directory = Path(tempfile.mkdtemp(prefix="image-", dir=directory))
        self.path = self.directory / "socket"
        self._descriptor = -1
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.directory.chmod(0o700)
            if len(os.fsencode(self.path)) >= 100:
                raise ValueError("image channel requires a short node-local directory")
            self._descriptor = os.dup(descriptor)
            self._socket.bind(str(self.path))
            self.path.chmod(0o600)
            self._socket.listen(16)
            self._socket.settimeout(0.2)
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
        except BaseException:
            self._socket.close()
            if self._descriptor >= 0:
                os.close(self._descriptor)
            self.path.unlink(missing_ok=True)
            self.directory.rmdir()
            raise

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                try:
                    connection.sendmsg([b"I"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [self._descriptor]))])
                    connection.sendall(len(self._proof).to_bytes(4, "big") + self._proof)
                except OSError:
                    continue

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=6)
        if self._thread.is_alive():
            raise RuntimeError("image channel did not stop")
        os.close(self._descriptor)
        self.path.unlink(missing_ok=True)
        self.directory.rmdir()

    def __enter__(self) -> ImageChannel:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _read(connection: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        block = connection.recv(length - len(result))
        if not block:
            raise ValueError("incomplete image capability transfer")
        result.extend(block)
    return bytes(result)


def receive_image(path: str) -> tuple[int, dict]:
    address = Path(path)
    parent = address.parent.stat()
    if address.parent.is_symlink() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError("image channel must have a private owned parent")
    descriptors = array.array("i")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(path)
            marker, ancillary, flags, _ = connection.recvmsg(1, socket.CMSG_SPACE(descriptors.itemsize))
            _accept_descriptor(marker, ancillary, flags, descriptors)
            proof = _receive_proof(connection)
            return descriptors[0], proof
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def _accept_descriptor(marker, ancillary, flags, descriptors) -> None:
    for level, kind, data in ancillary:
        if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
            descriptors.frombytes(data[: len(data) - len(data) % descriptors.itemsize])
    if marker != b"I" or flags & socket.MSG_CTRUNC or len(descriptors) != 1:
        raise ValueError("invalid image descriptor transfer")
    os.set_inheritable(descriptors[0], False)


def _receive_proof(connection: socket.socket) -> dict:
    size = int.from_bytes(_read(connection, 4), "big")
    if not 0 < size <= 1024 * 1024:
        raise ValueError("invalid image proof length")
    proof = json.loads(_read(connection, size))
    if not isinstance(proof, dict):
        raise ValueError("invalid image proof")
    return proof
