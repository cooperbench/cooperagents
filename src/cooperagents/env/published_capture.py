"""Direct repair-only V3 capture. Persistent output is metadata; payload stays node-local."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from cooperagents.checkpoint import complete_output, preview_patch, remove_tree, sha256
from cooperagents.env.capture_archives import archive, check_xattrs, prepared_mounts, publication_root, signatures, write_index, write_json
from cooperagents.env.capture_layers import select_layer
from cooperagents.env.image_base import ImageBase
from cooperagents.env.published_audit import inventory
from cooperagents.env.published_holder import MountHolder
from cooperagents.patching import strip_for_submission, strip_test_sections
from cooperagents.published import V3, PublishedCheckpoint, runtime_qualification
from cooperagents.published_profiles import validate_reader


def git_state(env) -> dict:
    result = {}
    for name, command in [
        ("head", "git rev-parse HEAD"),
        ("status", "git status --porcelain=v1 --untracked-files=all"),
        ("stashes", "git stash list"),
    ]:
        value = env.execute("GIT_OPTIONAL_LOCKS=0 " + command)
        complete_output(value, "capture Git " + name)
        result[name] = dataclasses.asdict(value)
    return result


def files(root: Path) -> dict:
    return {str(p.relative_to(root)): dict(bytes=p.stat().st_size, sha256=sha256(p)) for p in sorted(root.rglob("*")) if p.is_file()}


def _roots(env) -> dict[str, Path]:
    roots = {"rootfs": env.root / "fs", **env._checkpoint_mounts}
    seen = {}
    for label, root in roots.items():
        if root.is_symlink():
            raise ValueError("capture root is a symlink: " + label)
        for path in [root, *root.rglob("*")]:
            info = path.lstat()
            if (info.st_uid, info.st_gid) != (os.getuid(), os.getgid()):
                raise ValueError("unsupported capture ownership: " + str(path.relative_to(root)))
            if path.is_file() and not path.is_symlink():
                old = seen.setdefault((info.st_dev, info.st_ino), label)
                if old != label:
                    raise ValueError("hardlink crosses capture mount boundary")
    return roots


def _layers(env, base: Path, bundle: Path) -> dict:
    root = env.root / "fs"
    repo = env.repo_path.lstrip("/")
    target = inventory(root)
    check_xattrs(target)
    if target["entries"].get(repo, {}).get("type") != "dir":
        raise ValueError("capture requires a real private repo")
    before = inventory(base)
    layer = select_layer(before, target, repo)
    # Generated image metadata differs between identical SIF expansions.
    for name in target["entries"]:
        if any(
            name == p or name.startswith(p + "/")
            for p in [".singularity.d/Singularity", ".singularity.d/labels.json", ".singularity.d/bootstrap_history"]
        ):
            layer["entries"][name] = target["entries"][name]
    groups = [
        g for g in target["hardlink_groups"] if any(n.startswith(repo + "/") for n in g) and any(not n.startswith(repo + "/") for n in g)
    ]
    private = {n for g in groups for n in g if not n.startswith(repo + "/")}
    selected = set(layer["entries"]) - private
    for n in list(selected):
        selected.update(str(p) for p in Path(n).parents if str(p) != ".")
    archive(root, bundle / "delta.tar", target, selected)
    archive(root / repo, bundle / "repo.tar", inventory(root / repo))
    write_index(bundle / "target-index.json.gz", target)
    mounts, runtime = prepared_mounts(env._checkpoint_mounts, bundle)
    parents = {str(p) for n in private for p in Path(n).parents if str(p) != "."}
    return dict(
        repo=repo,
        repo_parent_metadata={str(p): target["entries"][str(p)] for p in Path(repo).parents if str(p) != "."},
        private_link_groups=groups,
        private_parent_metadata={p: target["entries"][p] for p in parents},
        target_inventory_sha256=sha256(bundle / "target-index.json.gz"),
        delta_sha256=sha256(bundle / "delta.tar"),
        repo_sha256=sha256(bundle / "repo.tar"),
        prepared_mounts=mounts,
        runtime_mounts=runtime,
    )


def _metadata(env, checkpoint: Path, destination: Path, metadata: dict, repair_input, git: dict, mounts: list, image_hash: str) -> str:
    checkpoint.mkdir()
    run = destination.parent / "run.json"
    if run.is_file():
        shutil.copyfile(run, checkpoint / "run.json")
    (checkpoint / "repair-input.json").write_text(repair_input.model_dump_json(indent=2))
    raw = preview_patch(env)
    patch = strip_test_sections(raw)
    (checkpoint / "raw.patch").write_text(raw)
    (checkpoint / "integration.patch").write_text(patch)
    (checkpoint / "submission.patch").write_text(strip_for_submission(patch))
    write_json(
        checkpoint / "state.json",
        dict(
            version=3,
            started_at=datetime.now(UTC).isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            repo_path=env.repo_path,
            base_commit=env._base_commit,
            runtime=dict(backend="apptainer", format=V3, image=str(env.image), image_sha256=image_hash, mounts=mounts),
            git=git,
            patch_source="working_tree",
            metadata=metadata,
            process_state_saved=False,
        ),
    )
    write_json(checkpoint / "manifest.json", dict(version=3, files=files(checkpoint)))
    return patch


def _reference(bundle: Path, key: str, image: Path, image_hash: str, *, publication_hash: str | None = None) -> PublishedCheckpoint:
    return PublishedCheckpoint.from_dict(
        dict(
            id="capture-" + key[:24],
            node=socket.gethostname(),
            version="v3",
            format=V3,
            publication=dict(path=str(bundle), key=key, sha256=publication_hash or sha256(bundle / "publication.json")),
            legacy_manifest_sha256=key,
            image=str(image),
            image_sha256=image_hash,
            training_eligible=False,
            repair_validated=False,
        )
    )


def _seal(bundle: Path) -> None:
    for path in [*bundle.rglob("*"), bundle]:
        path.chmod(0o500 if path.is_dir() else 0o400)


def save(env, destination: Path, *, metadata: dict, repair_input, trace=None) -> str:
    """Capture only at the serialized repair start boundary; never overwrite a publication."""
    validate_reader()
    if metadata.get("boundary") != "repair_agent_start" or repair_input is None:
        raise ValueError("direct V3 requires repair_agent_start and embedded repair input")
    if getattr(env, "_restore_cleanup", None) is not None:
        raise ValueError("capture of resumed overlay environments is not yet supported")
    with env._lock:
        if env._closed:
            raise RuntimeError("capture environment is closed")
        destination.mkdir(parents=True, mode=0o700, exist_ok=False)
        staging = None
        try:
            output = publication_root(env.root.parent)
            staging = Path(tempfile.mkdtemp(prefix=".capture-", dir=output))
            os.chown(staging, -1, os.getgid())
            staging.chmod(0o700)
            return _save(env, destination, metadata, repair_input, trace, staging)
        except BaseException as error:
            write_json(destination / "capture-failure.json", dict(error_type=type(error).__name__, error=str(error), complete=False))
            raise
        finally:
            if staging is not None:
                remove_tree(staging)


def _save(env, destination, metadata, repair_input, trace, staging):
    started = time.monotonic()
    image_hash = sha256(env.image)
    from cooperagents.published_profiles import validate_capture, validate_direct_writer

    qualification = validate_direct_writer()
    validate_capture(repair_input.source_hashes, "captured-direct-v3-v1")
    if trace:
        trace("checkpoint_start", path=str(destination), boundary=metadata["boundary"])
    roots = _roots(env)
    git = git_state(env)
    before = {name: signatures(root) for name, root in roots.items()}
    output = staging.parent
    bundle = staging / "bundle"
    bundle.mkdir()
    base_path = env._base_filesystem(env.image, env.root.parent, image_hash)
    layers = _layers(env, base_path, bundle)
    if before != {name: signatures(root) for name, root in roots.items()}:
        raise ValueError("capture source changed during archive creation")
    if {k: v["stdout"] for k, v in git.items()} != {k: v["stdout"] for k, v in git_state(env).items()}:
        raise ValueError("Git state changed during capture")
    patch = _metadata(env, bundle / "checkpoint", destination, metadata, repair_input, git, layers.pop("runtime_mounts"), image_hash)
    key = sha256(bundle / "checkpoint/manifest.json")
    from cooperagents.env.capture_risks import assess

    risks = assess(roots, {name: inventory(root) for name, root in roots.items()})
    write_json(bundle / "risks.json", risks)
    receipt = dict(
        format=V3,
        producer="direct-capture-v1",
        producer_identity=qualification["producer_identity"],
        profile="captured-direct-v3-v1",
        manifest_sha256=key,
        image_sha256=image_hash,
        runtime_qualification=runtime_qualification(),
        training_eligible=False,
        risk_sha256=sha256(bundle / "risks.json"),
        **layers,
    )
    write_json(bundle / "receipt.json", receipt)
    owned = False
    base = ImageBase.from_environment(image_hash)
    if base is None:
        base = ImageBase.prepare(env.image, image_hash, env.root.parent)
        owned = True
    published = None
    complete = False
    try:
        audit = MountHolder.audit_capture(bundle, env.root.parent, base)
        try:
            receipt["composition_audit"] = audit.ready
        finally:
            try:
                audit.close()
            finally:
                remove_tree(audit.root)
        receipt["capture_seconds_before_publication"] = time.monotonic() - started
        write_json(bundle / "receipt.json", receipt)
        write_json(
            bundle / "publication.json",
            dict(
                format=V3,
                key=key,
                state="filesystem-verified-repair-unverified",
                files=files(bundle),
                metadata=dict(
                    profile=receipt["profile"],
                    legacy_manifest_sha256=key,
                    image_sha256=image_hash,
                    receipt_sha256=sha256(bundle / "receipt.json"),
                    repair_validated=False,
                    training_eligible=False,
                ),
            ),
        )
        candidate = staging / key
        bundle.rename(candidate)
        _seal(candidate)
        reference = _reference(candidate, key, env.image, image_hash)
        reference.verify(purpose="difficulty")
        from cooperagents.published_restore import restore_environment

        restored = restore_environment(reference, candidate / "checkpoint", scratch=env.root.parent, image_base=base)
        restored.cleanup()
        final = output / key
        if final.exists():
            raise FileExistsError("capture publication already exists")
        # Moving a directory across parents requires write permission for its .. entry.
        # No locator is visible yet; payload files remain read-only throughout.
        candidate.chmod(0o700)
        candidate.rename(final)
        published = final
        final.chmod(0o500)
        reference = _reference(final, key, env.image, image_hash)
        # Publish only small persistent metadata after the full snapshot is certified.
        for path in (final / "checkpoint").iterdir():
            if path.name != "manifest.json":
                shutil.copyfile(path, destination / path.name)
        locator = destination.with_name(destination.name + ".publication.json")
        pending = locator.with_suffix(".json.pending")
        write_json(pending, reference.model_dump())
        pending.replace(locator)
        shutil.copyfile(final / "checkpoint/manifest.json", destination / "manifest.json.pending")
        (destination / "manifest.json.pending").replace(destination / "manifest.json")
        if trace:
            trace(
                "checkpoint_end",
                path=str(destination),
                boundary=metadata["boundary"],
                files=json.loads((destination / "manifest.json").read_bytes())["files"],
                patch_source="working_tree",
            )
        complete = True
        return patch
    finally:
        try:
            if owned:
                base.close()
        finally:
            if published is not None and not complete:
                (destination / "manifest.json").unlink(missing_ok=True)
                remove_tree(published)
                destination.with_name(destination.name + ".publication.json").unlink(missing_ok=True)
