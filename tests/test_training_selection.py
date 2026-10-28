import pytest

from cooperagents.env.base import ExecResult
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.training_selection import SOURCE_CONFLICT_COMMAND, select_training_checkpoint

CONFLICT = "<<<<<<< ours\nleft\n=======\nright\n>>>>>>> theirs\n"


def test_scan_final_sources_and_preserve_paths(tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    try:
        for name in ("src/a file.rs", "src/é\nfile.ts", "patch.txt", "Makefile", "tests/test_a.rs", ".cb_checks/check.py"):
            env.write_file(name, CONFLICT)
        env.write_file("old.rs", CONFLICT)
        env.git_diff()  # Track it, then remove the conflict from the final working tree.
        env.write_file("old.rs", "clean\n")
        env.write_file("partial.rs", "<<<<<<< ours\n=======\n")
        env.write_file(".gitignore", "ignored.rs\n")
        env.write_file("ignored.rs", CONFLICT)
        result = select_training_checkpoint(True, env.execute(SOURCE_CONFLICT_COMMAND))
        assert result == {
            "policy": "gate-or-source-conflicts-v1", "initial_healthy": True, "reason": "source_conflict",
            "source_conflicts": [{"path": path, "blocks": [[1, 3, 5]]} for path in ("src/a file.rs", "src/é\nfile.ts")],
        }
    finally:
        env.cleanup()


def test_no_match_keeps_gate_failure_and_healthy_distinct():
    assert select_training_checkpoint(False, ExecResult("", 1))["reason"] == "gate_failed"
    assert select_training_checkpoint(True, ExecResult("", 1))["reason"] == "healthy"


def test_complete_blocks_cannot_span_files():
    records = [("a.rs", 1, "<<<<<<< ours"), ("a.rs", 3, "======="), ("b.rs", 5, ">>>>>>> theirs")]
    output = "".join(f"{path}\0{line}\0{marker}\n" for path, line, marker in records)
    assert select_training_checkpoint(True, ExecResult(output, 0))["reason"] == "healthy"


def test_scan_preserves_multiple_block_locations():
    records = [(1, "<<<<<<< ours"), (3, "======="), (5, ">>>>>>> theirs"),
               (9, "<<<<<<< ours"), (11, "======="), (15, ">>>>>>> theirs")]
    output = "".join(f"src/lib.rs\0{line}\0{marker}\n" for line, marker in records)
    assert select_training_checkpoint(True, ExecResult(output, 0))["source_conflicts"] == [
        {"path": "src/lib.rs", "blocks": [[1, 3, 5], [9, 11, 15]]},
    ]


@pytest.mark.parametrize("result", [
    ExecResult("", 124), ExecResult("fatal: failed", 2), ExecResult("", 0), ExecResult("unexpected", 1),
    ExecResult("[output truncated after 20 bytes]\n", 0), ExecResult("a.rs\0x\0<<<<<<< ours\n", 0),
    ExecResult("../a.rs\0" + "1\0<<<<<<< ours\n", 0), ExecResult("a.rs\0" + "1\0not a marker\n", 0),
    ExecResult("a.rs\0" + "2\0<<<<<<< ours\na.rs\0" + "1\0=======\n", 0),
])
def test_scan_errors_never_become_healthy_skips(result):
    with pytest.raises((ValueError, RuntimeError)):
        select_training_checkpoint(True, result)


def test_selection_requires_collection_mode_and_journal(tmp_path):
    with pytest.raises(ValueError, match="requires stop_after"):
        UnifiedHarness(collect_source_conflicts=True)
    harness = UnifiedHarness(checkpoint_dir=tmp_path, stop_after_repair_checkpoint=True, collect_source_conflicts=True)
    from cooperagents.types import TeamSpec

    with pytest.raises(ValueError, match="trajectory journal"):
        harness.run(TeamSpec(run_id="test", repo="demo", task_id=1, features=[1, 2], repair_integrator=True),
                    env_factory=lambda _: pytest.fail("invalid collection must not create environments"))
