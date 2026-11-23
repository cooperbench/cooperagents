import ast
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from cooperagents import structural_signals
from cooperagents.agent_coordination_types import SignalConfig, StepRecord
from cooperagents.env.base import ExecResult
from cooperagents.env.local import LocalEnv
from cooperagents.git_snapshots import GitEvidence, RegionComparison
from cooperagents.structural_signals import DiffProbe, SignalTracker, overlap_signals, parse_probe, parse_steps

FIXTURES = Path(__file__).parent / "fixtures" / "agent_coordination" / "signals"
CLEAN = "a" * 64
DIRTY = "b" * 64


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def history(commands):
    messages = []
    for i, command in enumerate(commands):
        action = {"tool_name": "bash", "tool_call_id": str(i), "command": command}
        messages.extend(
            (
                {"role": "assistant", "extra": {"actions": [action]}},
                {"role": "tool", "tool_call_id": str(i), "extra": {"raw_output": "stdout\n雪", "returncode": 1}},
            )
        )
    return messages


def step(index, command="ls", code=1, *, is_test=False, fp=CLEAN, hunks=None):
    return StepRecord("agent1", index, command, code, "", is_test, fp, {"src.py": hunks} if hunks else {})


def tracker(**options):
    return SignalTracker("agent1", SignalConfig(**options), initial_probe=DiffProbe(CLEAN, {}, 0, 0, 0.0))


def replay(t, records):
    return [asdict(signal) for record in records for signal in t.observe([record])]


def test_message_pairing_and_cursor():
    config = SignalConfig()
    messages = fixture("messages_pairing")
    records = parse_steps("agent1", messages, start=0, config=config)
    assert [asdict(record) for record in records] == fixture("expected_pairing")
    assert parse_steps("agent1", messages, start=2, config=config) == records[:1] + records[2:]
    assert parse_steps("agent1", messages, start=0, config=SignalConfig(history_steps=2)) == records[-2:]
    assert parse_steps("agent1", messages, start=len(messages), config=config) == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd '/a b' && cd another && A='' B=\"a && b\" pytest tests -q", True),
        ("cd /repo && A= PYTHONPATH=src python3  -m pytest | head", True),
        ("./.cb_checks/test.sh", True),
        ("bash '.cb_checks/my check.sh'", True),
        ("python /work/.cb_checks/test.py", True),
        ("pytest-custom tests", False),
        ("python -m pytest_cov", False),
        ("echo pytest", False),
        ("cat .cb_checks/test.py", False),
        ("cd /repo && git status && pytest", False),
        ("printf '%s' pytest", False),
        ("npm test:watch", False),
        ("custom-test suite", False),
    ],
)
def test_classifies_command_only(command, expected):
    assert parse_steps("agent1", history([command]), start=0, config=SignalConfig())[0].is_test is expected


def test_test_prefix_extensions():
    config = SignalConfig(test_command_prefixes=("custom-test run",))
    records = parse_steps("agent1", history(["custom-test run suite", "pytest"]), start=0, config=config)
    assert [record.is_test for record in records] == [True, False]
    assert records[0].output_sha256 == sha256("stdout\n雪".encode()).hexdigest()


def test_probe_command_and_stdout():
    assert DiffProbe.command() + "\n" == (FIXTURES / "probe_command.sh").read_text()
    for name, expected in fixture("expected_probes").items():
        probe = parse_probe((FIXTURES / f"probe_{name}.txt").read_text(), latency_ms=1.25)
        assert json.loads(json.dumps(asdict(probe))) == expected


def test_probe_local_env(tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    env.write_file("tracked file.txt", "one\ntwo\nthree\n")
    env.write_file(".cb_tracked", "internal")
    assert env.execute("git add . && git commit -qm tracked").exit_code == 0
    clean = DiffProbe.run(env)
    assert clean.error is None
    assert clean.fingerprint == sha256(b"").hexdigest()
    env.write_file("tracked file.txt", "one\nchanged\nthree\n")
    edited = DiffProbe.run(env)
    assert edited.fingerprint not in (None, clean.fingerprint)
    assert edited.hunks == {"tracked file.txt": [(2, 1)]}
    assert (edited.added, edited.deleted) == (1, 1)
    assert env.execute("git checkout -- 'tracked file.txt'").exit_code == 0
    assert DiffProbe.run(env).fingerprint == clean.fingerprint
    env.write_file(".cb_tracked", "new internal")
    env.write_file(".cb_checks/test.sh", "internal")
    env.write_file("patch.txt", "untracked patch")
    assert DiffProbe.run(env).fingerprint == clean.fingerprint
    env.write_file("new file", "new untracked contents")
    untracked = DiffProbe.run(env)
    assert untracked.fingerprint not in (None, clean.fingerprint)
    assert untracked.hunks == {}
    env.write_file("patch.txt", "replaced untracked patch")
    assert DiffProbe.run(env).fingerprint == untracked.fingerprint
    assert env.execute("rm 'new file' && git add patch.txt && git commit -qm patch").exit_code == 0
    env.write_file("patch.txt", "tracked patch generated again")
    assert DiffProbe.run(env).fingerprint == clean.fingerprint


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (ExecResult("oops", 1), "execution_failed"),
        (ExecResult("", 124), "timeout"),
        (ExecResult("[output truncated after 123 bytes]", 1), "truncated"),
    ],
)
def test_probe_failure_and_timeout_forwarding(result, error):
    def execute(command, *, timeout):
        assert command.startswith("git -c core.quotepath=false")
        assert timeout == 0.25
        return result

    probe = DiffProbe.run(SimpleNamespace(execute=execute), config=SignalConfig(probe_timeout_s=0.25))
    assert probe.error == error
    assert probe.fingerprint is None


def test_no_progress_thresholds_and_reset():
    expected = fixture("expected_signals")
    for count in (4, 5, 6):
        records = [step(i) for i in range(1, count + 1)]
        assert replay(tracker(), records) == (expected["no_progress"] if count >= 5 else [])
    for failures in (2, 3, 4):
        records = [step(i, code=int(i > 5 - failures)) for i in range(1, 6)]
        assert replay(tracker(), records) == expected[f"failures_{failures}"]
    records = [step(i, "pytest " + "x" * 180, is_test=True) for i in range(1, 6)]
    assert replay(tracker(), records) == expected["no_progress_hard"]
    assert replay(tracker(), [*records[:-1], step(5, fp=DIRTY)]) == []
    assert replay(tracker(), [*records[:3], step(4, fp=None), records[4]]) == []
    assert replay(tracker(), [*records[:-1], step(5, code=0)]) == []
    assert replay(tracker(), [*records[:2], step(3, fp=DIRTY), *records[3:]]) == expected["no_progress_hard"]
    assert replay(tracker(), [step(i) for i in range(1, 15)]) == expected["no_progress"]
    assert replay(tracker(), [step(i, fp=CLEAN if i < 7 else DIRTY) for i in range(1, 14)]) == expected["progress_rearmed"]
    assert replay(tracker(), [step(i, code=int(i != 6), is_test=i == 6) for i in range(1, 13)]) == expected["pass_rearmed"]


def test_churn_thresholds_windows_and_common_region():
    expected = fixture("expected_signals")
    for count in (2, 3, 4):
        records = [step(i, hunks=[(3, 1 + i)]) for i in range(1, count + 1)]
        assert replay(tracker(), records) == (expected["churn"] if count >= 3 else [])
    for last in (9, 10, 11):
        records = [step(i, hunks=[(3, 2 if i == 1 else 3 if i < last else 4)]) for i in range(1, last + 1)]
        assert replay(tracker(no_progress_window=12), records) == expected[f"churn_window_{last}"]
    records = [step(1, hunks=[(3, 1)]), step(2, hunks=[(10, 1)]), step(3, hunks=[(20, 1)])]
    assert replay(tracker(), records) == []
    assert replay(tracker(), [step(i, hunks=[(3, i)]) for i in range(1, 3)] + [step(3, fp=None)]) == []
    # Insertions participate in the same base-coordinate overlap as the snapshots.
    records = [step(1, hunks=[(3, 0)]), step(2, hunks=[(4, 2)]), step(3, hunks=[(3, 0)])]
    assert replay(tracker(), records) == expected["churn_insertion"]


def test_churn_debounce_and_rearm():
    expected = fixture("expected_signals")
    records = [step(i, hunks=[(3, i + 1)]) for i in range(1, 16)]
    assert replay(tracker(no_progress_window=50), records) == expected["churn"]
    records[3] = step(4, "pytest", 0, is_test=True, hunks=[(3, 5)])
    assert replay(tracker(no_progress_window=50), records) == expected["churn_pass_rearmed"]
    records = [step(i, hunks=[(3, 1 + i if i <= 3 else 4 if i <= 13 else i - 9)]) for i in range(1, 18)]
    assert replay(tracker(no_progress_window=50), records) == expected["churn_quiet_rearmed"]
    # A separate region can qualify independently but shares the kind's cooldown.
    records = [step(i, hunks=[(3, min(i, 3) + 1), *([(30, i - 3)] if i >= 4 else [])]) for i in range(1, 10)]
    assert replay(tracker(no_progress_window=50), records) == expected["churn_other_region"]


def test_test_regression_normalization_and_cooldown():
    expected = fixture("expected_signals")
    passed = step(1, " cd /repo && pytest   tests/test_a.py 2>&1 | tail -5 ", 0, is_test=True)
    failures = [step(i, "cd /repo && pytest tests/test_a.py | head -10", is_test=True, fp=None) for i in range(2, 10)]
    assert replay(tracker(), failures) == []
    assert replay(tracker(), [passed, step(2, "cd /else && pytest tests/test_a.py", is_test=True)]) == []
    assert replay(tracker(), [passed, step(2, "cd /repo && pytest tests/test_a.py", 0, is_test=True)]) == []
    assert replay(tracker(), [passed, *failures[:6]]) == expected["regression"][:1]
    assert replay(tracker(), [passed, *failures[:7]]) == expected["regression"]
    assert replay(tracker(), [passed, *failures]) == expected["regression"]


def test_poll_returns_one_candidate_by_priority_and_keeps_probe_telemetry():
    expected = fixture("expected_signals")
    t = tracker()
    records = [
        step(1, "pytest", 0, is_test=True),
        step(2, "pytest", is_test=True),
        step(3),
        step(4),
        step(5, "pytest", is_test=True),
    ]
    assert [asdict(signal) for signal in t.observe(records)] == expected["poll_first_kind"]
    assert [asdict(signal) for signal in t.observe([step(6)], probe=DiffProbe(None, {}, 0, 0, 1.0, "truncated"))] == expected["probe"]
    assert t.records[-1].fingerprint is None
    assert t.observe([step(6)], probe=DiffProbe(None, {}, 0, 0, 1.0, "truncated")) == []
    assert t.observe([step(11)], probe=DiffProbe(None, {}, 0, 0, 1.0, "truncated")) == []
    assert [asdict(signal) for signal in t.observe([step(12)], probe=DiffProbe(None, {}, 0, 0, 1.0, "truncated"))] == expected[
        "probe_rearmed"
    ]


def test_poll_prefers_regression_over_warn_and_does_not_repeat_observations():
    t = tracker()
    records = [
        step(1, "pytest", 0, is_test=True, fp=None),
        step(2, "pytest", is_test=True, fp=DIRTY),
        *(step(i) for i in range(3, 9)),
    ]
    assert [signal.kind for signal in t.observe(records)] == ["TEST_REGRESSION"]
    assert t.observe(records) == []


def test_signal_events_telemetry_debounce_and_severity():
    t = tracker(no_progress_window=50)
    expected = fixture("expected_signals")
    assert t.observe([], probe=DiffProbe(CLEAN, {}, 0, 0, 1.0)) == []
    assert [asdict(signal) for signal in t.submit_attempt(None)] == expected["submit_accepted"]
    t.observe([step(1)])
    assert [asdict(signal) for signal in t.submit_attempt("E" * 520)] == expected["submit_first"]
    assert [asdict(signal) for signal in t.peer_message("agent1", "雪x")] == expected["peer_message"]
    assert t.peer_message("agent1", "") == []
    t.observe([step(6)])
    assert [asdict(signal) for signal in t.submit_attempt("second")] == expected["submit_cooldown"]
    assert t.peer_message("agent1", "") == []
    t.observe([step(7)])
    assert [asdict(signal) for signal in t.submit_attempt("third")] == expected["submit_hard"]
    assert [asdict(signal) for signal in t.peer_message("agent1", "")] == expected["peer_message_empty"]
    assert [asdict(signal) for signal in t.worker_finished("submitted")] == expected["finished"]
    assert t.worker_finished("submitted") == []


def test_overlap_signals_edge_trigger_each_key():
    comparison = fixture("evidence_snapshot")
    pair = tuple(comparison["pair"])
    evidence = GitEvidence(
        comparisons={
            pair: RegionComparison(
                overlaps=comparison["overlaps"],
                unknown=frozenset(comparison["unknown"]),
            )
        }
    )
    seen = set()
    assert overlap_signals(GitEvidence(), seen) == []
    assert [asdict(signal) for signal in overlap_signals(evidence, seen)] == fixture("expected_overlap")
    assert overlap_signals(evidence, seen) == []
    assert overlap_signals(GitEvidence(), seen) == []
    assert overlap_signals(evidence, seen) == []
    assert len(overlap_signals(GitEvidence(comparisons={pair: RegionComparison(overlaps={"src.py": "L9-9 / L9-9"})}), seen)) == 1


def test_recorded_det_main_signal_counts():
    recorded = fixture("messages_det_main")
    expected = fixture("expected_det_main")
    actual = {}
    for worker, messages in recorded["workers"].items():
        records = parse_steps(worker, messages, start=0, config=SignalConfig())
        actual[worker] = {"executed_steps": len(records), "signals": replay(SignalTracker(worker, SignalConfig()), records)}
    assert actual == expected


def test_structural_module_does_not_import_harness():
    tree = ast.parse(Path(structural_signals.__file__).read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and "harness" in (node.module or "")
        or isinstance(node, ast.Import)
        and any("harness" in name.name for name in node.names)
        for node in ast.walk(tree)
    )
