"""Heartbeat telemetry: model wrapper writes; live checker reads."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModel  # noqa: E402


def _model() -> LitellmModel:
    return LitellmModel(model_name="dummy", cost_tracking="ignore_errors")


def test_heartbeat_disabled_by_default(tmp_path):
    m = _model()
    assert m.heartbeat_path is None
    m._heartbeat(1.0)  # must be a no-op, not an error


def test_heartbeat_appends_lines(tmp_path):
    m = _model()
    m.heartbeat_path = str(tmp_path / "1234_agent1.hb")
    m._heartbeat(2.5)
    m._heartbeat(0.4)
    lines = (tmp_path / "1234_agent1.hb").read_text().strip().splitlines()
    assert len(lines) == 2
    ts, wait = lines[0].split()
    assert abs(float(ts) - time.time()) < 5
    assert wait == "wait=2.5"


def test_heartbeat_write_failure_is_silent(tmp_path):
    m = _model()
    m.heartbeat_path = str(tmp_path / "no_such_dir" / "x.hb")
    m._heartbeat(1.0)  # directory missing: swallowed, never raises


def test_live_checker_reads_own_process(tmp_path):
    """A hb file keyed to a live pid (ours) is reported OK."""
    hb = tmp_path / f"{os.getpid()}_agent1.hb"
    hb.write_text(f"{time.time():.1f} wait=1.0\n")
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts/fleet/live_starvation_check.py"),
         "--hb-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "OK" in out.stdout and "agent1" in out.stdout


def test_live_checker_flags_stale(tmp_path):
    hb = tmp_path / f"{os.getpid()}_agent1.hb"
    hb.write_text(f"{time.time() - 9999:.1f} wait=1.0\n")
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts/fleet/live_starvation_check.py"),
         "--hb-dir", str(tmp_path), "--stale", "1200"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 3, out.stdout + out.stderr
    assert "STALE" in out.stdout
