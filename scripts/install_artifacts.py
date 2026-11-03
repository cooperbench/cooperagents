"""Install an exclusive root-downloaded staging tree, never a worker-owned directory."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

STORE = Path("/opt/polar-repair-assets")


def flush(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install(kind, pin, stage, manifest):
    if os.geteuid() != 0 or Path("/proc/self/uid_map").read_text().split() != ["0", "0", "4294967295"]:
        raise ValueError("installer requires initial-namespace root")
    if kind not in {"pub", "prepared-sqfs"} or not re.fullmatch("[a-f0-9]{64}", pin):
        raise ValueError("invalid installed identity")
    if stage.parent != STORE / ".staging" or stage != stage.resolve() or not stage.name:
        raise ValueError("noncanonical root staging path")
    for p in (*reversed(stage.parents), stage):
        st = p.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid or st.st_mode & 0o022:
            raise ValueError("unprotected staging ancestor")
    if stage.stat().st_mode & 0o077:
        raise ValueError("staging must be private")
    if (stage / ".measured.json").exists() or manifest.is_absolute() or ".." in manifest.parts:
        raise ValueError("invalid staging manifest")
    target = STORE / kind / pin
    target.parent.mkdir(mode=0o755, exist_ok=True)
    with (STORE / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.lexists(target):
            raise FileExistsError(target)
        files = {}
        for path in stage.rglob("*"):
            info = path.lstat()
            if info.st_uid or info.st_mode & 0o022:
                raise ValueError("non-root or writable staging member")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("invalid staging member")
            with path.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            files[path.relative_to(stage).as_posix()] = dict(bytes=info.st_size, sha256=digest)
        if files[str(manifest)]["sha256"] != pin:
            raise ValueError("manifest identity mismatch")
        doc = json.loads((stage / manifest).read_bytes())
        if kind == "pub":
            if doc.get("format") != "repair-overlay-v3" or manifest.parts != (doc["key"], "publication.json"):
                raise ValueError("requires V3 publication")
            prefix = doc["key"] + "/"
            inventory = {k.removeprefix(prefix): v for k, v in files.items() if k != str(manifest)}
            if inventory != doc["files"] or any(not k.startswith(prefix) for k in files):
                raise ValueError("publication inventory mismatch")
        elif (str(manifest) != "manifest.json" or set(files) != {"manifest.json", "base.sqfs"}
              or doc.get("format") != "polar-audited-image-base-v1"
              or files["base.sqfs"] != dict(bytes=doc["sqfs_bytes"], sha256=doc["sqfs_sha256"])):
            raise ValueError("invalid prepared SQFS inventory")
        (stage / ".measured.json").write_text(json.dumps(dict(schema=1, kind=kind, manifest_sha256=pin, files=files)))
        for path in [*sorted(stage.rglob("*"), key=lambda p: len(p.parts), reverse=True), stage]:
            path.chmod(0o555 if path.is_dir() else 0o444)
            flush(path)
        stage.rename(target)
        for directory in (stage.parent, target.parent, STORE):
            flush(directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind")
    parser.add_argument("pin")
    parser.add_argument("stage", type=Path)
    parser.add_argument("manifest", type=Path)
    install(**vars(parser.parse_args()))
