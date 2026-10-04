"""Persistent, job-owned writable Apptainer sandboxes for CPU cluster tasks."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from cooperagents.env.base import Environment, ExecResult
from cooperagents.env.limited_process import run_limited


class ApptainerEnv(Environment):
    def __init__(
        self,
        image: str,
        *,
        scratch: str,
        shared: str | None = None,
        repo_path: str = "/workspace/repo",
        coordinator_dir: Path | None = None,
    ) -> None:
        sif = Path(image).resolve(strict=True)
        if not sif.is_file():
            raise ValueError("Apptainer image must be a prepared SIF file")
        Path(scratch).mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ca-", dir=scratch))
        self.repo_path = repo_path
        self.image = sif
        self._checkpoint_mounts = {}
        self._lock = threading.RLock()
        self._closed = False
        self._host_env = {
            k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "APPTAINER_CACHEDIR", "APPTAINER_TMPDIR")
        }
        self._host_env["HOME"] = str(self.root / "host-home")
        (self.root / "host-home").mkdir()
        try:
            subprocess.run(
                ["apptainer", "build", "--fix-perms", "--sandbox", str(self.root / "fs"), str(sif)],
                env=self._host_env,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            for directory in ("tmp", "var/tmp", "home/agent", "workspace", "patches", "cbshared"):
                (self.root / "fs" / directory).mkdir(parents=True, exist_ok=True)
            self._argv = [
                "apptainer",
                "exec",
                "--writable",
                "--containall",
                "--cleanenv",
                "--no-mount",
                "hostfs,bind-paths",
                "--bind",
                "/etc/resolv.conf:/etc/resolv.conf:ro",
                "--home",
                f"{self.root / 'fs/home/agent'}:/home/agent",
                "--bind",
                f"{self.root / 'fs/tmp'}:/tmp",
                "--bind",
                f"{self.root / 'fs/var/tmp'}:/var/tmp",
            ]
            if shared:
                Path(shared).mkdir(parents=True, exist_ok=True)
                self._argv += ["--bind", f"{Path(shared).resolve()}:/cbshared"]
                self._checkpoint_mounts["/cbshared"] = Path(shared).resolve()
            if coordinator_dir is not None:
                notebook_dir = Path(coordinator_dir).resolve(strict=True)
                if not notebook_dir.is_dir():
                    raise ValueError("Coordinator mount must be a directory")
                (self.root / "fs" / "coordination").mkdir(exist_ok=True)
                self._argv += ["--bind", f"{notebook_dir}:/coordination:ro"]
                self._checkpoint_mounts["/coordination"] = notebook_dir
            self._argv += ["--pwd", "/", str(self.root / "fs")]
            result = self.execute("git rev-parse HEAD")
            if result.exit_code:
                raise RuntimeError(f"Task repository unavailable: {result.stdout}")
            self._base_commit = result.stdout.strip()
        except BaseException:
            self.cleanup()
            raise

    def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        # Serialize commands within one writable filesystem, including Git sync.
        with self._lock:
            if self._closed:
                raise RuntimeError("Apptainer environment is closed")
            marker = f"__COOPER_STARTED_{uuid.uuid4().hex}__\n"
            script = (
                f"printf %s {shlex.quote(marker)}\n"
                f'cd {shlex.quote(self.repo_path)} || {{ echo "__COOPER_REPO_UNAVAILABLE__"; exit 125; }}\n{command}'
            )
            result, _ = run_limited(
                [*self._argv, "bash", "-s"],
                input_text=script,
                timeout=timeout,
                env=self._host_env,
                new_session=True,
            )
            if marker not in result.stdout:
                raise RuntimeError("Apptainer command failed before container shell startup")
            result = ExecResult(result.stdout.replace(marker, "", 1), result.exit_code)
            if result.exit_code == 125 and "__COOPER_REPO_UNAVAILABLE__" in result.stdout:
                raise RuntimeError("Apptainer task repository is unavailable")
            return result

    def read_file(self, path: str) -> str:
        result = self.execute(f"cat -- {shlex.quote(path)}")
        return result.stdout if result.exit_code == 0 else ""

    def write_file(self, path: str, content: str) -> None:
        encoded = base64.b64encode(content.encode()).decode()
        parent = shlex.quote(str(Path(path).parent))
        result = self.execute(f"mkdir -p -- {parent} && printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}")
        if result.exit_code:
            raise RuntimeError(result.stdout)

    def git_diff(self) -> str:
        from cooperagents.checkpoint import complete_output

        complete_output(self.execute("git add -A"), "Stage integrated patch")
        result = self.execute(f"git diff --cached --binary {shlex.quote(self._base_commit)}")
        complete_output(result, "Export integrated patch")
        stashes = complete_output(self.execute("git stash list"), "Read patch stashes")
        if not result.stdout.strip() and stashes.strip():
            complete_output(
                self.execute("git stash pop -q 2>/dev/null || git checkout stash@{0} -- . 2>/dev/null; git add -A"), "Recover stashed patch"
            )
            result = self.execute(f"git diff --cached --binary {shlex.quote(self._base_commit)}")
        return complete_output(result, "Export integrated patch")

    @classmethod
    def from_checkpoint(cls, checkpoint: Path, *, scratch: Path, base_image: Path | None = None) -> ApptainerEnv:
        """Verify and restore into a fresh owned sandbox, without rebuilding a SIF."""
        from cooperagents.checkpoint import complete_output, preview_patch, restore_tree, verify_checkpoint

        state = verify_checkpoint(checkpoint)
        runtime = state["runtime"]
        repo = Path(state["repo_path"])
        if runtime["backend"] != "apptainer" or not repo.is_absolute() or ".." in repo.parts:
            raise ValueError("Checkpoint requires an Apptainer rootfs and absolute repository path")
        if not re.fullmatch(r"[0-9a-f]{40}", state["base_commit"]):
            raise ValueError("Checkpoint has no valid original base commit")
        mounts = runtime.get("mounts", [])
        targets = [m["target"] for m in mounts]
        if len(set(targets)) != len(targets) or any(t not in {"/cbshared", "/coordination"} for t in targets):
            raise ValueError("Unsupported or duplicate checkpoint mount target")
        if any(type(m["readonly"]) is not bool or m["readonly"] != (m["target"] == "/coordination") for m in mounts):
            raise ValueError("Unsupported checkpoint mount access mode")
        image, image_hash = runtime.get("image"), runtime.get("image_sha256")
        if (
            not isinstance(image, str)
            or not Path(image).is_absolute()
            or not isinstance(image_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", image_hash)
        ):
            raise ValueError("Checkpoint requires valid original image provenance")
        scratch.mkdir(parents=True, exist_ok=True)
        env = cls.__new__(cls)
        env.root = Path(tempfile.mkdtemp(prefix="ca-repair-", dir=scratch))
        env.repo_path = str(repo)
        env.image = base_image if base_image is not None else Path(image)
        env._image_sha256 = image_hash
        env._checkpoint_mounts = {}
        env._lock = threading.RLock()
        env._closed = False
        env._base_commit = state["base_commit"]
        env._host_env = {
            k: v
            for k, v in os.environ.items()
            if k
            in (
                "PATH",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "APPTAINER_CACHEDIR",
                "APPTAINER_TMPDIR",
            )
        }
        env._host_env["HOME"] = str(env.root / "host-home")
        try:
            (env.root / "host-home").mkdir()
            if runtime.get("format") == "checkpoint-tar-delta-v1":
                base = cls._base_filesystem(env.image, scratch, image_hash)
            else:
                base = env.root
            restore_tree(checkpoint, runtime, base, env.root / "fs")
            env._argv = [
                "apptainer",
                "exec",
                "--writable",
                "--containall",
                "--cleanenv",
                "--no-mount",
                "hostfs,bind-paths",
                "--bind",
                "/etc/resolv.conf:/etc/resolv.conf:ro",
            ]
            for directory, target in (("home/agent", "/home/agent"), ("tmp", "/tmp"), ("var/tmp", "/var/tmp")):
                source = env.root / "fs" / directory
                if source.resolve().is_relative_to(env.root / "fs") is False:
                    raise ValueError("Runtime directory escapes restored rootfs")
                source.mkdir(parents=True, exist_ok=True)
                env._argv += ["--home" if target == "/home/agent" else "--bind", f"{source}:{target}"]
            empty_base = env.root / "empty-base"
            empty_base.mkdir()
            for i, mount in enumerate(mounts):
                source = env.root / f"mount-{i}"
                restore_tree(checkpoint, mount, empty_base, source)
                env._checkpoint_mounts[mount["target"]] = source
                env._argv += ["--bind", f"{source}:{mount['target']}" + (":ro" if mount["readonly"] else "")]
            env._argv += ["--pwd", "/", str(env.root / "fs")]
            complete_output(env.execute(f"git cat-file -e {env._base_commit}^{{commit}}"), "Validate original base")
            for name, command in (
                ("head", "git rev-parse HEAD"),
                ("status", "git status --porcelain=v1 --untracked-files=all"),
                ("stashes", "git stash list"),
            ):
                observed = complete_output(env.execute(f"GIT_OPTIONAL_LOCKS=0 {command}"), f"Restore Git {name}")
                if observed != state["git"][name]["stdout"]:
                    raise ValueError(f"Restored Git {name} differs from checkpoint")
            if preview_patch(env).encode() != (checkpoint / "raw.patch").read_bytes():
                raise ValueError("Restored patch differs from checkpoint")
            return env
        except BaseException:
            env.cleanup()
            raise

    def recover_shared_diff(self, agent_id: str) -> str:
        # Run outside the repo in case the worker removed its working directory.
        with self._lock:
            cwd, self.repo_path = self.repo_path, "/"
            try:
                result = self.execute(
                    f"git --git-dir=/cbshared/repo.git diff --binary {shlex.quote(self._base_commit)} {shlex.quote(agent_id)}"
                )
                return result.stdout if result.exit_code == 0 else ""
            finally:
                self.repo_path = cwd

    def cleanup(self) -> None:
        from cooperagents.checkpoint import remove_tree

        with self._lock:
            self._closed = True
            if not self.root.exists():
                return
            remove_tree(self.root)

    @staticmethod
    def _base_filesystem(image: Path, scratch: Path, expected_hash: str) -> Path:
        from cooperagents.checkpoint import sha256
        from cooperagents.checkpoint_delta import cached_base

        if not image.is_file() or sha256(image) != expected_hash:
            raise ValueError("Checkpoint base SIF is missing or has a different SHA256; supply the matching base_image")

        def build(destination):
            subprocess.run(
                ["apptainer", "build", "--fix-perms", "--sandbox", str(destination), str(image)],
                check=True,
                capture_output=True,
                timeout=300,
            )
            inspected = subprocess.run(
                ["apptainer", "inspect", "--json", str(image)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            labels = json.loads(inspected.stdout).get("data", {}).get("attributes", {}).get("labels", {})
            provenance = {key: labels.get(key) for key in ("org.opencontainers.image.base.name", "org.opencontainers.image.base.digest")}
            if any(value is not None and not isinstance(value, str) for value in provenance.values()):
                raise ValueError("Invalid SIF base image labels")
            (destination.parent / "image.json").write_text(json.dumps(provenance))

        return cached_base(scratch / ".checkpoint-bases", expected_hash, build)

    def checkpoint(self, destination: Path) -> dict:
        from cooperagents.checkpoint import save_tree_delta, sha256

        with self._lock:
            if self._closed:
                raise RuntimeError("Apptainer environment is closed")
            image_hash = getattr(self, "_image_sha256", None) or sha256(self.image)
            base = self._base_filesystem(self.image, self.root.parent, image_hash)
            provenance_file = base.parent / "image.json"
            provenance = json.loads(provenance_file.read_text()) if provenance_file.is_file() else {}
            filesystem = save_tree_delta(self.root / "fs", base, destination, "rootfs")
            mounts = []
            with tempfile.TemporaryDirectory(dir=self.root.parent) as temporary:
                for i, (target, source) in enumerate(self._checkpoint_mounts.items()):
                    record = save_tree_delta(source, Path(temporary), destination, f"mount-{i}")
                    mounts.append(dict(target=target, readonly=target == "/coordination", base="empty", **record))
            return dict(
                backend="apptainer",
                **filesystem,
                image=str(self.image),
                image_sha256=image_hash,
                base_image_name=provenance.get("org.opencontainers.image.base.name"),
                registry_digest=provenance.get("org.opencontainers.image.base.digest"),
                mounts=mounts,
            )
