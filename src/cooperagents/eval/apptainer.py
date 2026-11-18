"""Container adapter for the pinned CooperBench evaluator (scoring stays upstream)."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from cooperagents.checkpoint import sha256
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.image_base import ImageBase
from cooperagents.env.runtime_cache import RuntimeCache, RuntimeCacheArtifact
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
    ) -> None:
        if (image_base is None) != (shared_image is None):
            raise ValueError("shared evaluator requires image identity and capability together")
        if shared_image is not None and (not Path(shared_image[0]).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", shared_image[1])):
            raise ValueError("invalid shared evaluator image identity")
        self.images, self.scratch = images, scratch
        self.image_base, self.shared_image = image_base, shared_image
        self.runtime_caches = runtime_caches or {}
        self.image_manifest_sha256 = image_manifest_sha256
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
            if cache is None:
                env = ApptainerEnv.from_image_base(path, checksum, scratch=Path(self.scratch), base=self.image_base, workdir=workdir)
            else:
                task = (cache.identity.repo, cache.identity.task_id) if cache.identity else None
                env = ApptainerEnv.from_image_base(path, checksum, scratch=Path(self.scratch), base=self.image_base,
                                                  workdir=workdir, runtime_cache=cache, cache_task=task)
        check_base = self.image_base is not None or (cache is not None and env.runtime_cache_receipt.get("status") == "hit")
        return EvalSandbox(env, timeout, check_base=check_base)

    def _cache_for(self, image: str) -> RuntimeCache | None:
        cache = self.runtime_caches.get(image)
        if cache is None:
            return None
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


def main() -> None:
    import argparse
    import json
    import os
    from pathlib import Path

    from cooperbench.eval import evaluate
    from cooperbench.eval.runs import discover_runs

    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--logs", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--shared-image-path")
    parser.add_argument("--shared-image-sha256")
    parser.add_argument("--runtime-cache-catalog", type=Path)
    args = parser.parse_args()
    if os.environ.get("COOPER_REPAIR_BASE_FD"):
        raise ValueError("shared evaluator requires channel transport, not inherited descriptors")
    runs = discover_runs(run_name=args.run, logs_dir=args.logs, dataset_dir=args.dataset)
    if not runs:
        raise RuntimeError("Official evaluator discovered no runs")
    if bool(args.shared_image_path) != bool(args.shared_image_sha256):
        raise ValueError("complete shared evaluator image identity required")
    if os.environ.get("COOPER_REPAIR_BASE_CHANNEL") and not args.shared_image_path:
        raise ValueError("shared image controller requires an explicit evaluation image identity")
    with ExitStack() as stack:
        base = None
        identity = None
        if args.shared_image_path:
            identity = (args.shared_image_path, args.shared_image_sha256)
            base = ImageBase.from_environment(args.shared_image_sha256)
            if base is None:
                raise ValueError("shared evaluator requires the prepared image controller")
            stack.enter_context(base)
            base.validate(args.shared_image_sha256)
        caches = {}
        if args.runtime_cache_catalog is not None:
            try:
                catalog = json.loads(args.runtime_cache_catalog.read_text())
                if not isinstance(catalog, dict):
                    raise ValueError("cache catalog must map canonical image names to pinned artifact locators")
                for image, locator in catalog.items():
                    try:
                        artifact = RuntimeCacheArtifact.model_validate_json(json.dumps(locator))
                        caches[image] = stack.enter_context(artifact.open())
                    except ValueError as exc:
                        logging.getLogger(__name__).warning("uv runtime cache fallback for %s: %s", image, exc)
            except (OSError, ValueError) as exc:
                logging.getLogger(__name__).warning("uv runtime cache catalog fallback: %s", exc)
        manifest = Path(os.environ["COOPER_IMAGE_MANIFEST"])
        backend = ApptainerEvalBackend(
            json.loads(manifest.read_text()),
            os.environ["COOPER_SCRATCH"],
            image_base=base,
            shared_image=identity,
            runtime_caches=caches,
            image_manifest_sha256=sha256(manifest) if args.runtime_cache_catalog is not None else None,
        )
        stack.callback(backend.close)
        evaluate(args.run, backend=backend, dataset_dir=args.dataset, logs_dir=args.logs, concurrency=args.concurrency, force=True)
        for run in runs:
            result = json.loads((Path(run["log_dir"]) / "eval.json").read_text())
            if result.get("error") or not all(result.get(f"feature{i}") for i in (1, 2)):
                raise RuntimeError(f"Official evaluation failed: {result.get('error')}")


if __name__ == "__main__":
    main()
