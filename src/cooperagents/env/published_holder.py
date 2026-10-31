"""A per-rollout namespace holder; commands still use ApptainerEnv.execute.

The holder protocol has one initialization result and EOF shutdown. It never
transports agent commands, model requests, callbacks, or credentials.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import gzip
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cooperagents.checkpoint import remove_tree
from cooperagents.env.image_base import ImageBase
from cooperagents.env.published_layers import prepare_layers
from cooperagents.published import PublicationError, PublishedCheckpoint

MODULE = "cooperagents.env.published_holder"
MAX_READY_BYTES = 2 * 1024 * 1024


def read_ready(descriptor: int, timeout: float) -> dict:
    end = time.monotonic() + timeout
    data = bytearray()
    while b"\n" not in data:
        remaining = end - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise TimeoutError("published restore holder initialization timed out")
        block = os.read(descriptor, 65536)
        if not block:
            raise PublicationError("published restore holder exited before initialization")
        data.extend(block)
        if len(data) > MAX_READY_BYTES:
            raise PublicationError("oversized holder initialization record")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("status") != "ready":
        raise PublicationError("restore holder failed: " + str(value)[:2000])
    return value


class MountHolder:
    def __init__(self, reference: PublishedCheckpoint, scratch: Path, base: ImageBase) -> None:
        self._initialize(dict(reference=reference.model_dump()), scratch, base, reference.id)

    @classmethod
    def for_image(cls, image: Path, checksum: str, scratch: Path, base: ImageBase) -> MountHolder:
        base.validate(checksum)
        holder = cls.__new__(cls)
        holder._initialize(dict(operation="clean-image", image=str(image), image_sha256=checksum), scratch, base, checksum)
        return holder

    @classmethod
    def audit_capture(cls, bundle: Path, scratch: Path, base: ImageBase) -> MountHolder:
        base.validate(str(base.proof["image_sha256"]))
        holder = cls.__new__(cls)
        holder._initialize(dict(operation="audit-capture", bundle=str(bundle)), scratch, base, "capture-audit")
        return holder

    def _initialize(self, config: dict, scratch: Path, base: ImageBase, identity: str) -> None:
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ca-overlay-", dir=scratch))
        os.chown(self.root, -1, os.getgid())
        self.root.chmod(0o700)
        self._fds: list[int] = []
        self._closed = False
        self._stderr = (self.root / "holder.stderr").open("wb")
        self.process: subprocess.Popen | None = None
        control = self.root / "holder.json"
        control.write_text(json.dumps({**config, "root": str(self.root), "base_fd": base.descriptor, "base_proof": base.proof}))
        control.chmod(0o600)
        try:
            args = [sys.executable, "-m", MODULE, "--launch", str(os.getpid()), str(control)]
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                pass_fds=(base.descriptor,),
                start_new_session=True,
            )
            if self.process.stdout is None:
                raise PublicationError("missing holder result pipe")
            self.ready = read_ready(self.process.stdout.fileno(), 1800)
            self._open_namespaces()
        except BaseException as exc:
            self.close()
            failures = scratch / "failed-restores"
            failures.mkdir(mode=0o700, exist_ok=True)
            detail = (self.root / "holder.stderr").read_text(errors="replace")[-4000:]
            (failures / (self.root.name + ".json")).write_text(
                json.dumps(dict(checkpoint_id=identity, error_type=type(exc).__name__, error=str(exc), stderr=detail))
            )
            remove_tree(self.root)
            raise

    def _open_namespaces(self) -> None:
        if self.process is None or self.process.poll() is not None:
            raise PublicationError("holder died before namespace acquisition")
        parent = self.process.pid
        children = Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
        if len(children) != 1 or not children[0].isdigit():
            raise PublicationError("unexpected holder process topology")
        for namespace in ["user", "mnt", "pid"]:
            self._fds.append(os.open(f"/proc/{children[0]}/ns/{namespace}", os.O_RDONLY | os.O_CLOEXEC))

    def exec_prefix(self) -> tuple[str, ...]:
        if self._closed or len(self._fds) != 3:
            raise PublicationError("holder has no live namespaces")
        # Stable capabilities avoid target-PID reuse. nsenter opens these paths
        # before joining the holder's user/mount/PID namespaces.
        paths = [f"/proc/{os.getpid()}/fd/{fd}" for fd in self._fds]
        return ("nsenter", "--user=" + paths[0], "--mount=" + paths[1], "--pid=" + paths[2], "--preserve-credentials")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _stop(self.process)
        finally:
            for descriptor in self._fds:
                os.close(descriptor)
            self._fds.clear()
            self._stderr.close()


def _stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)
    if process.stdout is not None:
        process.stdout.close()


def _launch(parent: int, control: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    # Parent death kills unshare; --kill-child then kills the PID namespace init,
    # which kills FUSE and command descendants, including new-session commands.
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise PublicationError("cannot establish holder parent-death boundary")
    args = [
        "unshare",
        "--user",
        "--map-current-user",
        "--keep-caps",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
        "--mount-proc",
        "--propagation",
        "private",
        sys.executable,
        "-m",
        MODULE,
        str(control),
    ]
    os.execvp(args[0], args)


def _command(args: list[str], *, timeout: int = 120) -> None:
    subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout)


def _base_mount(root: Path, descriptor: int, proof: dict, expected_image: str) -> subprocess.Popen:
    info = os.fstat(descriptor)
    seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK
    if proof["image_sha256"] != expected_image or [info.st_dev, info.st_ino, info.st_size] != proof["fd_identity"]:
        raise PublicationError("image base capability identity mismatch")
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & seals != seals:
        raise PublicationError("image base is not immutable")
    base = root / "protected-base"
    base.mkdir()
    helper = subprocess.Popen(
        ["/usr/libexec/apptainer/bin/squashfuse_ll", "-f", "-o", "ro", f"/proc/self/fd/{descriptor}", str(base)],
        pass_fds=(descriptor,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while not (base / "usr").exists():
        if helper.poll() is not None or time.monotonic() > deadline:
            helper.kill()
            helper.wait()
            raise PublicationError("protected image base mount failed")
        time.sleep(0.02)
    return helper


def _mount_overlay(root: Path) -> None:
    for name in ["work", "fs"]:
        (root / name).mkdir()
    for name in ["lower", "protected-base"]:
        path = str(root / name)
        _command(["mount", "--bind", path, path])
        _command(["mount", "-o", "remount,bind,ro", path])
    options = f"lowerdir={root}/lower:{root}/protected-base,upperdir={root}/upper,workdir={root}/work,userxattr,index=off,metacopy=off"
    _command(["mount", "-t", "overlay", "overlay", "-o", options, str(root / "fs")])


def _hold(control: Path) -> None:
    config = json.loads(control.read_bytes())
    if config.get("operation") == "audit-capture":
        if Path(config["root"]) != control.parent:
            raise PublicationError("invalid private capture control path")
        from cooperagents.env.capture_audit import hold
        hold(config)
        return
    if config.get("operation") == "audit-base":
        _audit_base(config)
        return
    if config.get("operation") == "clean-image":
        if Path(config["root"]) != control.parent:
            raise PublicationError("invalid private evaluator control path")
        _hold_image(config)
        return
    reference = PublishedCheckpoint.from_dict(config["reference"])
    publication = reference.verify(purpose="difficulty")
    root = Path(config["root"])
    if root != control.parent or root.is_symlink() or root.stat().st_uid != os.getuid():
        raise PublicationError("invalid private holder root")
    if config["base_proof"].get("runtime_qualification") != publication.receipt["runtime_qualification"]:
        raise PublicationError("image base was qualified for a different runtime")
    prepare_layers(publication, root)
    helper = _base_mount(root, config["base_fd"], config["base_proof"], reference.image_sha256)
    try:
        _mount_overlay(root)
        _check_base_precision(root, publication, config["base_proof"])
        for directory in ["home/agent", "tmp", "var/tmp"]:
            path = root / "fs" / directory
            if path.is_symlink() or not path.resolve().is_relative_to(root / "fs"):
                raise PublicationError("runtime path escapes restored filesystem")
            path.mkdir(parents=True, exist_ok=True)
        print(
            json.dumps(
                dict(status="ready", state=publication.state, risks=publication.risks, publication_sha256=reference.publication.sha256)
            ),
            flush=True,
        )
        sys.stdin.buffer.read(1)
    finally:
        helper.terminate()
        try:
            helper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait()
    # Exiting PID init and releasing the parent's namespace FDs tears down all
    # mounts. No fragile chain of umount calls can skip later cleanup.


def _hold_image(config: dict) -> None:
    """Mount the pristine authenticated image, never a repair checkpoint layer."""
    root = Path(config["root"])
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise PublicationError("invalid private evaluator root")
    base = ImageBase(config["base_fd"], config["base_proof"])
    base.validate(config["image_sha256"])
    helper = _base_mount(root, base.descriptor, base.proof, config["image_sha256"])
    try:
        workspace = root / "protected-base/workspace"
        if workspace.is_symlink() or not workspace.is_dir():
            raise PublicationError("evaluator requires a real image workspace")
        (root / "lower").mkdir()
        (root / "upper").mkdir()
        _command(["cp", "-a", str(workspace), str(root / "upper/workspace")], timeout=1800)
        os.setxattr(root / "upper/workspace", "user.overlay.opaque", b"y")
        source_root = root / "protected-base"
        info = source_root.stat()
        os.chown(root / "upper", info.st_uid, info.st_gid)
        shutil.copystat(source_root, root / "upper", follow_symlinks=False)
        _mount_overlay(root)
        for name in ["home/agent", "tmp", "var/tmp", "patches", "cbshared"]:
            path = root / "fs" / name
            if path.is_symlink() or not path.resolve().is_relative_to(root / "fs"):
                raise PublicationError("evaluator runtime path escapes image")
            path.mkdir(parents=True, exist_ok=True)
        print(json.dumps(dict(status="ready", operation="clean-image")), flush=True)
        sys.stdin.buffer.read(1)
    finally:
        helper.terminate()
        try:
            helper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch", type=int)
    parser.add_argument("control", type=Path)
    args = parser.parse_args()
    try:
        if args.launch is not None:
            _launch(args.launch, args.control)
        _hold(args.control)
        return 0
    except BaseException as exc:
        print(json.dumps(dict(status="failed", error_type=type(exc).__name__, error=str(exc)[:2000])), flush=True)
        return 1


def _audit_base(config: dict) -> None:
    from cooperagents.env.published_audit import inventory
    from cooperagents.published import file_hash

    root = Path(config["root"])
    target = Path(config["native_inventory"])
    if file_hash(target) != config["native_inventory_sha256"]:
        raise PublicationError("Native base inventory identity mismatch")
    expected = json.load(gzip.open(target))
    helper = _base_mount(root, config["base_fd"], config["base_proof"], config["base_proof"]["image_sha256"])
    try:
        actual = inventory(root / "protected-base")
        losses = _compare_base(expected, actual)
        print(
            json.dumps(dict(status="ready", operation="audit-base", audit=dict(paths=len(actual["entries"]), mtime_precision=losses))),
            flush=True,
        )
    finally:
        helper.terminate()
        try:
            helper.wait(timeout=10)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper.wait()


def _compare_base(expected: dict, actual: dict) -> list[str]:
    if expected["entries"].keys() != actual["entries"].keys() or expected["hardlink_groups"] != actual["hardlink_groups"]:
        raise PublicationError("Protected base paths/hardlinks differ from native image")
    losses = []
    for name, row in expected["entries"].items():
        observed = actual["entries"][name]
        if {k: v for k, v in row.items() if k != "mtime_ns"} != {k: v for k, v in observed.items() if k != "mtime_ns"}:
            raise PublicationError("Protected base differs from native image: " + name)
        if row["mtime_ns"] != observed["mtime_ns"]:
            if row["mtime_ns"] // 1000000000 != observed["mtime_ns"] // 1000000000:
                raise PublicationError("Unexpected base timestamp change: " + name)
            losses.append(name)
    return losses


def _check_base_precision(root, publication, proof) -> None:
    losses = proof.get("audit", {}).get("mtime_precision", [])
    if not losses:
        return
    with gzip.open(publication.root / "target-index.json.gz") as stream:
        target = json.load(stream)["entries"]
    for name in losses:
        if name not in target or target[name]["type"] == "symlink":
            continue
        if abs((root / "fs" / name).lstat().st_mtime_ns - target[name]["mtime_ns"]) >= 1000:
            raise PublicationError("Combined checkpoint timestamp differs: " + name)


if __name__ == "__main__":
    raise SystemExit(main())
