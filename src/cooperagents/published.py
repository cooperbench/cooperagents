"""Authenticated references for the two evaluated repair publication formats.

Publication identity and embedded native-checkpoint identity are separate. A catalog
is data; loader_data in historical catalogs is never imported or executed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

LEGACY = "repair-overlay-filesystem-candidate-v1"
V3 = "repair-overlay-v3"
FORMATS = {"legacy": LEGACY, "v3": V3}


class PublicationError(ValueError):
    """Invalid/incompatible infrastructure, never a repair failure."""


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_object(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise PublicationError(f"expected JSON object: {path.name}")
    return value


class Locator(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    path: str
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def absolute(self):
        _absolute_path(self.path, "publication")
        return self


class PublishedCheckpoint(BaseModel):
    """Evaluation-only publication; acquiring it never upgrades repair qualification."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    node: str = Field(min_length=1)
    version: Literal["legacy", "v3"]
    format: Literal["repair-overlay-filesystem-candidate-v1", "repair-overlay-v3"]
    publication: Locator
    legacy_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_eligible: Literal[False]
    repair_validated: Literal[False]

    @field_validator("training_eligible", "repair_validated", mode="before")
    @classmethod
    def boolean_qualification(cls, value):
        if type(value) is not bool:
            raise ValueError("qualification flags must be booleans")
        return value

    @model_validator(mode="after")
    def identities(self):
        if self.format != FORMATS[self.version]:
            raise ValueError("version and format disagree")
        _absolute_path(self.image, "image")
        return self

    @property
    def manifest_sha256(self) -> str:
        return self.legacy_manifest_sha256

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PublishedCheckpoint:
        try:
            return cls.model_validate(value)
        except ValidationError as exc:
            raise PublicationError(str(exc)) from exc

    def verify(self, *, purpose: str) -> VerifiedPublication:
        if purpose != "difficulty":
            raise PublicationError("unvalidated publications require explicit difficulty purpose")
        if socket.gethostname().split(".")[0] != self.node.split(".")[0]:
            raise PublicationError("publication requires its recorded compute node")
        return verify_publication(self)


def load_publications(path: Path, *, expected_sha256: str) -> list[PublishedCheckpoint]:
    if file_hash(path) != expected_sha256:
        raise PublicationError("catalog identity mismatch")
    catalog = read_object(path)
    if catalog.get("supported_versions") != ["legacy", "v3"]:
        raise PublicationError("unsupported catalog versions")
    rows = catalog.get("checkpoints")
    if not isinstance(rows, list) or not rows:
        raise PublicationError("catalog requires checkpoints")
    items = [PublishedCheckpoint.from_dict(row) for row in rows]
    if len({item.id for item in items}) != len(items):
        raise PublicationError("duplicate checkpoint IDs")
    return items


@dataclass(frozen=True)
class VerifiedPublication:
    reference: PublishedCheckpoint
    root: Path
    checkpoint: Path
    state: dict
    receipt: dict
    risks: dict


def _manifest(root: Path, reference: PublishedCheckpoint) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise PublicationError("publication directory missing or symlinked")
    path = root / "publication.json"
    if path.is_symlink() or file_hash(path) != reference.publication.sha256:
        raise PublicationError("publication manifest identity mismatch")
    value = read_object(path)
    expected = (reference.format, reference.publication.key, "filesystem-verified-repair-unverified")
    if (value.get("format"), value.get("key"), value.get("state")) != expected or root.name != reference.publication.key:
        raise PublicationError("unsupported publication contract/key")
    return value


def _inventory(root: Path) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PublicationError("symlink in publication")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PublicationError("nonregular publication member")
        name = path.relative_to(root).as_posix()
        if name != "publication.json":
            files[name] = dict(sha256=file_hash(path), bytes=path.stat().st_size)
    return files


def _receipt(root: Path, metadata: dict, reference: PublishedCheckpoint) -> dict:
    from cooperagents.published_profiles import validate_producer

    receipt = read_object(root / "receipt.json")
    validate_producer(receipt, reference.format)
    if receipt.get("runtime_qualification") != runtime_qualification():
        raise PublicationError("unqualified kernel, Apptainer version or user identity")
    if metadata.get("profile") != receipt.get("profile"):
        raise PublicationError("publication/receipt execution profile differs")
    if file_hash(root / "target-index.json.gz") != receipt["target_inventory_sha256"]:
        raise PublicationError("target inventory identity mismatch")
    _v3_receipt(root, receipt, reference)
    checks = {
        "legacy_manifest_sha256": reference.manifest_sha256,
        "image_sha256": reference.image_sha256,
        "receipt_sha256": file_hash(root / "receipt.json"),
        "repair_validated": False,
        "training_eligible": False,
    }
    if any(metadata.get(name) != expected for name, expected in checks.items()):
        raise PublicationError("publication metadata identity mismatch")
    if receipt["manifest_sha256"] != reference.manifest_sha256 or receipt["image_sha256"] != reference.image_sha256:
        raise PublicationError("receipt checkpoint/image identity mismatch")
    if receipt.get("training_eligible") is not False:
        raise PublicationError("unsupported publication qualification")
    _private_layers(root, receipt)
    return receipt


def verify_publication(reference: PublishedCheckpoint) -> VerifiedPublication:
    from cooperagents.checkpoint import verify_checkpoint
    from cooperagents.published_profiles import validate_capture

    root = Path(reference.publication.path)
    manifest = _manifest(root, reference)
    if manifest["files"] != _inventory(root):
        raise PublicationError("publication payload mismatch")
    receipt = _receipt(root, manifest["metadata"], reference)
    checkpoint = root / "checkpoint"
    if file_hash(checkpoint / "manifest.json") != reference.manifest_sha256:
        raise PublicationError("original checkpoint manifest mismatch")
    state = verify_checkpoint(checkpoint)
    if state["runtime"]["image_sha256"] != reference.image_sha256:
        raise PublicationError("original checkpoint image mismatch")
    validate_capture(read_object(checkpoint / "repair-input.json")["source_hashes"], receipt["profile"])
    risks = read_object(root / "risks.json")
    if file_hash(root / "risks.json") != receipt["risk_sha256"] or risks.get("training_eligible") is not False:
        raise PublicationError("risk identity mismatch")
    if risks.get("review_status") != "needs_review" or any(x["category"] == "confirmed_missing" for x in risks["findings"]):
        raise PublicationError("confirmed missing state or unsupported risk qualification")
    return VerifiedPublication(reference, root, checkpoint, state, receipt, risks)


def runtime_qualification() -> dict:
    return dict(
        kernel=platform.release(),
        uid=os.getuid(),
        gid=os.getgid(),
        apptainer=subprocess.check_output(["apptainer", "--version"], text=True).strip(),
    )


def _absolute_path(value: str, name: str) -> None:
    if not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"{name} path must be absolute")


def _v3_receipt(root: Path, receipt: dict, reference: PublishedCheckpoint) -> None:
    if reference.format == V3:
        if any(p.lstat().st_mode & 0o222 for p in [root, *root.rglob("*")]):
            raise PublicationError("V3 publication is not sealed read-only")
        if file_hash(root / "legacy-publication.json") != receipt["legacy_publication_sha256"]:
            raise PublicationError("historical publication provenance mismatch")


def _private_layers(root: Path, receipt: dict) -> None:
    for name in ("delta", "repo"):
        if file_hash(root / (name + ".tar")) != receipt[name + "_sha256"]:
            raise PublicationError("private layer identity mismatch")
