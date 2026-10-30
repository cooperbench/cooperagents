"""Freeze a reviewed direct writer identity. Run regression and review before release."""

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src/cooperagents"
# JSON qualifications are excluded to avoid a self-referential hash. All source
# code contributing to capture, audit, provenance, and repair admission is bound.
names = sorted(
    p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml"} and "__pycache__" not in p.parts
)

capture = ["harness.py", "workers/mini_swe_worker.py", "vendor/mini_swe/models/litellm_model.py"]


def identity(names):
    return {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}


value = dict(schema=1, producer="direct-capture-v1", producer_identity=identity(names), capture_sources=identity(capture))
path = root / "direct_capture_qualification.json"
if path.exists():
    previous = json.loads(path.read_bytes())
    admitted = previous.pop("admitted", [])
    value["admitted"] = admitted
path.write_text(json.dumps(value, indent=2) + "\n")
