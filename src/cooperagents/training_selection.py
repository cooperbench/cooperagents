"""Opt-in training selection; never changes the evaluation health gate."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from cooperagents.checkpoint import complete_output
from cooperagents.env.base import ExecResult
from cooperagents.patching import _SCRATCH, _TEST_PATH

# Source languages in the collection corpus; auxiliary patches/docs are not source.
_SOURCE_SUFFIXES = ("c", "h", "cc", "cpp", "cxx", "hpp", "hxx", "go", "py", "pyi", "rs",
                    "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts")
SOURCE_CONFLICT_COMMAND = (
    "git grep --no-color --no-ext-grep --untracked --exclude-standard -n -z -I -E "
    + shlex.quote("^(<<<<<<<( |\r?$)|=======\r?$|>>>>>>>( |\r?$))")
    + " -- " + " ".join(shlex.quote(f"*.{suffix}") for suffix in _SOURCE_SUFFIXES)
)
_SCAN_ROW = re.compile(r"([^\0]+)\0([0-9]+)\0([^\n]*)\n")


def select_training_checkpoint(initial_healthy: bool, scan: ExecResult) -> dict:
    """Parse NUL-delimited Git evidence, retaining only complete source conflict blocks."""
    if type(initial_healthy) is not bool:
        raise ValueError("Training selection requires the original boolean health result")
    output = complete_output(scan, "Source conflict scan", allow_failure=True)
    if scan.exit_code not in (0, 1) or (scan.exit_code == 1 and output) or (scan.exit_code == 0 and not output):
        raise RuntimeError("Source conflict scan failed")
    files: dict[str, dict] = {}
    position = 0
    while position < len(output):
        record = _SCAN_ROW.match(output, position)
        if record is None:
            raise ValueError("Malformed source conflict evidence")
        path, number, marker = record.groups()
        marker = marker.removesuffix("\r")
        position = record.end()
        name = PurePosixPath(path)
        if int(number) < 1 or name.is_absolute() or ".." in name.parts:
            raise ValueError("Malformed source conflict evidence")
        opening = marker == "<<<<<<<" or marker.startswith("<<<<<<< ")
        closing = marker == ">>>>>>>" or marker.startswith(">>>>>>> ")
        if not (opening or closing or marker == "======="):
            raise ValueError("Malformed source conflict marker")
        if (name.suffix.removeprefix(".") not in _SOURCE_SUFFIXES
                or _TEST_PATH.search(path) or _SCRATCH.search(path)):
            continue
        item = files.setdefault(path, {"blocks": [], "pending": [], "last": 0})
        line = int(number)
        if line <= item["last"]:
            raise ValueError("Source conflict lines must increase within each file")
        item["last"] = line
        if opening:
            item["pending"] = [line]
        elif marker == "=======":
            item["pending"] = [*item["pending"], line] if len(item["pending"]) == 1 else []
        else:
            if len(item["pending"]) == 2:
                item["blocks"].append([*item["pending"], line])
            item["pending"] = []
    conflicts = [{"path": path, "blocks": item["blocks"]} for path, item in sorted(files.items()) if item["blocks"]]
    return {
        "policy": "gate-or-source-conflicts-v1",
        "initial_healthy": initial_healthy,
        "reason": "gate_failed" if not initial_healthy else "source_conflict" if conflicts else "healthy",
        "source_conflicts": conflicts,
    }
