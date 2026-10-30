"""Freeze a reviewed reader's source identity; rerun regression before release.

This is a maintainer command, never a runtime fallback for a mismatched package.
"""

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src/cooperagents"
sources = {
    p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(root.rglob("*"))
    if p.is_file() and (p.suffix in {".py", ".yaml"} or p.name == "direct_capture_qualification.json") and "__pycache__" not in p.parts
}
value = dict(
    schema=1,
    scope="repair-only",
    admitted_capture_profiles=["captured-post-gate-v1", "captured-direct-v3-v1"],
    sources=sources,
    reference_revision="1b5139d017e9c3ccfdbb1c5b1f0604df28c54e43",
    runtime_base_revision="1b5139d017e9c3ccfdbb1c5b1f0604df28c54e43",
    qualification_note=("direct V3 is selected only at repair-only capture; legacy replay retains its source profile; "
                        "worker matches recorded POST bytes; require regression evidence before release"),
)
(root / "reader_qualification.json").write_text(json.dumps(value, indent=2) + "\n")
print("frozen", len(sources), "reader source files; this command does not certify checkpoints or train")
