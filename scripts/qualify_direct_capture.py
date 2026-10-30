"""Freeze a reviewed direct writer identity. Run regression and review before release."""

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src/cooperagents"
names = [
    "checkpoint.py",
    "checkpoint_metadata.py",
    "patching.py",
    "env/apptainer.py",
    "env/published_capture.py",
    "env/capture_archives.py",
    "env/capture_audit.py",
    "env/capture_layers.py",
    "env/capture_risks.py",
    "env/published_audit.py",
    "env/published_layers.py",
    "env/published_holder.py",
    "env/image_base.py",
]
capture = ["harness.py", "workers/mini_swe_worker.py", "vendor/mini_swe/models/litellm_model.py"]


def identity(names):
    return {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}


value = dict(schema=1, producer="direct-capture-v1", producer_identity=identity(names), capture_sources=identity(capture))
(root / "direct_capture_qualification.json").write_text(json.dumps(value, indent=2) + "\n")
