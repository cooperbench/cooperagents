"""Container adapter for the pinned CooperBench evaluator (scoring stays upstream)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.image_base import ImageBase
from cooperagents.env.image_channel import receive_image
from cooperagents.env.runtime_cache import CacheReceipt, RuntimeCache, RuntimeCacheArtifact
from cooperagents.eval.dataset import image_name


@dataclass
class EvalResult:
    returncode: int
    output: str

    def stdout_read(self) -> str:
        return self.output

    def stderr_read(self) -> str:
        return ""


class EvalSandbox:
    def __init__(self, env: ApptainerEnv, timeout: int, *, check_base: bool = False) -> None:
        self.env, self.timeout = env, timeout
        self.check_base = check_base

    def exec(self, *args: str) -> EvalResult:
        result = self.env.execute(shlex.join(args), timeout=self.timeout)
        if self.check_base:
            health = self.env.execute("printf EVALUATOR_BASE_OK", timeout=30)
            if health.exit_code != 0 or health.stdout != "EVALUATOR_BASE_OK":
                raise RuntimeError("evaluator image base unavailable after command")
        return EvalResult(result.exit_code, result.stdout)

    def terminate(self) -> None:
        self.env.cleanup()


class ApptainerEvalBackend:
    def __init__(
        self, images: dict[str, str], scratch: str, *, image_base: ImageBase | None = None, shared_image: tuple[str, str] | None = None,
        runtime_caches: dict[str, RuntimeCache] | None = None, image_manifest_sha256: str | None = None,
        unmatched_cache_reason: str | None = None,
    ) -> None:
        if (image_base is None) != (shared_image is None):
            raise ValueError("shared evaluator requires image identity and capability together")
        if shared_image is not None and (not Path(shared_image[0]).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", shared_image[1])):
            raise ValueError("invalid shared evaluator image identity")
        self.images, self.scratch = images, scratch
        self.image_base, self.shared_image = image_base, shared_image
        self.runtime_caches = runtime_caches or {}
        self.image_manifest_sha256 = image_manifest_sha256
        self._unmatched_cache_reason = unmatched_cache_reason
        self.runtime_cache_receipts: list[dict] = []
        self._receipts_lock = threading.Lock()
        self._cache_bases: dict[tuple[Path, str], ImageBase] = {}
        self._cache_base_errors: dict[tuple[Path, str], str] = {}
        self._base_lock = threading.Lock()

    def create_sandbox(self, image: str, timeout: int = 600, workdir: str = "/workspace") -> EvalSandbox:
        path = Path(self.images[image])
        cache = self._cache_for(image)
        if self.image_base is None and cache is not None:
            env = self._cached_environment(path, cache, workdir)
        elif self.image_base is None:
            env = ApptainerEnv(str(path), scratch=self.scratch)
            env.repo_path = workdir
        else:
            assert self.shared_image is not None
            expected_path, checksum = self.shared_image
            if path.resolve() != Path(expected_path).resolve():
                raise ValueError("evaluator requested a different image from shared base")
            if cache is None or cache.descriptor < 0:
                env = ApptainerEnv.from_image_base(path, checksum, scratch=Path(self.scratch), base=self.image_base, workdir=workdir)
                if cache is not None:
                    reason = cache.proof.get("reason", "cache closed or unavailable")
                    try:
                        env._record_runtime_cache(dict(status="fallback", reason=reason))
                    except BaseException:
                        env.cleanup()
                        raise
            else:
                task = (cache.identity.repo, cache.identity.task_id) if cache.identity else None
                env = ApptainerEnv.from_image_base(
                    path, checksum, scratch=Path(self.scratch), base=self.image_base,
                    workdir=workdir, runtime_cache=cache, cache_task=task,
                )
        if cache is not None:
            with self._receipts_lock:
                self.runtime_cache_receipts.append(env.runtime_cache_receipt)
        check_base = self.image_base is not None or (cache is not None and env.runtime_cache_receipt.get("status") == "hit")
        return EvalSandbox(env, timeout, check_base=check_base)

    def _cache_for(self, image: str) -> RuntimeCache | None:
        cache = self.runtime_caches.get(image)
        if cache is None:
            if self._unmatched_cache_reason is None:
                return None
            return RuntimeCache(-1, {"reason": self._unmatched_cache_reason})
        if cache.descriptor < 0 and cache.proof.get("reason"):
            return cache
        identity = cache.identity
        if identity is None or image != image_name(identity.repo, identity.task_id):
            return RuntimeCache(-1, {"reason": "evaluation image does not match cache repo/task"}, identity=identity)
        if self.image_manifest_sha256 is not None and identity.image_manifest_sha256 != self.image_manifest_sha256:
            return RuntimeCache(-1, {"reason": "evaluation image manifest differs from cache"}, identity=identity)
        return cache

    def _cached_environment(self, path: Path, cache: RuntimeCache, workdir: str) -> ApptainerEnv:
        try:
            config = cache.transport()
            if config["descriptor"] < 0 or cache.identity is None:
                raise ValueError(config["proof"].get("reason", "cache has no environment identity"))
            checksum = cache.identity.image_sha256
            base = self._cache_image_base(path, checksum)
            env = ApptainerEnv.from_image_base(
                path, checksum, scratch=Path(self.scratch), base=base, workdir=workdir, runtime_cache=cache,
                cache_task=(cache.identity.repo, cache.identity.task_id),
            )
            if env.runtime_cache_receipt["status"] == "hit":
                return env
            reason = env.runtime_cache_receipt["reason"]
            env.cleanup()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        # A cache miss uses the original constructor, including its original
        # argv. Cache-hit sandboxes borrow the backend's admitted image base.
        env = ApptainerEnv(str(path), scratch=self.scratch)
        env.repo_path = workdir
        env._record_runtime_cache(dict(status="fallback", reason=reason))
        return env

    def _cache_image_base(self, path: Path, checksum: str) -> ImageBase:
        # SIF hashing/packing/audit happen once per backend admission, not once
        # per sandbox. Hold the seal for all borrowers; path replacement cannot
        # change the authenticated bytes they mount.
        key = (path.resolve(), checksum)
        with self._base_lock:
            if key in self._cache_base_errors:
                raise ValueError(self._cache_base_errors[key])
            if key not in self._cache_bases:
                try:
                    self._cache_bases[key] = ImageBase.prepare(path, checksum, Path(self.scratch))
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    self._cache_base_errors[key] = f"{type(exc).__name__}: {exc}"
                    raise
            return self._cache_bases[key]

    def close(self) -> None:
        """Release owned admissions after all evaluation sandboxes terminate."""
        with self._base_lock:
            for base in self._cache_bases.values():
                base.close()
            self._cache_bases.clear()


def _read_cache_metadata(path: Path) -> bytes:
    limit = 64 * 1024
    # A nonblocking open lets fstat reject special files without waiting on them.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"{path.name} must be a regular file of at most 64 KiB")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds 64 KiB")
    return data


def _borrow_runtime_cache(
    channel: str, locator_file: Path, manifest: bytes, *, shared_image_sha256: str | None = None,
) -> RuntimeCache:
    identity = None
    with ExitStack() as stack:
        try:
            locator = RuntimeCacheArtifact.model_validate_json(_read_cache_metadata(locator_file))
            identity = locator.identity
            receipt_bytes = _read_cache_metadata(locator.directory / "receipt.json")
            if hashlib.sha256(receipt_bytes).hexdigest() != locator.receipt_sha256:
                raise ValueError("cache receipt digest mismatch")
            receipt = CacheReceipt.model_validate_json(receipt_bytes)
            if receipt.identity != identity:
                raise ValueError("cache receipt does not match locator identity")

            descriptor, proof = receive_image(channel)
            cache = stack.enter_context(RuntimeCache(descriptor, proof, identity=identity))
            if CacheReceipt.model_validate(proof.get("receipt")) != receipt:
                raise ValueError("channel receipt does not match pinned receipt")
            transport = cache.transport()
            if transport["descriptor"] < 0:
                raise ValueError(transport["proof"]["reason"])
            if os.fstat(descriptor).st_size != receipt.size_bytes:
                raise ValueError("cache descriptor size does not match receipt")
            if identity.image_manifest_sha256 != hashlib.sha256(manifest).hexdigest():
                raise ValueError("evaluation image manifest differs from cache")
            if shared_image_sha256 is not None and identity.image_sha256 != shared_image_sha256:
                raise ValueError("evaluation shared image digest differs from cache")

            # Transfer the validated descriptor to the caller.
            stack.pop_all()
            return cache
        except (OSError, ValueError) as exc:
            return RuntimeCache(-1, {"reason": f"{type(exc).__name__}: {exc}"}, identity=identity)


def _write_cache_receipts(path: Path, receipts: list[dict]) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(receipts, stream, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--logs", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--shared-image-path")
    parser.add_argument("--shared-image-sha256")
    parser.add_argument("--runtime-cache-channel")
    parser.add_argument("--runtime-cache-locator", type=Path)
    parser.add_argument("--runtime-cache-receipts", type=Path)
    args = parser.parse_args()
    cache_args = (args.runtime_cache_channel, args.runtime_cache_locator, args.runtime_cache_receipts)
    if any(arg is not None for arg in cache_args) and not all(cache_args):
        parser.error("runtime cache requires --runtime-cache-channel, --runtime-cache-locator and --runtime-cache-receipts")
    if os.environ.get("COOPER_REPAIR_BASE_FD") or os.environ.get("COOPER_REPAIR_FD"):
        raise ValueError("shared evaluator requires channel transport, not inherited descriptors")
    if bool(args.shared_image_path) != bool(args.shared_image_sha256):
        raise ValueError("complete shared evaluator image identity required")
    if os.environ.get("COOPER_REPAIR_BASE_CHANNEL") and not args.shared_image_path:
        raise ValueError("shared image controller requires an explicit evaluation image identity")

    from cooperbench.eval import evaluate
    from cooperbench.eval.runs import discover_runs

    receipts: list[dict] = []
    try:
        runs = discover_runs(run_name=args.run, logs_dir=args.logs, dataset_dir=args.dataset)
        if not runs:
            raise RuntimeError("Official evaluator discovered no runs")
        with ExitStack() as stack:
            base = None
            shared_image = None
            if args.shared_image_path:
                shared_image = (args.shared_image_path, args.shared_image_sha256)
                base = ImageBase.from_environment(args.shared_image_sha256)
                if base is None:
                    raise ValueError("shared evaluator requires the prepared image controller")
                stack.enter_context(base)
                base.validate(args.shared_image_sha256)

            manifest = Path(os.environ["COOPER_IMAGE_MANIFEST"]).read_bytes()
            caches = {}
            manifest_sha256 = None
            unmatched_cache_reason = None
            if args.runtime_cache_channel is not None:
                cache = stack.enter_context(
                    _borrow_runtime_cache(
                        args.runtime_cache_channel, args.runtime_cache_locator, manifest,
                        shared_image_sha256=args.shared_image_sha256,
                    )
                )
                manifest_sha256 = hashlib.sha256(manifest).hexdigest()
                if cache.identity is not None:
                    caches[image_name(cache.identity.repo, cache.identity.task_id)] = cache
                    unmatched_cache_reason = "evaluation image does not match cache repo/task"
                else:
                    unmatched_cache_reason = cache.proof["reason"]

            backend = ApptainerEvalBackend(
                json.loads(manifest),
                os.environ["COOPER_SCRATCH"],
                image_base=base,
                shared_image=shared_image,
                runtime_caches=caches,
                image_manifest_sha256=manifest_sha256,
                unmatched_cache_reason=unmatched_cache_reason,
            )
            stack.callback(backend.close)
            if args.runtime_cache_receipts is not None:
                receipts = backend.runtime_cache_receipts
            evaluate(args.run, backend=backend, dataset_dir=args.dataset, logs_dir=args.logs, concurrency=args.concurrency, force=True)
            for run in runs:
                result = json.loads((Path(run["log_dir"]) / "eval.json").read_text())
                if result.get("error") or not all(result.get(f"feature{i}") for i in (1, 2)):
                    raise RuntimeError(f"Official evaluation failed: {result.get('error')}")
    finally:
        if args.runtime_cache_receipts is not None:
            _write_cache_receipts(args.runtime_cache_receipts, receipts)


if __name__ == "__main__":
    main()
