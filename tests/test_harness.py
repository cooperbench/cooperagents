"""The unified orchestrator: seeding, both modes, and dynamic spawning."""

from __future__ import annotations

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.llm import Action, ScriptedLLM
from cooperagents.policies import DemoPolicy
from cooperagents.types import Assignment, TeamSpec


def _harness(run_id="r"):
    return UnifiedHarness(bus=InMemoryBus(run_id))


def _features_spec(run_id="r", *, max_agents=None, allow_spawn=True):
    return TeamSpec(
        run_id=run_id,
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        max_agents=max_agents,
        allow_spawn=allow_spawn,
    )


def test_seed_only_run_no_helpers():
    res = _harness().run(_features_spec(max_agents=2), env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert set(res.seeds) == {"agent1", "agent2"}
    assert res.helpers == {}
    assert all(r.status == "submitted" for r in res.seeds.values())
    # feature ownership preserved
    assert res.seeds["agent1"].feature_id == 1
    assert res.seeds["agent2"].feature_id == 2


def test_dynamic_helper_spawned_when_cap_allows():
    res = _harness().run(_features_spec(max_agents=3), env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert len(res.helpers) == 1
    assert res.spawn_metrics["granted"] == 1
    assert res.spawn_metrics["requests_total"] == 1


def test_spawn_capped_at_max_agents():
    # Lead requests a helper but cap == seed count → request is refused.
    res = _harness().run(_features_spec(max_agents=2), env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert res.helpers == {}
    # With spawning disabled (cap == seeds) no spawn metrics are produced.
    assert res.spawn_metrics == {}


def test_spawn_disabled_flag():
    res = _harness().run(
        _features_spec(max_agents=5, allow_spawn=False),
        env_factory=lambda _id: LocalEnv.fresh(),
        llm=DemoPolicy(),
    )
    assert res.helpers == {}


def test_many_helpers_capped():
    # A policy where the lead asks for 5 helpers; cap allows only 2 extra.
    class Greedy:
        def __init__(self):
            self.n = {}

        def decide(self, *, agent_id, role, messages, tools):
            i = self.n.get(agent_id, 0)
            self.n[agent_id] = i + 1
            if role == "lead" and i < 5:
                return Action(tool="spawn_helper", args={"task": f"help {i}"})
            return Action(tool="finish")

    spec = TeamSpec(
        run_id="r2",
        repo="demo_task",
        task_id=1,
        features=[1],
        assignments=[Assignment(agent_id="agent1", role="lead", feature_id=1, task="t")],
        max_agents=3,  # 1 seed + 2 helpers
    )
    res = UnifiedHarness(bus=InMemoryBus("r2")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=Greedy())
    assert len(res.helpers) == 2
    assert res.spawn_metrics["granted"] == 2
    assert res.spawn_metrics["capped"] >= 1


def test_shared_mode_fans_objective_to_team():
    spec = TeamSpec(
        run_id="r3",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        objective="Build the whole thing together",
        team_size=2,
        max_agents=2,
    )
    res = UnifiedHarness(bus=InMemoryBus("r3")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert set(res.seeds) == {"agent1", "agent2"}
    assert res.seeds["agent1"].role == "lead"
    assert res.seeds["agent2"].role == "member"


def test_helper_works_in_isolated_env():
    # Each agent gets its own checkout; the helper's diff is its own.
    res = _harness("r4").run(_features_spec("r4", max_agents=3), env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    helper = next(iter(res.helpers.values()))
    assert "HELPER_NOTES.md" in helper.patch or helper.patch_lines > 0


def test_metrics_present():
    res = _harness("r5").run(_features_spec("r5", max_agents=2), env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert "tasks_total" in res.metrics
    assert res.total_steps > 0


def test_shared_workspace_produces_one_integrated_diff():
    # In shared mode both agents edit the SAME tree; the result is a single
    # coherent diff (no per-agent conflict), exposed as result.integrated.
    spec = TeamSpec(
        run_id="rs",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rs")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert res.integrated is not None
    # Both agents' NOTES files land in the one integrated diff.
    assert "NOTES_agent1.md" in res.integrated.patch
    assert "NOTES_agent2.md" in res.integrated.patch
    assert set(res.seeds) == {"agent1", "agent2"}


def test_shared_workspace_eval_output_is_single_patch(tmp_path):
    from cooperagents.eval.cooperbench import write_run_outputs

    spec = TeamSpec(
        run_id="rs2",
        repo="demo_task",
        task_id=7,
        features=[3, 4],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=3, task="f3"),
            Assignment(agent_id="agent2", role="member", feature_id=4, task="f4"),
        ],
        shared_workspace=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rs2")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    log_dir = write_run_outputs(res, run_name="exp", logs_dir=tmp_path, setting="team")
    # Integrated diff goes in the first feature slot; the rest are empty —
    # CooperBench then runs BOTH feature suites against it as a clean merge.
    assert (log_dir / "agent3.patch").read_text().strip() != ""
    assert (log_dir / "agent4.patch").read_text() == ""
    assert (log_dir / "integrated.patch").exists()


def test_verify_fix_runs_integrator_pass():
    # S5: with verify_fix, an extra "integrator" agent runs on the shared tree
    # after the feature agents, and its work lands in the integrated diff.
    spec = TeamSpec(
        run_id="rvf",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        verify_fix=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rvf")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert "integrator" in res.seeds
    assert "NOTES_integrator.md" in res.integrated.patch


def test_spec_fidelity_injected_into_prompt():
    # S8: the team prepends a spec-fidelity instruction to every agent's task.
    spec = TeamSpec(
        run_id="rsf",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        spec_fidelity=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rsf")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    joined = " ".join(m["content"] for m in res.seeds["agent1"].messages)
    assert "API fidelity" in joined


def test_teammate_context_seeds_prior_work_across_containers():
    # S2 under the own-container constraint: agent2's separate container is
    # seeded with agent1's committed diff (git apply), and its prompt points to it.
    spec = TeamSpec(
        run_id="rtc",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        teammate_context=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rtc")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    a2 = " ".join(m["content"] for m in res.seeds["agent2"].messages)
    assert "ALREADY in this repository" in a2  # prompt tells agent2 the prior work is present
    # Prior work was actually applied into agent2's separate container, so the
    # integrated diff (from agent2's env) contains agent1's file too.
    assert "NOTES_agent1.md" in res.integrated.patch
    assert "NOTES_agent2.md" in res.integrated.patch


def test_completeness_review_runs_reviewer():
    # T3: with completeness_review, a "reviewer" pass runs in its own container.
    spec = TeamSpec(
        run_id="rcr",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        completeness_review=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rcr")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert "reviewer" in res.seeds
    assert "NOTES_reviewer.md" in res.integrated.patch


def test_tdd_preamble_injected_into_prompt():
    # T2: each agent's prompt is prefixed with the self-verification workflow.
    spec = TeamSpec(
        run_id="rtdd",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        tdd_preamble=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rtdd")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    joined = " ".join(m["content"] for m in res.seeds["agent1"].messages)
    assert "acceptance criteria" in joined


def test_mine_conventions_injected_into_prompt():
    # T4: each agent's prompt is prefixed with the convention-mining workflow.
    spec = TeamSpec(
        run_id="rmc",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        mine_conventions=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rmc")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    joined = " ".join(m["content"] for m in res.seeds["agent1"].messages)
    assert "mine conventions" in joined


def test_no_seed_independent_plus_merge():
    # no-seed: agents work independently (own container, base only); a mechanical
    # merge combines their patches → integrated has both features' work.
    spec = TeamSpec(
        run_id="rns",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        seed_prior=False,
    )
    res = UnifiedHarness(bus=InMemoryBus("rns")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert res.integrated is not None
    assert "NOTES_agent1.md" in res.integrated.patch
    assert "NOTES_agent2.md" in res.integrated.patch


def test_best_of_n_runs_n_attempts_and_selects():
    # T6: best_of_n runs the isolated team N times (own containers each) and a
    # selector picks one candidate; metrics record the selection.
    calls = {"n": 0}

    def env_factory(_id):
        calls["n"] += 1
        return LocalEnv.fresh()

    spec = TeamSpec(
        run_id="rbo",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        best_of_n=3,
    )
    # selector always picks the last candidate
    res = UnifiedHarness(bus=InMemoryBus("rbo")).run(
        spec, env_factory=env_factory, llm=DemoPolicy(), selector=lambda cands: len(cands) - 1
    )
    # 2 agents x 3 attempts = 6 fresh containers (own-container constraint per attempt)
    assert calls["n"] == 6
    assert res.metrics["best_of_n"] == 3
    assert res.metrics["chosen_index"] == 2
    assert len(res.metrics["candidate_coverage"]) == 3
    assert res.integrated is not None


def test_best_of_n_default_selector_is_coverage():
    # With no selector, best-of-N falls back to the max-coverage candidate (offline-safe).
    spec = TeamSpec(
        run_id="rbo2",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        best_of_n=2,
    )
    res = UnifiedHarness(bus=InMemoryBus("rbo2")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert res.metrics["best_of_n"] == 2
    assert 0 <= res.metrics["chosen_index"] <= 1


def test_decompose_runs_dag_parallel_and_merges():
    # G1+G2: a planner emits 2 independent subtasks + 1 dependent integrator;
    # the harness runs them (own container each), seeds along edges, and merges.
    from cooperagents.types import SubTask

    def planner(specs, objective):
        return (
            [
                SubTask(id="a", task="feature 1", depends_on=[], features=[1]),
                SubTask(id="b", task="feature 2", depends_on=[], features=[2]),
                SubTask(id="c", task="integrate", depends_on=["a", "b"], features=[1, 2]),
            ],
            "a,b independent",
        )

    ids = []

    def env_factory(_id):
        ids.append(_id)
        return LocalEnv.fresh()

    spec = TeamSpec(
        run_id="rdc",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        decompose=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rdc")).run(spec, env_factory=env_factory, llm=DemoPolicy(), planner=planner)
    # one container per subtask + integrator (own-container constraint)
    assert {"a", "b", "c", "integrator"}.issubset(set(ids))
    assert res.metrics["decompose"] is True
    assert res.metrics["n_subtasks"] == 3
    assert res.metrics["n_edges"] == 2
    assert res.metrics["max_parallel"] == 2  # a,b run in parallel
    assert set(res.seeds) == {"a", "b", "c"}
    assert res.integrated is not None


def test_decompose_injects_ownership_writeset():
    # region-aware: two subtasks on the SAME file, disjoint owns → each agent's
    # prompt carries its write-set boundary and the teammate's off-limits region.
    from cooperagents.types import SubTask

    def planner(specs, objective):
        return (
            [
                SubTask(id="a", task="feature 1", owns=["m.py: foo()"], features=[1]),
                SubTask(id="b", task="feature 2", owns=["m.py: bar()"], features=[2]),
            ],
            "disjoint regions in m.py",
        )

    spec = TeamSpec(
        run_id="rdo",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        decompose=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rdo")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy(), planner=planner)
    a = " ".join(m["content"] for m in res.seeds["a"].messages)
    assert "m.py: foo()" in a  # owns its region
    assert "m.py: bar()" in a  # told teammate owns the other region
    assert res.metrics["max_parallel"] == 2  # same file, still parallel
    assert res.metrics["n_subtasks"] == 2


def test_decompose_default_planner_falls_back_offline():
    # With no planner injected and no API creds, plan_decomposition falls back to
    # one-subtask-per-feature (fully parallel) rather than crashing.
    spec = TeamSpec(
        run_id="rdc2",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        decompose=True,
    )

    # force the OpenAI path to be unavailable by passing a planner that mimics fallback
    from cooperagents.planner import fallback_plan

    res = UnifiedHarness(bus=InMemoryBus("rdc2")).run(
        spec,
        env_factory=lambda _id: LocalEnv.fresh(),
        llm=DemoPolicy(),
        planner=lambda specs, obj: (fallback_plan(specs), "fallback"),
    )
    assert res.metrics["n_subtasks"] == 2
    assert res.metrics["n_edges"] == 0  # fully parallel
    assert res.metrics["max_parallel"] == 2


def test_preserve_invariants_publishes_and_guards_checks():
    # C1: agent1 is told to PUBLISH a check for its feature; agent2 is told to
    # KEEP agent1's check green (the regression-guard coordination protocol).
    spec = TeamSpec(
        run_id="rpi",
        repo="demo_task",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id="agent1", role="lead", feature_id=1, task="feature 1"),
            Assignment(agent_id="agent2", role="member", feature_id=2, task="feature 2"),
        ],
        shared_workspace=True,
        preserve_invariants=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rpi")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    a1 = " ".join(m["content"] for m in res.seeds["agent1"].messages)
    a2 = " ".join(m["content"] for m in res.seeds["agent2"].messages)
    assert ".cb_checks/f1.py" in a1  # agent1 publishes its own invariant
    assert "TEAMMATE INVARIANTS" in a2 and ".cb_checks/f1.py" in a2  # agent2 must keep it green


def test_strip_for_submission_removes_scratch_checks():
    from cooperagents.patching import strip_for_submission

    patch = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,2 @@\n+x=1\n"
        "diff --git a/.cb_checks/f1.py b/.cb_checks/f1.py\n--- a/.cb_checks/f1.py\n+++ b/.cb_checks/f1.py\n@@ -0,0 +1 @@\n+assert True\n"
    )
    out = strip_for_submission(patch)
    assert "src/app.py" in out  # real code kept
    assert ".cb_checks/f1.py" not in out  # scratch check removed before grading


def test_verify_fix_skipped_for_single_agent():
    spec = TeamSpec(
        run_id="rvf2",
        repo="demo_task",
        task_id=1,
        features=[1],
        assignments=[Assignment(agent_id="agent1", role="lead", feature_id=1, task="f1")],
        shared_workspace=True,
        verify_fix=True,
    )
    res = UnifiedHarness(bus=InMemoryBus("rvf2")).run(spec, env_factory=lambda _id: LocalEnv.fresh(), llm=DemoPolicy())
    assert "integrator" not in res.seeds


def test_llm_factory_used_per_agent():
    seen = []

    def factory(agent_id, role):
        seen.append((agent_id, role))
        return ScriptedLLM({"*": [Action(tool="finish")]})

    res = _harness("r6").run(_features_spec("r6", max_agents=2), env_factory=lambda _id: LocalEnv.fresh(), llm_factory=factory)
    assert ("agent1", "lead") in seen and ("agent2", "member") in seen
    assert all(r.status == "submitted" for r in res.seeds.values())
