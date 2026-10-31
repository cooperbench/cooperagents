"""Model-free official repair smoke on real publications across a Slurm step.

The scripted policy resolves unmerged files with the current branch and commits.
This tests execution/patch transport, not correctness of feature implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from litellm import ModelResponse

from cooperagents.completion import CompletionBinding, CompletionSettings
from cooperagents.env.image_base import ImageBase
from cooperagents.env.image_channel import ImageChannel
from cooperagents.published import load_publications
from cooperagents.repair import run_repair_checkpoint

COMMAND = r"""set -e
python3 - <<'REPAIR'
import os, re, subprocess
from pathlib import Path
pattern = re.compile(rb"^<<<<<<<[^\n]*\n(.*?)^=======[^\n]*\n.*?^>>>>>>>[^\n]*(?:\n|$)", re.M | re.S)
count = 0
for name in subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0"):
    if not name:
        continue
    path = Path(os.fsdecode(name))
    if path.is_symlink() or not path.is_file():
        continue
    original = path.read_bytes()
    if b"\0" in original:
        continue
    replaced, hits = pattern.subn(lambda m: re.split(rb"^\|{7}[^\n]*\n", m[1], flags=re.M)[0], original)
    if hits:
        if re.search(rb"^(?:<{7}|>{7}|={7})(?: |$)", replaced, re.M):
            raise RuntimeError("unsupported remaining conflict in " + str(path))
        path.write_bytes(replaced)
        count += hits
print("CONFLICTS_RESOLVED", count)
REPAIR
python3 -m compileall -q .
git diff --check
git add -A
git -c user.name=qualification -c user.email=qualification@example.invalid commit --allow-empty -qm 'resolve checkpoint conflicts'
git status --porcelain
"""


def execute(row, scratch, output, *, source=None):
    calls = []
    started = time.monotonic()
    first_command_returned = None

    def complete(request):
        nonlocal first_command_returned
        with output.with_suffix(".requests.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    dict(purpose=request.purpose, elapsed_seconds=time.monotonic() - started, messages=request.messages[-2:]), default=str
                )
                + "\n"
            )
        if request.purpose == "summary":
            return ModelResponse(
                choices=[
                    dict(
                        index=0,
                        finish_reason="stop",
                        message=dict(
                            role="assistant",
                            content=(
                                "Continue the repair from the captured repository state. "
                                "Resolve merge conflicts, commit the result, then submit."
                            ),
                        ),
                    )
                ],
                usage=dict(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            )
        if len(calls) == 1 and first_command_returned is None:
            first_command_returned = time.monotonic() - started
        if len(calls) >= 3:
            raise RuntimeError("scripted policy exceeded bounded smoke; inspect gate output")
        command = COMMAND if not calls else "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        calls.append(command)
        return ModelResponse(
            choices=[
                dict(
                    index=0,
                    finish_reason="tool_calls",
                    message=dict(
                        role="assistant",
                        content=None,
                        tool_calls=[
                            dict(
                                id=f"call{len(calls)}",
                                type="function",
                                function=dict(name="bash", arguments=json.dumps(dict(command=command))),
                            )
                        ],
                    ),
                )
            ],
            usage=dict(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )

    settings = CompletionSettings(
        model="offline-qualification", revision="scripted-no-model", generation={"max_tokens": 4096}, timeout=30.0
    )
    with ImageBase.from_environment(row.image_sha256) as base:
        result = run_repair_checkpoint(
            source or Path(row.publication.path) / "checkpoint",
            published=None if source is not None else row,
            purpose="difficulty",
            image_base=base,
            scratch=scratch,
            run_id="official-" + row.version,
            completion=CompletionBinding(complete, settings, complete, settings),
            max_attempts=1,
        )
    patch = result.integrated.patch
    output.with_suffix(".patch").write_text(patch)
    output.write_text(
        json.dumps(
            dict(
                id=row.id,
                version=row.version,
                calls=calls,
                first_command_roundtrip_seconds=first_command_returned,
                metrics=result.metrics,
                patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
                patch_bytes=len(patch),
                training_eligible=False,
                repair_validated=False,
            ),
            indent=2,
            default=str,
        )
        + "\n"
    )
    assert patch and calls, "official repair did not export a patch"


def main():
    if os.environ.get("SLURM_JOB_NUM_NODES", "1") != "1":
        raise ValueError("prepared image requires a single-node allocation")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", type=Path, required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--scratch", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--worker", type=int)
    a = p.parse_args()
    rows = load_publications(a.catalog, expected_sha256=a.sha256)
    if a.worker is not None:
        execute(rows[a.worker], a.scratch, a.output)
        return
    assert len({r.image_sha256 for r in rows}) == 1
    a.output.mkdir(exist_ok=False)
    with ImageBase.prepare(Path(rows[0].image), rows[0].image_sha256, a.scratch) as base:
        (a.output / "base.json").write_text(json.dumps(base.proof, indent=2))
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="ca-off-") as directory:
            with ImageChannel(base.descriptor, base.proof, Path(directory)) as channel:
                for i, row in enumerate(rows):
                    command = [
                        "srun",
                        "--exact",
                        "--cpu-bind=cores",
                        "-N1",
                        "-n1",
                        "-c2",
                        "--mem=8G",
                        sys.executable,
                        __file__,
                        "--catalog",
                        str(a.catalog),
                        "--sha256",
                        a.sha256,
                        "--scratch",
                        str(a.scratch),
                        "--output",
                        str(a.output / f"{row.version}.json"),
                        "--worker",
                        str(i),
                    ]
                    subprocess.run(command, env=dict(os.environ, COOPER_REPAIR_BASE_CHANNEL=str(channel.path)), check=True, timeout=1800)


if __name__ == "__main__":
    main()
