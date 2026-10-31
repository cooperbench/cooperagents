"""Reject incompatible Slurm placement before preparing a base or starting a client."""

import pytest

from cooperagents.env.prepared_image import main


def test_multinode_allocation_rejected_before_admission(monkeypatch):
    monkeypatch.setenv("SLURM_JOB_NUM_NODES", "2")
    with pytest.raises(ValueError, match="single-node"):
        main()
