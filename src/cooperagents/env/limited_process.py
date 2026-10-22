"""Drain container command output without retaining unbounded text in memory."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Sequence

from cooperagents.env.base import ExecResult

MAX_OUTPUT_BYTES = 2 * 1024 * 1024


def run_limited(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
    env: dict[str, str] | None = None,
    new_session: bool = False,
) -> tuple[ExecResult, bool]:
    output = bytearray()
    truncated = False
    timed_out = False
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=new_session,
    ) as proc:
        assert proc.stdout is not None

        def drain() -> None:
            nonlocal truncated
            while chunk := proc.stdout.read1(65536):
                available = MAX_OUTPUT_BYTES - len(output)
                output.extend(chunk[:available])
                truncated |= len(chunk) > available

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        writer = None
        if input_text is not None:
            assert proc.stdin is not None

            def feed() -> None:
                try:
                    proc.stdin.write(input_text.encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass

            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if new_session:
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()
            code = 124
        if writer is not None:
            writer.join()
        reader.join()

    text = output.decode(errors="replace")
    if truncated:
        text += f"\n[output truncated after {MAX_OUTPUT_BYTES} bytes]"
    if timed_out:
        text += f"\n[timed out after {timeout}s]"
    return ExecResult(text, code), truncated
