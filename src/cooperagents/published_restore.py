"""Official repair restoration; historical catalogs and producers are data only."""

from __future__ import annotations

import time
from pathlib import Path

from cooperagents.checkpoint import remove_tree
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.image_base import ImageBase
from cooperagents.env.published_holder import MountHolder
from cooperagents.published import PublicationError, PublishedCheckpoint, _manifest, file_hash
from cooperagents.workers.mini_swe_worker import MiniSweEnvAdapter


def validate_checkpoint(reference: PublishedCheckpoint, checkpoint: Path, purpose: str | None) -> None:
    if purpose != "difficulty":
        raise PublicationError("published repair requires explicit difficulty purpose")
    root = Path(reference.publication.path)
    header = _manifest(root, reference)
    if checkpoint.resolve() != (root / "checkpoint").resolve() or checkpoint.is_symlink():
        raise PublicationError("repair checkpoint differs from publication")
    if file_hash(checkpoint / "manifest.json") != reference.manifest_sha256:
        raise PublicationError("repair checkpoint manifest identity mismatch")
    if header["metadata"]["legacy_manifest_sha256"] != reference.manifest_sha256:
        raise PublicationError("publication/native manifest identity mismatch")


def restore_environment(
    reference: PublishedCheckpoint,
    checkpoint: Path,
    *,
    scratch: Path,
    base_image: Path | None = None,
    image_base: ImageBase | None = None,
    requested: float | None = None,
) -> ApptainerEnv:
    started = requested if requested is not None else time.monotonic()
    owned = image_base is None
    base = image_base or ImageBase.prepare(base_image or Path(reference.image), reference.image_sha256, scratch)
    try:
        if base.proof["image_sha256"] != reference.image_sha256:
            raise PublicationError("shared image capability belongs to a different SIF")
        return _restore_with_base(reference, checkpoint, scratch, base, started, owned)
    except BaseException:
        if owned:
            base.close()
        raise


def _restore_with_base(reference, checkpoint, scratch, base, started, owned) -> ApptainerEnv:
    holder = None
    try:
        holder = MountHolder(reference, scratch, base)

        def close() -> None:
            try:
                holder.close()
            finally:
                if owned:
                    base.close()

        env = ApptainerEnv.from_filesystem(
            holder.root, holder.ready["state"], exec_prefix=holder.exec_prefix(), cleanup=close, checkpoint=checkpoint
        )
        try:
            probe = _probe(env)
            env.restore_metrics = _metrics(reference, holder, base, started, owned, probe)
            return env
        except BaseException:
            env.cleanup()
            raise
    except BaseException:
        if holder is not None:
            holder.close()
            if holder.root.exists():
                remove_tree(holder.root)
        if owned:
            base.close()
        raise


def _metrics(reference, holder, base, started, owned, probe) -> dict:
    from cooperagents.published_profiles import reader_identity

    duration = time.monotonic() - started
    return dict(
        restore_mode=reference.version,
        publication_format=reference.format,
        publication_sha256=reference.publication.sha256,
        reader_identity=reader_identity(),
        restore_seconds=duration,
        restore_seconds_excluding_base_preparation=duration - base.proof["preparation_seconds"] if owned else duration,
        base_prepared_this_call=owned,
        base_preparation=base.proof,
        restore_first_command=probe,
        restore_risks=holder.ready["risks"],
        training_eligible=False,
        repair_validated=False,
    )


def _probe(env: ApptainerEnv) -> dict:
    probe = MiniSweEnvAdapter(env, timeout=300).execute({"command": "test -e .git && printf 'QUALIFIED_READY\\n'"})
    if probe["returncode"] != 0 or "QUALIFIED_READY" not in probe["output"]:
        raise PublicationError("published repair command probe failed")
    return probe
