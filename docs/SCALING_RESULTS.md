# Scaling-benchmark results (cooperagents)

Measured success rates for the cooperagents in-place stack on the
Scaling-Agent-Systems benchmarks (arXiv:2512.08296). Model: `sap/gemini-2.5-flash`
via SAP AI Core. Lead with the **relative solo→team delta** (robust to
scorer-version differences); absolute rates are system-to-system (our agent loop
is not the paper's scaffold), so they place roughly rather than reproduce.

Arms:

- `solo` — 1 agent (single-agent baseline).
- `solo-iso` — 1 agent granted `team_size × step_limit` total steps (the
  equal-total-compute control).
- `team` — `team_size` agents sharing a bus + coordinator, reconciled by a reducer.

## Latest sweep (2026-10-21)

Full-set runs on three benchmarks, `team_size = 3`. PlanCraft (110) and BrowseComp
(100) ran their full splits. WorkBench ran all six domains (690 instances total).

| Benchmark | n | solo | solo-iso | team (3) | team vs solo | team vs solo-iso |
| --- | --- | --- | --- | --- | --- | --- |
| PlanCraft (val.small) | 110 | 0.155 | 0.191 | 0.164 | +5.9% | -14.3% |
| WorkBench (pooled, 6 domains) | 690 | 0.488 | 0.483 | 0.542 | +11.0% | +12.3% |
| BrowseComp (test) | 100 | 0.010 | 0.010 | 0.020 | +100.0% | +100.0% |

WorkBench per-domain:

| Domain | n | solo | solo-iso | team (3) | team vs solo |
| --- | --- | --- | --- | --- | --- |
| email | 90 | 0.400 | 0.344 | 0.722 | +80.6% |
| calendar | 110 | 0.682 | 0.673 | 0.755 | +10.7% |
| analytics | 120 | 0.508 | 0.508 | 0.592 | +16.4% |
| project_management | 80 | 0.463 | 0.487 | 0.425 | -8.1% |
| customer_relationship_manager | 80 | 0.512 | 0.512 | 0.463 | -9.8% |
| multi_domain | 210 | 0.414 | 0.414 | 0.400 | -3.4% |

Run dirs: `runs/scaling/sweep_plancraft`, `runs/scaling/sweep_browsecomp`, and the
six `runs/scaling/sweep_workbench*` directories. All arms ran to completion; exit 0.

## Findings

### WorkBench — team gain concentrated in the email domain

Pooled across the six domains (690 instances), the team arm reaches 0.542 against
solo 0.488 (+11.0%) and solo-iso 0.483 (+12.3%). The per-domain spread is wide:
email +80.6%, analytics +16.4%, and calendar +10.7% are positive; multi_domain
-3.4%, project_management -8.1%, and customer_relationship_manager -9.8% are
negative. The email result (+80.6%) does not reproduce in the other five domains.
The email split consists of short tool-use tasks where the best-of-3
`lead_selection` reducer (the lead picks the best of three independent attempts)
raises the pass rate; domains with longer or more state-dependent tasks show a
smaller gain or a regression. The pooled +11.0% is the result for the full
WorkBench set.

An earlier partial run of multi_domain (171 of 210 team tasks) reported team 0.474;
the completed 210-instance run gives 0.400. The remaining 39 tasks resolved mostly
to failures for the team arm, so the partial figure was biased by task ordering.

### PlanCraft — small team gain over solo, below the iso-compute control

On the full 110 instances: team 0.164 against solo 0.155 (+5.9%) and solo-iso 0.191
(-14.3%). The team arm exceeds the single-agent baseline by a small margin and falls
below the equal-total-compute solo-iso baseline. This is consistent with the paper's
finding that multi-agent does not help the strongly-sequential crafting task. Prior
full 110-instance runs at other team sizes:

| Run | solo | solo-iso | team | team vs solo | team vs solo-iso |
| --- | --- | --- | --- | --- | --- |
| team_size = 2 (`runs/scaling/plancraft_full`) | 0.173 | 0.200 | 0.209 | +21.1% | +4.5% |
| team_size = 3 (`runs/scaling/plancraft_team3`) | 0.191 | 0.191 | 0.236 | +23.8% | +23.8% |

Caveats on the PlanCraft team arm (they bound the interpretation):

- **Oracle pass@N upper bound.** Each agent runs its own simulator; the reducer
  `select_by((success, -steps))` reads the gym's ground-truth reward, so the team
  passes if *any* of its N episodes passed. That is pass@N with an oracle
  selector, not an operating point a deployed system reaches without the grader.
- **Realized compute is not matched.** PlanCraft episodes terminate early, so a
  single agent granted the full team budget stops well short of it; the team's
  realized compute scales with team_size (team_size = 3: 2.16× the steps / 1.99×
  the cost of solo-iso). The gain is a best-of-N restart-sampling effect.
- **Missing control.** A `solo-best-of-N` arm (N independent single-agent episodes
  with the same outcome selection) is the sampling- and compute-matched baseline;
  until run, the +5 team solves at team_size = 3 are attributable to best-of-3
  restart sampling. McNemar on the team_size = 3 pairs gives p = 0.062 (borderline).

### BrowseComp — near the floor

On the full 100 instances, all arms are near zero (team 2/100, both solo arms
1/100). `sap/gemini-2.5-flash` does not reach the capability required for this
retrieval-reasoning task. The +100% relative delta is 2 solves against 1 —
directional only, below the threshold for significance.

### Coordination occurs only with the FACTORY23 stack

cooperagents' team is its own "5th MA setup": isolated per-agent environments
coordinating in real time over a shared bus, reconciled by a reducer.

- **Bare bus: coordination does not occur.** An instrumented PlanCraft team run
  (team_size = 3) recorded **0 bus messages on every task** — agents never
  broadcast or messaged, even while failing to the step limit.
- **FACTORY23-portable stack: coordination engages.** Adding the portable half of
  the FACTORY23 config (a coordinator delivering loop/stall nudges + a task-board
  protocol + a benchmark `brief()`, all default-on for the team arm) lifts uptake
  from zero:

  | signal | bare bus | +coordinator +task_board |
  | --- | --- | --- |
  | bus messages / task | 0 | 2–3 |
  | `task_create` calls (run total) | 0 | 20 |
  | coordinator stall-nudges | n/a | 7 |
  | agent-initiated `broadcast` | 0 | 0 |

  Agents do not free-form broadcast, but they readily use the *structured* channels
  (task board + coordinator nudges). On the git substrate (SWE-bench coopgitc2) the
  same coordinator fired 9 nudges that measurably changed agent behaviour. Uptake is
  real and measurable once the mechanisms exist; whether it *helps* is task-dependent
  (big on WorkBench, nil on PlanCraft). Toggle with `scaling_box.py --no-coordinator`
  / `--no-task-board`.

### Compute balancing vs the paper

The paper claims a compute-matched design in its text (since v1): matched maximum
total iterations between MAS and SAS, "all systems matched for total reasoning
tokens (mean ~4,800 per trial)," plus overhead `O%` and efficiency `E_c` metrics.
Its released code (through tag v2.2.3) does **not** implement this — single-agent is
hard-capped at 100 steps while each MAS gets `3 × 10 × 25 ≈ 750` LLM calls, and
`success_per_1k_tokens` is computed but dropped from the regression. The matching is
a stated methodology, not a shipped one, across all three paper versions.
cooperagents implements the equal-total-compute comparison via the **`solo-iso` vs
`team`** arms (both receive `team_size × step_limit` total steps).

## Measurement integrity

Runs pass the per-task fault detectors:

- Instance-fingerprint consistency across arms (detects the reused/mutated-instance
  fault that produced spurious step-count-zero episodes): 0 divergences.
- Team episodes with zero steps (detects a spent instance selected by the reducer): 0.

Per-task records (success, steps, cost, duration, scorer detail, per-agent
breakdown, trajectory pointers) are in each run dir under `tasks.jsonl`.

Known defect: the `solo-iso` arm's per-task **duration** readout is wrong (absurd
values like 12521 s next to 22 s); success/steps/cost are unaffected. See TODO.

## Reproduction

```
uv run python scripts/scaling_box.py --benchmark plancraft --split val.small --models sap/gemini-2.5-flash --team-size 3 --step-limit 15 --out-dir runs/scaling/plancraft
```

Add `--resume <out-dir>` to reuse finished tasks and run only the remainder.

## In progress (2026-10-22): Terminal-Bench-core and SWE-bench Verified

These two adapter benchmarks are being run locally (Apple Silicon arm64, Docker).
Model `sap/gemini-2.5-flash`. Results are filled in as each run completes.

### Terminal-Bench-core (light subset)

New driver `scripts/tb_core_box.py` runs the adapter path (no StateBenchmark
`box_sweep` driver exists for it): per task it runs the agent via
`bench_programbench.run_team_once`, writes the git diff as `integrated.patch`,
then scores every patch in a separate phase with `TerminalBenchCoreAdapter.evaluate`
(fresh container, the task's own pytest). Arms: `solo`, `solo-iso`
(`step_limit * team_size`), `team` (team_size 3, coordinator, git-share;
integration selects the lead's non-empty tree — TB tasks are single-objective, so
best-of-N selection, not a 3-way merge of independent full solutions). Subset is
15 light single-container tasks (flat Dockerfile, multi-arch base, no
kernel/qemu/ML/large-download); the 5 nested-layout tasks are excluded (the
adapter's `image()` cannot build them). Verified on `hello-world`: reward 1.0.

Result (2026-10-23) — degraded by the model-endpoint outage, see caveat:

| Arm | n (scored) | success rate | Status |
| --- | --- | --- | --- |
| solo | 14/15 | 0.357 (5/14) | Complete (degraded) |
| solo-iso | 14/15 | 0.286 (4/14) | Complete (degraded) |
| team (3) | 14/15 | 0.286 (4/14) | Complete (degraded) |

`fix-git` (1 of 15) is excluded from every arm: its image does not build on arm64,
so it scores None rather than 0. Passing tasks are the simple ones (hello-world,
fix-permissions, organization-json-generator, heterogeneous-dates,
new-encrypt-command).

Caveat — the arm comparison is contaminated. A SAP AI Core outage during the run
caused `Connection timed out` on many model calls; the solo and solo-iso arms each
lost 4 tasks to empty patches (the agent produced no diff before the endpoint
failed), while the team arm lost only 1. The arm deltas (team vs solo -20%) reflect
that uneven loss, not coordination. These numbers place the pipeline, not the
method; a clean re-run when the endpoint is healthy is required (resume reuses the
non-empty patches).

Run dir: `runs/scaling/tbcore_light/`.

### SWE-bench Verified (6-instance graded run)

Two patch-hygiene fixes landed first (see TODO entries below). Instances (distinct
repos, small gold patches, arm64 images confirmed): `pylint-dev__pylint-4551`,
`pallets__flask-5014`, `sympy__sympy-22914`, `django__django-16082`,
`pytest-dev__pytest-6202`, `sphinx-doc__sphinx-8621`. Grade set
`data/swebench_grade_six.jsonl` (official Verified rows, `image` rewritten to the
`sweb.eval.arm64.*` tag). Arms: `solo` and `coopgitc2` (team 3). Grading is the
official `swebench.harness.run_evaluation`. An arm64 grade is not
leaderboard-canonical (canonical is x86_64).

Hygiene fix confirmed (2026-10-22): a flask-5014 solo run now submits a 517-byte
patch touching only `src/flask/blueprints.py` — no scratch files (the prior smoke
submission was scratch-polluted).

Result (2026-10-23):

| Arm | resolved | n | Status |
| --- | --- | --- | --- |
| solo | 3 | 6 | Complete |
| team (3) | 3 | 6 | Complete |

Both arms resolve the same three instances (django-16082, pytest-6202,
sympy-22914). Solo: flask-5014 unresolved, pylint-4551 `no_tests_collected`,
sphinx-8621 empty patch (the agent produced no diff during a model-endpoint
timeout window). Team: all six graded cleanly — 0 `no_tests_collected`, 0
ambiguous, 0 empty; the three unresolved cases (flask-5014, pylint-4551,
sphinx-8621) failed on test outcome, not patch hygiene.

Patch-hygiene fix outcome: the pre-fix team submission was ~105 KB of scratch and
failed `no_tests_collected`; post-fix, five of six team patches are clean and the
blocker is gone. Residual: the django-16082 team patch still carried one scratch
helper (`modify_expressions.py`) — the team arm can commit scratch into a shared
branch where tracked-only staging does not remove it (did not affect this grade;
django still resolved). No team benefit on this set (team 3 = solo 3); n=6 is a
plumbing check, not a measurement.

Run dir: `runs/sweb_small/`; grade reports `cooperagents.sweb_small_{solo,team}.json`.

## TODO — required additions and unfinished benchmarks

Scale-ups:

- [x] **WorkBench full 690** — all six domains complete (2026-10-21). Pooled team
  +11.0%; the +80.6% email win does not reproduce in the other five domains.
- [x] **BrowseComp full 100** — complete (2026-10-21). team 2/100 vs solo 1/100,
  near the floor; not significant.
- [x] **PlanCraft full 110** — complete (2026-10-21). team +5.9% vs solo, -14.3%
  vs solo-iso.
- [ ] **Repeats for significance.** All sweep numbers are single runs; run-to-run
  variation on PlanCraft solo was ~2 tasks. Repeat k≥3 for the headline deltas.
  The multi_domain partial-vs-complete gap (0.474 → 0.400) shows the size of the
  ordering effect on a single run.

Benchmarks not yet run at scale:

- [ ] **Finance-Agent** — not run; needs `TAVILY_API_KEY` (data `data/finance_open50.csv`
  and the AI Core grader are ready). Only the open-50 split is public; the full set
  and official Vals grader are gated.
- [x] **SWE-bench Verified** — patch-hygiene fixes landed (2026-10-22) and a
  6-instance graded run completed (2026-10-23): solo 3/6, team 3/6 (see "In progress"
  section). The blocker is cleared — the team arm now grades with 0 `no_tests_collected`.
  Fixes: (a) the submitted diff excludes untracked scratch for SWE-bench —
  `git_diff()` stages tracked edits only (`git add -u`) when the adapter sets
  `include_untracked_in_diff = False`; the `patch.txt` instruction in the mini-swe
  configs now writes to `/tmp`; (b) the `presub-merge` selector no longer ranks by
  patch size — with no build/reference signal it selects the lead's non-empty tree.
  Per-instance images are multi-GB (arm64 locally; x86_64 is the canonical grading arch).
- [ ] **SWE-bench team-arm scratch leak (residual)** — the django-16082 team patch
  still carried a scratch helper (`modify_expressions.py`). The team shares work by
  committing to a shared branch (`_apply_commit` / the scratchpad export use
  `git add -A`), so scratch becomes tracked and survives `git add -u`. Scope the
  shared-branch staging for SWE-bench, or strip untracked-origin files before commit.
- [x] **Terminal-Bench-core** — adapter verified on `hello-world` (reward 1.0); a
  15-task light-subset run (solo / solo-iso / team) completed via the new
  `scripts/tb_core_box.py` driver (see "In progress" section). `evaluate()` was made
  fault-tolerant (a task whose image fails to build scores None and no longer aborts
  the batch). The run was degraded by a model-endpoint outage — a clean re-run is
  required before the arm comparison is usable. `fix-git` does not build on arm64.
  Broader coverage of the 77 single-container tasks and the heavy/nested-layout
  tasks remains.
- [ ] **Terminal-Bench-core clean re-run** — repeat the 15-task light subset when SAP
  AI Core is healthy (no timeouts). `uv run python scripts/tb_core_box.py --resume
  runs/scaling/tbcore_light` reuses non-empty patches; delete the empty-patch task
  dirs first so their agents re-run.

Required additions:

- [ ] **`solo-best-of-N` control arm** — the sampling- and compute-matched baseline
  for the team arm; needed to attribute PlanCraft's team gain to structure vs restart
  sampling.
- [ ] **Fix the `solo-iso` duration-measurement artifact** (wrong per-task wall-clock).
- [ ] **Per-task `task_events` counts** — the `messages` metric counts send/broadcast
  only, not `task_create`/`task_claim`, so task-board coordination is under-reported
  in the persisted records.
