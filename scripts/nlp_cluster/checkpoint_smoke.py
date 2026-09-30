"""Real Apptainer/mini-SWE checkpoint round-trip using deterministic model replies."""

import argparse
import json
import os
import shutil
import subprocess
from contextlib import closing
from pathlib import Path

import litellm

from cooperagents.checkpoint import preview_patch, sha256, verify_checkpoint
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.eval.cooperbench import write_run_outputs
from cooperagents.harness import UnifiedHarness, _tree_health
from cooperagents.trajectory import Trajectory, replay
from cooperagents.types import Assignment, TeamSpec


def complete(**request):
    prompt = "\n".join(str(m.get("content", "")) for m in request["messages"] if m["role"] == "user")
    if "You are the merge integrator." in prompt:
        command = "printf 'package chi\\nconst checkpointSmoke = 1\\n' > checkpoint_bad.go"
    elif "checkpoint smoke worker1" in prompt:
        command = (
            "printf 'package chi\\nfunc checkpointBroken( {\\n' > checkpoint_bad.go; "
            "printf '.checkpoint-cache\\n' >> .git/info/exclude; "
            "printf 'ignored runtime state\\n' > .checkpoint-cache; "
            "printf 'staged\\n' > checkpoint_marker.txt; git add checkpoint_marker.txt; "
            "printf 'unstaged\\n' > checkpoint_marker.txt; "
            "printf '\\000\\001\\377' > checkpoint_binary; "
            "ln -s checkpoint_marker.txt checkpoint_link; chmod +x checkpoint_marker.txt; "
            "printf 'temporary state\\n' > /tmp/checkpoint-worker1"
        )
    else:
        command = "printf 'worker2\\n' > checkpoint_worker2.txt; cat /coordination/notebook.md >/dev/null"
    command += "; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
    return litellm.ModelResponse(
        choices=[dict(message=dict(role="assistant", content="Deterministic checkpoint smoke.", tool_calls=[dict(
            id="smoke", type="function", function=dict(name="bash", arguments=json.dumps(dict(command=command))),
        )]), finish_reason="tool_calls")],
        usage=dict(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    if sha256(args.image) != args.image_sha256:
        raise ValueError("Prepared task image checksum mismatch")
    args.run.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    # No real inference service or credentials: exercise the actual worker loop.
    os.environ.update(OPENAI_API_KEY="dummy-local-only", ENV_FILE="/dev/null", LITELLM_LOCAL_MODEL_COST_MAP="True")
    litellm.completion = complete
    spec = TeamSpec(run_id="checkpoint-smoke", repo="go_chi_task", task_id=27, features=[3, 4],
                    assignments=[Assignment(agent_id=f"agent{i}", task=f"checkpoint smoke worker{i}", feature_id=i + 2) for i in (1, 2)],
                    shared_workspace=True, seed_prior=False, coop_tools=True, git_share=True, coordinator=True,
                    worker="mini_swe", model="openai/dummy", repair_integrator=True, repair_attempts=2, repair_step_limit=2)
    pair = args.run / "logs/real/team/go_chi_task/27/f3_f4"
    notes = pair / "coordination/notebook.md"
    shared = args.scratch / "shared"
    with closing(Trajectory(pair / "trajectory.jsonl.gz")) as journal:
        journal.emit("harness", "pair_start")
        result = UnifiedHarness(trajectory=journal, checkpoint_dir=pair / "checkpoints", step_limit=2,
                                coordinator_notebook_path=notes, coordinator_complete=lambda _: []).run(
            spec, env_factory=lambda aid: ApptainerEnv(
                str(args.image), scratch=str(args.scratch), shared=str(shared),
                coordinator_dir=notes.parent if aid.startswith("agent") else None,
            ),
        )
        journal.emit("harness", "pair_end")
    write_run_outputs(result, run_name="real", logs_dir=args.run / "logs")
    (args.run / "metadata.json").write_text(json.dumps(dict(pairs=["go_chi_task:27:3,4"], coordinator=True,
                                                           checkpoint_repair=True, synthetic_model=True)))
    assert set(result.seeds) == {"agent1", "agent2", "integrator1"}
    assert all(r.status == "submitted" for r in result.seeds.values())
    checkpoints = pair / "checkpoints"
    assert verify_checkpoint(checkpoints / "pre-repair")["metadata"]["healthy"] is False
    assert verify_checkpoint(checkpoints / "post-repair")["metadata"]["healthy"] is True
    for name in ("worker-agent1", "before-integrator1", "post-repair"):
        path = checkpoints / name
        state = verify_checkpoint(path)
        restored_share = args.scratch / f"restore-{name}-share"
        restored_notes = args.scratch / f"restore-{name}-notes"
        for mount in state["runtime"]["mounts"]:
            target = restored_share if mount["target"] == "/cbshared" else restored_notes
            target.mkdir()
            subprocess.run(["tar", "-xzf", str(path / mount["archive"]), "-C", str(target), "--no-same-owner"], check=True)
        env = ApptainerEnv(str(args.image), scratch=str(args.scratch),
                           shared=str(restored_share) if state["runtime"]["mounts"] else None,
                           coordinator_dir=restored_notes if restored_notes.exists() else None)
        try:
            shutil.rmtree(env.root / "fs")
            (env.root / "fs").mkdir()
            subprocess.run(["tar", "-xzf", str(path / "rootfs.tar.gz"), "-C", str(env.root / "fs"), "--no-same-owner"], check=True)
            env._base_commit = state["base_commit"]
            status = env.execute("GIT_OPTIONAL_LOCKS=0 git status --porcelain=v1 --untracked-files=all")
            assert status.exit_code == 0 and status.stdout == state["git"]["status"]["stdout"]
            if name == "worker-agent1":
                assert env.read_file(".checkpoint-cache") == "ignored runtime state\n"
                assert env.execute("git show :checkpoint_marker.txt").stdout == "staged\n"
                assert env.read_file("checkpoint_marker.txt") == "unstaged\n"
                assert env.execute("cat /tmp/checkpoint-worker1").stdout == "temporary state\n"
                assert env.execute("test -L checkpoint_link && test -x checkpoint_marker.txt").exit_code == 0
                assert env.execute("od -An -tu1 checkpoint_binary").stdout.split() == ["0", "1", "255"]
                assert env.read_file("/coordination/notebook.md") == notes.read_text()
                assert env.execute("git --git-dir=/cbshared/repo.git rev-parse agent1").exit_code == 0
            else:
                assert preview_patch(env) == (path / "raw.patch").read_text()
                assert _tree_health(env) == (name == "post-repair")
        finally:
            env.cleanup()
    assert not replay(pair / "trajectory.jsonl.gz")["pending_calls"]
    subprocess.run(["python", "scripts/audit_trajectories.py", str(args.run)], check=True)
    summary = dict(passed=True, synthetic_model=True, real_runtime="apptainer", restored=3,
                   repair_agents=1, image_sha256=args.image_sha256,
                   checkpoint_bytes=sum(p.stat().st_size for p in checkpoints.rglob("*") if p.is_file()))
    (args.run / "smoke.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
