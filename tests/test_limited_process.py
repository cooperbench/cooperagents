"""A noisy container command cannot consume unbounded host memory."""

import sys

from cooperagents.env.limited_process import MAX_OUTPUT_BYTES, run_limited


def test_output_is_drained_but_bounded():
    result, truncated = run_limited(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000000)"],
    )
    assert result.exit_code == 0
    assert truncated
    assert result.stdout.startswith("x" * 100)
    assert len(result.stdout) < MAX_OUTPUT_BYTES + 100
    assert "output truncated" in result.stdout
