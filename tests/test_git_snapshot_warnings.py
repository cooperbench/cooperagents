"""Coordinator evidence checks use local Git and scripted inputs, never models or containers."""

import json
import shlex
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cooperagents.coordination_prompts import COORDINATOR_WARNING_RULES
from cooperagents.env.base import ExecResult
from cooperagents.env.local import LocalEnv
from cooperagents.git_snapshots import GitShareSync, _overlapping_regions
from cooperagents.harness import _Coordinator
from test_coordinator import coordinator


def history(commands, outputs=None, codes=None):
    messages = []
    outputs = outputs or ["error: unchanged"] * len(commands)
    codes = codes or [1] * len(commands)
    for index, (command, output, code) in enumerate(zip(commands, outputs, codes, strict=True)):
        call = f"call-{index}"
        messages.append({"role": "assistant", "extra": {"actions": [{"tool_name": "bash", "tool_call_id": call, "command": command}]}})
        if output is not None:
            messages.append({"role": "tool", "tool_call_id": call, "extra": {"raw_output": output, "returncode": code}})
    return messages


def observed(messages, **kwargs):
    requests = []
    c = coordinator(complete=lambda prompt: requests.append(prompt) or [], **kwargs)
    c.register("agent1", SimpleNamespace(n_calls=len(messages), messages=messages))
    c.decide()
    return json.loads(requests[-1].split("OBSERVATION:\n")[1]), requests[-1]


@pytest.mark.parametrize("size", [0, 1000, 1001, 2000])
def test_output_head_tail_metadata_and_no_execution_state(size):
    raw = "头" * 300 + "中" * max(0, size - 1000) + "尾" * 700 if size > 1000 else "字" * size
    observation, _ = observed(history(["pytest"], [raw], [0]))
    result = observation["workers"][0]["recent_actions"][0]["result"]
    assert result["output"] == (raw if len(raw) <= 1000 else raw[:300] + "\n[truncated]\n" + raw[-700:])
    assert result["output_chars"] == len(raw)
    assert result["output_truncated"] == (len(raw) > 1000)
    assert "state" not in result and "status" not in result
    assert "collected_at" not in observation
    assert "_loop_signature" not in json.dumps(observation)


@pytest.mark.parametrize(
    "commands,outputs,codes,loops",
    [
        (["pytest"] * 3, None, None, False),
        (["pytest"] * 4, None, None, True),
        ([f"cd /repo && pytest {i}" for i in range(6)], None, None, False),
        (["pytest"] * 4, ["same prefix" * 100 + str(i) for i in range(4)], None, False),
        (["pytest"] * 4, None, [0, 1, 2, 3], False),
        (["pytest"] * 4, [None] * 4, None, False),
        (["pytest"] * 4, ["x\n[output truncated after 2097152 bytes]"] * 4, None, False),
    ],
)
def test_loop_complete_signatures(commands, outputs, codes, loops):
    observation, _ = observed(history(commands, outputs, codes))
    warnings = observation["workers"][0]["warnings"]
    assert any(w.startswith("LOOP:") for w in warnings) == loops


def test_loop_window_compaction_and_full_command():
    commands = ["A" * 1000 + str(i) for i in range(4)]
    observation, _ = observed(history(commands))
    assert not any(w.startswith("LOOP:") for w in observation["workers"][0]["warnings"])
    records = _Coordinator._recent_actions(history(["old"] * 4 + ["new"] * 6))
    assert len(records) == 6 and all(r["action"]["command"] == "new" for r in records)
    observation, _ = observed(history(["new"] * 2))
    assert not any(w.startswith("LOOP:") for w in observation["workers"][0]["warnings"])


@pytest.mark.parametrize("variant", ["auto-prompt", "human-prompt"])
def test_rules_once_and_planning_exception(tmp_path, variant):
    _, prompt = observed([], notebook_path=tmp_path / "notebook.md", coordination_variant=variant, coordination_mode="group-chat-planning")
    for rule in filter(None, COORDINATOR_WARNING_RULES.strip().splitlines()):
        assert prompt.splitlines().count(rule) == 1
    assert "publishing explicitly proposed ownership or APIs" in prompt
    assert "Workers then confirm the exact candidate version." in prompt
    assert "do examples:" in prompt and "no do examples:" in prompt
    assert "not a required checklist or sequence" in prompt
    assert "You do not need to perform every example." in prompt


def test_stall_behavior_retains_output_prefix():
    observation, _ = observed(history([f"pytest {i}" for i in range(4)], ["error: " + "x" * 1500] * 4))
    assert any(w.startswith("STALL:") for w in observation["workers"][0]["warnings"])
    assert not any(w.startswith("LOOP:") for w in observation["workers"][0]["warnings"])


# Local repositories exercise real stash/push/diff primitives.

TEXT = "".join(f"value_{i} = {i}\n" for i in range(40))
NAMES = ["shared.py", "space name.py", "tab\tname.py", "line\nname.py", "-leading.py", "literal*.py", ":(glob)*"]


@pytest.fixture
def git_pair(tmp_path):
    source = LocalEnv.fresh(workdir=str(tmp_path))
    for name in NAMES:
        source.write_file(name, TEXT)
    assert source.execute("git add -A && git commit -qm source").exit_code == 0
    bare = tmp_path / "shared.git"
    assert source.execute("git init -q --bare " + shlex.quote(str(bare))).exit_code == 0
    envs = {aid: LocalEnv.clone(source.repo_path, workdir=str(tmp_path)) for aid in ["agent1", "agent2"]}
    heads = {}
    for aid, env in envs.items():
        heads[aid] = env.execute("git rev-parse HEAD").stdout.strip()
        assert env.execute("git remote add shared " + shlex.quote(str(bare))).exit_code == 0
        assert env.execute(f"git push -q shared HEAD:refs/heads/{aid}").exit_code == 0
    sync = GitShareSync(envs, observe=True, git_dir=str(bare))
    sync.initialize(heads, set(envs))
    yield sync, envs, heads
    for env in [source, *envs.values()]:
        env.cleanup()


def edit(env, line, value, name="shared.py"):
    lines = TEXT.splitlines(keepends=True)
    lines[line] = f"value_{line} = {value!r}\n"
    env.write_file(name, "".join(lines))


def snapshot_observation(sync, envs):
    from cooperagents.bus.memory import InMemoryBus
    from test_coordinator import ROSTER

    requests = []
    c = _Coordinator(envs, assignments=ROSTER, bus=InMemoryBus("snapshot"), complete=lambda p: requests.append(p) or [])
    c._git_sync = sync
    c.decide(initial=True)
    return json.loads(requests[-1].split("OBSERVATION:\n")[1])


@pytest.mark.parametrize("name", NAMES)
def test_same_file_disjoint_regions_and_literal_paths(git_pair, name):
    sync, envs, _ = git_pair
    edit(envs["agent1"], 3, "A", name)
    edit(envs["agent2"], 20, "B", name)
    before = {
        aid: (env.execute("git rev-parse HEAD").stdout, env.execute("git ls-files --stage -z").stdout, env.read_file(name))
        for aid, env in envs.items()
    }
    sync.publish_cycle()
    pair = sync.evidence().comparisons[("agent1", "agent2")]
    assert pair.error is None and name in pair.covered
    assert not pair.overlaps and not pair.unknown
    observation = snapshot_observation(sync, envs)
    assert all(not w["warnings"] for w in observation["workers"])
    assert all(s["sha"] for s in observation["git_snapshots"]["workers"].values())
    after = {
        aid: (env.execute("git rev-parse HEAD").stdout, env.execute("git ls-files --stage -z").stdout, env.read_file(name))
        for aid, env in envs.items()
    }
    assert before == after


@pytest.mark.parametrize("kind", ["replacement", "bof", "eof", "boundary"])
def test_overlap_regions_with_insertions(git_pair, kind):
    sync, envs, _ = git_pair
    if kind == "replacement":
        edit(envs["agent1"], 3, "A")
        edit(envs["agent2"], 3, "B")
    elif kind == "bof":
        for aid, env in envs.items():
            env.write_file("shared.py", f"# {aid}\n" + TEXT)
    elif kind == "eof":
        for aid, env in envs.items():
            env.write_file("shared.py", TEXT + f"# {aid}\n")
    else:
        envs["agent1"].write_file(
            "shared.py", "".join(TEXT.splitlines(keepends=True)[:3]) + "# insertion\n" + "".join(TEXT.splitlines(keepends=True)[3:])
        )
        edit(envs["agent2"], 3, "B")
    sync.publish_cycle()
    pair = sync.evidence().comparisons[("agent1", "agent2")]
    assert "shared.py" in pair.overlaps and pair.error is None
    observation = snapshot_observation(sync, envs)
    assert all(
        any(warning.startswith("POTENTIAL_CONFLICT: possible overlapping edits") for warning in w["warnings"])
        for w in observation["workers"]
    )


def test_identical_file_versions_suppress_snapshot_warning(git_pair):
    sync, envs, _ = git_pair
    for env in envs.values():
        edit(env, 3, "same")
    sync.publish_cycle()
    pair = sync.evidence().comparisons[("agent1", "agent2")]
    assert "shared.py" in pair.covered and not pair.overlaps and not pair.unknown
    assert all(not w["warnings"] for w in snapshot_observation(sync, envs)["workers"])


@pytest.mark.parametrize("kind", ["untracked", "tracked_addition", "deletion", "binary", "mode", "symlink", "rename"])
def test_unknown_file_structures(git_pair, kind):
    sync, envs, _ = git_pair
    path = "new.py" if kind in {"untracked", "tracked_addition"} else "shared.py"
    for aid, env in envs.items():
        if kind in {"untracked", "tracked_addition"}:
            env.write_file(path, aid + "\n")
            if kind == "tracked_addition":
                assert env.execute("git add new.py").exit_code == 0
        elif kind == "deletion" and aid == "agent1":
            assert env.execute("rm shared.py").exit_code == 0
        elif kind == "binary":
            Path(env.repo_path, path).write_bytes(b"\0" + aid.encode())
        elif kind == "mode" and aid == "agent1":
            assert env.execute("chmod +x shared.py").exit_code == 0
        elif kind == "symlink" and aid == "agent1":
            assert env.execute("rm shared.py && ln -s target shared.py").exit_code == 0
        elif kind == "rename" and aid == "agent1":
            assert env.execute("mv shared.py renamed.py && git add -A").exit_code == 0
        else:
            edit(env, 3, aid)
    sync.publish_cycle()
    observation = snapshot_observation(sync, envs)
    warnings = observation["workers"][0]["warnings"]
    assert any("regions unknown" in w and json.dumps(path) in w for w in warnings)
    if kind == "untracked":
        assert all("source=current path observation" in w for w in warnings)
        assert path not in sync.evidence().comparisons[("agent1", "agent2")].covered


def test_current_paths_missing_from_old_snapshot_are_unknown(git_pair):
    sync, envs, _ = git_pair
    for env in envs.values():
        env.write_file("new.py", "new untracked\n")
    observation = snapshot_observation(sync, envs)
    assert all(any("regions unknown" in w for w in worker["warnings"]) for worker in observation["workers"])
    for env in envs.values():
        env.execute = lambda *a, **k: ExecResult("unavailable", 1)
    observation = snapshot_observation(sync, envs)
    assert all(w["modified_files"] is None and not w["warnings"] for w in observation["workers"])


@pytest.mark.parametrize("code", [1, 2])
def test_capture_or_push_failure_retains_old_metadata(git_pair, code):
    sync, envs, _ = git_pair
    previous = sync.evidence().workers["agent1"]
    edit(envs["agent1"], 3, "A")
    edit(envs["agent2"], 3, "B")
    execute = envs["agent1"].execute
    envs["agent1"].execute = lambda command, **k: (
        ExecResult("", code) if command.startswith("C=$(git stash create") else execute(command, **k)
    )
    sync.publish_cycle()
    evidence = sync.evidence()
    assert evidence.workers["agent1"] == previous
    assert "agent1" in evidence.collection_errors
    assert evidence.comparisons[("agent1", "agent2")].error
    assert all(any("regions unknown" in w for w in worker["warnings"]) for worker in snapshot_observation(sync, envs)["workers"])


def test_cache_and_failed_new_comparison_cannot_reuse_old_result(git_pair):
    sync, envs, _ = git_pair
    edit(envs["agent1"], 3, "A")
    edit(envs["agent2"], 20, "B")
    sync.publish_cycle()
    execute = envs["agent1"].execute
    queries = []

    def count(command, **kwargs):
        queries.append(command)
        return execute(command, **kwargs)

    envs["agent1"].execute = count
    sync._refresh_comparisons()
    assert queries == []
    edit(envs["agent2"], 3, "new B")

    def fail(command, **kwargs):
        if "--git-dir=" in command:
            return ExecResult("missing object", 1)
        return execute(command, **kwargs)

    envs["agent1"].execute = fail
    sync.publish_cycle()
    assert sync.evidence().comparisons[("agent1", "agent2")].error
    assert sync.evidence().metadata()["comparison_errors"]
    envs["agent1"].execute = execute
    sync._refresh_comparisons()
    assert sync.evidence().comparisons[("agent1", "agent2")].overlaps


def test_common_baseline_mismatch_and_missing_initial_snapshot(git_pair):
    sync, envs, heads = git_pair
    sync.initialize({**heads, "agent2": "0" * 40}, set(envs))
    for aid, env in envs.items():
        edit(env, 3, aid)
    sync.publish_cycle()
    assert sync.evidence().base_sha is None
    assert all("regions unknown" in w for worker in snapshot_observation(sync, envs)["workers"] for w in worker["warnings"])
    other = GitShareSync(envs, observe=True)
    other.initialize(heads, {"agent1"})
    assert "agent2" in other.evidence().collection_errors


def test_query_budget_and_stop_never_execute(git_pair):
    sync, envs, _ = git_pair
    envs["agent1"].execute = lambda *a, **k: pytest.fail("query must not execute")
    with pytest.raises(TimeoutError):
        sync._query(envs["agent1"], ["diff"], time.monotonic() - 1)
    sync.stop()
    with pytest.raises(TimeoutError):
        sync._query(envs["agent1"], ["diff"], time.monotonic() + 5)


def test_stop_while_waiting_for_checkpoint_lock(git_pair):
    sync, envs, _ = git_pair
    envs["agent1"].execute = lambda *a, **k: pytest.fail("stopped capture must not execute")
    with sync._locks["agent1"]:
        thread = threading.Thread(target=sync.publish_cycle)
        thread.start()
        sync.stop()
    sync.finish(thread)
    assert not thread.is_alive()
    sync.publish_cycle()


@pytest.mark.parametrize(
    "a,b,overlap",
    [
        ([(1, 1)], [(2, 1)], False),
        ([(1, 1)], [(1, 1)], True),
        ([(0, 0)], [(1, 1)], True),
        ([(40, 0)], [(40, 1)], True),
        ([(2, 0)], [(4, 0)], False),
        ([(1, 1), (2, 0)], [(2, 1)], True),
    ],
)
def test_region_coordinates(a, b, overlap):
    assert (_overlapping_regions(a, b) is not None) == overlap


def test_only_six_outputs_hashed_and_unexecuted_results_excluded(monkeypatch):
    import cooperagents.harness as harness

    original = harness.hashlib.sha256
    hashed = []

    def digest(value):
        hashed.append(value)
        return original(value)

    monkeypatch.setattr(harness.hashlib, "sha256", digest)
    records = _Coordinator._recent_actions(history(["pytest"] * 20))
    assert len(records) == 6 and len(hashed) == 6
    messages = history(["pytest"] * 4, codes=[-1] * 4)
    for message in messages:
        if message["role"] == "tool":
            message["extra"]["exception_info"] = "action was not executed"
    observation, _ = observed(messages)
    assert not any(w.startswith("LOOP:") for w in observation["workers"][0]["warnings"])
    observation, _ = observed(history(["pytest"] * 4, codes=[None] * 4))
    assert not any(w.startswith("LOOP:") for w in observation["workers"][0]["warnings"])


def test_tool_call_id_is_preserved_without_truncation():
    messages = history(["pytest"])
    call = "call-" + "x" * 1500
    messages[0]["extra"]["actions"][0]["tool_call_id"] = call
    messages[1]["tool_call_id"] = call
    observation, _ = observed(messages)
    record = observation["workers"][0]["recent_actions"][0]
    assert record["action"]["tool_call_id"] == call and record["result"]["returncode"] == 1


def test_initial_head_query_failure_does_not_abort_team(monkeypatch):
    from dataclasses import replace

    from cooperagents.harness import UnifiedHarness
    from cooperagents.types import AgentResult
    from test_harness import _features_spec

    captures = []
    cleaned = []

    def env_factory(aid):
        def execute(command, **kwargs):
            if aid == "agent1" and command == "git rev-parse HEAD":
                raise RuntimeError("observation unavailable")
            return ExecResult("", 0)

        return SimpleNamespace(execute=execute, git_diff=lambda: "", cleanup=lambda: cleaned.append(aid))

    def worker(env, *, agent_id, role, monitor, **kwargs):
        monitor.register(agent_id, SimpleNamespace(messages=[]))
        monitor.mark_finished(agent_id, "submitted")
        return AgentResult(agent_id=agent_id, role=role, status="submitted")

    monkeypatch.setattr("cooperagents.workers.mini_swe_worker.run_mini_swe_agent", worker)
    spec = replace(
        _features_spec(),
        worker="mini_swe",
        shared_workspace=True,
        coop_tools=True,
        coordinator=True,
        coordinator_notebook=False,
        seed_prior=False,
        git_share=True,
    )
    result = UnifiedHarness(coordinator_complete=lambda p: captures.append(p) or []).run(spec, env_factory=env_factory)
    assert all(r.status == "submitted" for r in result.seeds.values())
    observation = json.loads(captures[0].split("OBSERVATION:\n")[1])
    assert observation["git_snapshots"]["base_sha"] is None
    assert "agent1" in observation["git_snapshots"]["collection_errors"]
    assert set(cleaned) == {"agent1", "agent2", "merge"}
