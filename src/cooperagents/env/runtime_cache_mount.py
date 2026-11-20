"""Optional uv overlay inside the existing episode holder namespace."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from cooperagents.env.runtime_cache import TARGET, validate_transport

LOG = logging.getLogger(__name__)


def prepare_cache(
    config: dict, root: Path, publication=None, *, task: tuple[str, int] | None = None, image: str | None = None,
    repo_path: str = "/workspace/repo",
) -> tuple[dict, subprocess.Popen | None]:
    """Never turn a failed cache admission/mount into a failed repair."""
    helper = None
    mounted = False
    directory = root / "runtime-cache"
    checks: dict = dict(helper_exists=False, squashfuse_mount=False, overlay_mount=False)
    try:
        if publication is not None:
            from cooperagents.repair import load_repair_input

            inputs = load_repair_input(publication.checkpoint)
            task = (inputs.settings.repo, inputs.settings.task_id)
            image = publication.reference.image_sha256
            repo_path = publication.state["repo_path"]
        if task is None or image is None:
            raise ValueError("cache requires an explicit repository/task/image binding")
        receipt = validate_transport(
            config, repo=task[0], task_id=task[1], image=image, filesystem=root / "fs", repo_path=repo_path,
        )
        target = root / "fs" / TARGET.lstrip("/")
        for parent in [*reversed(target.parents), target]:
            if parent.is_relative_to(root / "fs"):
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise ValueError("uv cache mount target crosses a non-directory")
                parent.mkdir(exist_ok=True)
        for name in ["lower", "upper", "work", "fs"]:
            (directory / name).mkdir(parents=True)
        descriptor = config["descriptor"]
        executable = squashfuse_helper()
        checks.update(helper_path=executable, helper_exists=True)
        helper = subprocess.Popen(
            [executable, "-f", "-o", "ro", f"/proc/self/fd/{descriptor}", str(directory / "lower")],
            pass_fds=(descriptor,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while not os.path.ismount(directory / "lower"):
            if helper.poll() is not None or time.monotonic() > deadline:
                raise ValueError("uv cache SQFS mount failed")
            time.sleep(0.02)
        checks["squashfuse_mount"] = True
        options = (f"lowerdir={directory}/lower,upperdir={directory}/upper,workdir={directory}/work,"
                   "userxattr,index=off,metacopy=off")
        subprocess.run(["mount", "-t", "overlay", "overlay", "-o", options, str(directory / "fs")],
                       capture_output=True, check=True, timeout=120)
        mounted = True
        if not os.path.ismount(directory / "fs"):
            raise ValueError("uv cache overlay command did not create a mount")
        checks.update(overlay_mount=True, **namespace_identity())
        record = dict(status="hit", key=receipt.identity.key, mount_checks=checks, **receipt.model_dump())
        return record, helper
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # A separate mount means a broken cache cannot replace any original
        # checkpoint path. Namespace shutdown also releases partial mounts.
        try:
            if mounted:
                subprocess.run(["umount", str(directory / "fs")], capture_output=True, timeout=30)
            if helper is not None:
                helper.terminate()
                try:
                    helper.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    helper.kill()
                    helper.wait()
        except (OSError, subprocess.SubprocessError) as cleanup_error:
            # The failed cache is never bound into Apptainer. Holder namespace
            # teardown releases any mount/helper left after best-effort cleanup.
            LOG.warning("uv cache fallback cleanup deferred to holder shutdown: %s", cleanup_error)
        detail = str(getattr(exc, "stderr", "") or "")[-2000:]
        reason = f"{type(exc).__name__}: {exc}" + (f"; stderr: {detail}" if detail else "")
        LOG.warning("uv runtime cache fallback: %s", reason)
        return dict(status="fallback", reason=reason, mount_checks=checks), None


def squashfuse_helper() -> str:
    """Resolve a trusted host helper before attempting the optional layer."""
    bundled = Path("/usr/libexec/apptainer/bin/squashfuse_ll")
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    discovered = shutil.which("squashfuse_ll")
    if discovered is None:
        raise ValueError("executable squashfuse_ll helper is unavailable")
    return discovered


def namespace_identity() -> dict:
    return dict(namespace_uid=os.getuid(), namespace_gid=os.getgid(), uid_map=Path("/proc/self/uid_map").read_text())
