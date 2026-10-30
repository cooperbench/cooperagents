"""Reviewed producer provenance and repair-only execution compatibility.

Historical producers remain authenticated, independently of the new reader build.
Current runtime changes versus 093fc43 are planning-only for monitor=None repair,
apart from post-gate trace reporting matching the captured-post-gate profile.
Unknown source triples/transitive capture files remain a hard failure.
"""

import json
from pathlib import Path

from cooperagents.published import PublicationError, file_hash

PRODUCERS = {
    "repair-overlay-filesystem-candidate-v1": {
        "compat_sha256": "110f31abbeb5121385712dcdf4d4ebfefdc9db2b0d15080c98bd3309850134e0",
        "contract_sha256": "e7cb3a1c41d612a4fe4545941b881c63fd4ac5367f065ce042b03ee9570ad662",
        "converter_identity": "9f21a91ad89a563ca3b0cd5dd10742b0c354c691520d6cfc009a96675dfa2b33",
        "driver_sha256": "8fff9fdc2195e31e5e2b1b85c2643bd3b17c77f1d08229951c387a2212bd9b7e",
        "format": "repair-overlay-filesystem-candidate-v1",
        "publication_module_sha256": "fc0f7b99223888654d0ae3dcaaf2b8c45dff5da363448908a40e79160e992e7c",
        "risk_scanner_sha256": "a0c7c11f69f5fcc78834a27603187a3aaf6405e047cede2b4b5769296b32a369",
        "source_identity": {
            "checkpoint.py": "ce82ea31e41759437674098c642d21444b1bdd2704ee40c2bfbb49a381242d9c",
            "checkpoint_delta.py": "d61c09cf212d78272d494f3547c2063d33e2c05dbbb7913c3ee7857a59fe4c4a",
            "env/apptainer.py": "75951a760cd8a4f427d5fa581f7e4f12e1f414a5e61ffb029cd100bcda4cd648",
            "env/limited_process.py": "f9bbbf4fab43f4a745f8a5e39dda23ad67ed1047186df88764db47dee2714901",
            "harness.py": "3598812b755ae0e390857bc3946d0a4fe21df47c6409d45a8e7a36f5bc64c7bc",
            "patching.py": "5180f4dbf3fd344c85a5eec745ea1216cc5ea23719564ad7bba422f5c73d428d",
            "prototype/guards.py": "6b3134dde4005258c6707c3e84904c56f5d5add36a874bdaef97b78885ee9f6e",
            "repair.py": "c9653c2ae707b8195b4b7dd59e4c052ecd7dafe125a3b81d20bc4bcde3c87c46",
            "types.py": "70f80d32ffed6e5bea8932f4377f7761feb52a5c81067e32f61df4585b550ce5",
            "workers/mini_swe_worker.py": "425853835b97cb4d400c9ed7adc600d691bf6feb724e0c12caee1ab38f3a411d",
        },
    },
    "repair-overlay-v3": {
        "compat_sha256": "110f31abbeb5121385712dcdf4d4ebfefdc9db2b0d15080c98bd3309850134e0",
        "contract_sha256": "a28b0ac4cf17819753b4e7ab08163d3eb71e7a7e030c019a751407994478c46d",
        "converter_identity": "2ea84aa413c44a22cded8948bfb3c029bd3c7f9a22852a263611f328d4bce3af",
        "driver_sha256": "07dc98f1cb875fdd18b978f87a4a3c0e16bf7af07d314a8ccb3edd691352e7d5",
        "format": "repair-overlay-v3",
        "publication_module_sha256": "fbe82dd8a3f50a528369436772be745348dc8ebcffe89325d3e07ac3a1743a0b",
        "risk_scanner_sha256": "a0c7c11f69f5fcc78834a27603187a3aaf6405e047cede2b4b5769296b32a369",
        "source_identity": {
            "checkpoint.py": "ce82ea31e41759437674098c642d21444b1bdd2704ee40c2bfbb49a381242d9c",
            "checkpoint_delta.py": "d61c09cf212d78272d494f3547c2063d33e2c05dbbb7913c3ee7857a59fe4c4a",
            "env/apptainer.py": "75951a760cd8a4f427d5fa581f7e4f12e1f414a5e61ffb029cd100bcda4cd648",
            "env/limited_process.py": "f9bbbf4fab43f4a745f8a5e39dda23ad67ed1047186df88764db47dee2714901",
            "harness.py": "3598812b755ae0e390857bc3946d0a4fe21df47c6409d45a8e7a36f5bc64c7bc",
            "patching.py": "5180f4dbf3fd344c85a5eec745ea1216cc5ea23719564ad7bba422f5c73d428d",
            "prototype/guards.py": "6b3134dde4005258c6707c3e84904c56f5d5add36a874bdaef97b78885ee9f6e",
            "repair.py": "c9653c2ae707b8195b4b7dd59e4c052ecd7dafe125a3b81d20bc4bcde3c87c46",
            "types.py": "70f80d32ffed6e5bea8932f4377f7761feb52a5c81067e32f61df4585b550ce5",
            "workers/mini_swe_worker.py": "425853835b97cb4d400c9ed7adc600d691bf6feb724e0c12caee1ab38f3a411d",
        },
    },
}

CAPTURED = {
    "captured-post-gate-v1": {
        "harness.py": "6dad3831185c31d28e39cd9d2c10c3a1f4b39b46bda93dfca03d414a82219dc3",
        "vendor/mini_swe/models/litellm_model.py": "224acf18db39594516573ef86a21e9a5d8d00482e38638f75fc6dd2d63d1b1a6",
        "workers/mini_swe_worker.py": "9508c3aed51a82c4391ed8fbf108e8be0c5bd7f52d66e3c6956ce39c3ac94950",
    },
    "captured-pre-gate-v1": {
        "harness.py": "3598812b755ae0e390857bc3946d0a4fe21df47c6409d45a8e7a36f5bc64c7bc",
        "vendor/mini_swe/models/litellm_model.py": "6d4a7b1b9494e78bb7c7ba6d57161a036df67bf76515de8884427a0d43ee83d2",
        "workers/mini_swe_worker.py": "425853835b97cb4d400c9ed7adc600d691bf6feb724e0c12caee1ab38f3a411d",
    },
}


def validate_producer(receipt: dict, fmt: str) -> None:
    expected = PRODUCERS.get(fmt)
    if expected is None or any(receipt.get(k) != v for k, v in expected.items()):
        raise PublicationError("unknown producer/source identity")


def validate_capture(captured: dict[str, str], profile: str) -> None:
    import cooperagents

    root = Path(cooperagents.__file__).parent
    expected = CAPTURED.get(profile)
    if expected is None or any(captured.get(n) != h for n, h in expected.items()):
        raise PublicationError("unknown captured source profile")
    validate_runtime(profile)
    for name, checksum in captured.items():
        if name in expected:
            continue
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts or not (root / rel).is_file():
            raise PublicationError("unsafe/missing captured transitive source")
        if file_hash(root / rel) != checksum:
            raise PublicationError("unreviewed transitive source difference")


def reader_identity() -> dict:
    return _qualification()["sources"]


def _qualification() -> dict:
    value = json.loads(Path(__file__).with_name("reader_qualification.json").read_bytes())
    if value.get("schema") != 1 or value.get("scope") != "repair-only":
        raise PublicationError("missing/unknown reader qualification")
    return value


def validate_runtime(profile: str) -> None:
    import cooperagents

    root = Path(cooperagents.__file__).parent
    qualification = _qualification()
    if profile not in qualification["admitted_capture_profiles"]:
        raise PublicationError("captured source profile not qualified for this reader")
    actual = {
        p.relative_to(root).as_posix(): file_hash(p)
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml"} and "__pycache__" not in p.parts
    }
    if actual != qualification["sources"]:
        raise PublicationError("unqualified repair runtime source identity")


def identify_capture(captured: dict[str, str]) -> str:
    for profile, expected in CAPTURED.items():
        if all(captured.get(name) == checksum for name, checksum in expected.items()):
            validate_capture(captured, profile)
            return profile
    raise PublicationError("unknown captured repair source identity")


def qualification_identity() -> str:
    return file_hash(Path(__file__).with_name("reader_qualification.json"))
