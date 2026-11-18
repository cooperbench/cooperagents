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

This doc lists only the **most recent result per benchmark**. Superseded runs and
rerun history (the pre-loop-fix sweep, the pre→post comparison, the team-reducer bug,
the local arm64 SWE/TB grades, and prior PlanCraft team sizes) are in
[OLD_RESULTS.md](OLD_RESULTS.md). No further runs are planned for the time being.

> **Model-availability note (2026-11-17).** Every number in this doc was produced
> with `sap/gemini-2.5-flash` served through the SAP AI Core **orchestration** path
> (litellm `sap/` → `/v2/completion`). As of 2026-11-17 SAP's orchestration catalog
> dropped the entire Gemini 2.x generation, so that path no longer serves 2.5-flash
> by any name (it now offers gemini-3.1/3.5/3.6/3.8-flash, gemini-4-argon, etc.). The
> model itself is NOT retired — a dedicated `foundation-models` deployment
> (`da8af67d8c6439bc`, v001) is still running and reachable via the raw Gemini
> `:generateContent` API, but only through a direct-deployment client (not litellm's
> orchestration-only `sap/` provider). 2.5-flash is the paper's middle Google tier
> (the paper ran Gemini-2.0 Flash / 2.5 Flash / 2.5 Pro, GPT-5-nano/mini/5, Claude
> Sonnet 3.7/4/4.5), so keeping it is paper-level reproducibility. Consequence for
> these results: they stand as the orchestration-path measurement, but have NOT been
> re-verified on the direct-deployment path — the two serving paths may not be
> byte-identical, so a small overlap re-run is advisable before comparing new
> direct-path runs against these.

## State benchmarks (full sets, team_size = 3)

| Benchmark | n | solo | solo-iso | team (3) | team vs solo | team vs solo-iso |
| --- | --- | --- | --- | --- | --- | --- |
| PlanCraft (val.small) | 110 | 0.155 | 0.200 | 0.191 | +23.2% | -4.5% |
| WorkBench (pooled, 6 domains) | 690 | 0.506 | 0.507 | 0.596 | +17.8% | +17.6% |
| BrowseComp (test) | 100 | 0.110 | 0.140 | 0.090 | -18.2% | -35.7% |
| Finance-Agent (open-50) | 50 | 0.100 | 0.080 | 0.080 | -20.0% | +0.0% |

WorkBench per-domain (team arm):

| Domain | n | team (3) |
| --- | --- | --- |
| email | 90 | 0.833 |
| calendar | 110 | 0.818 |
| analytics | 120 | 0.575 |
| project_management | 80 | 0.500 |
| customer_relationship_manager | 80 | 0.512 |
| multi_domain | 210 | 0.457 |

Run dirs: `runs/scaling/wb_*_v2` (WorkBench), `runs/scaling/plancraft_*_v2`,
`runs/scaling/*_v3` (BrowseComp/Finance). Full per-domain solo/solo-iso breakdown is
in [OLD_RESULTS.md](OLD_RESULTS.md).

## Terminal-Bench-core (14-task light subset, x86_64 canonical)

Driver `scripts/tb_core_box.py` runs the adapter path (no StateBenchmark `box_sweep`
driver exists for it): per task it runs the agent via
`bench_programbench.run_team_once`, writes the git diff as `integrated.patch`, then
scores every patch in a separate phase with `TerminalBenchCoreAdapter.evaluate`
(fresh container, the task's own pytest). Arms: `solo`, `solo-iso`
(`step_limit * team_size`), `team` (team_size 3, coordinator, git-share; integration
selects the lead's non-empty tree — TB tasks are single-objective, so best-of-N
selection, not a 3-way merge). Subset is 14 light single-container tasks (flat
Dockerfile, multi-arch base, no kernel/qemu/ML/large-download); the 5 nested-layout
tasks and `fix-git` (git-clones at build time) are excluded. `evaluate()` is
fault-tolerant: a task whose image fails to build scores None and does not abort the
batch.

Result (x86_64, AWS 2026-11-06; canonical arch, pipeline/teardown in
`benchmark_aws.md`):

| Arm | n (scored) | success rate |
| --- | --- | --- |
| solo | 14 | 0.214 (3/14) |
| solo-iso | 14 | 0.143 (2/14) |
| team (3) | 14 | 0.214 (3/14) |

team vs solo +0.0%; team vs solo-iso +49.6% (3 vs 2 — directional only). No team
benefit. Single run. Four light tasks (nginx-request-logging, sanitize-git-repo,
jupyter-notebook-server, polyglot-c-py) yield empty patches under this model. The
broader 44-task coverage run did not complete (blocked by the orchestration
model-retirement above). Run: `s3://cooperagents-bench-361202841353/cooperagents/remote2/`.

## SWE-bench Verified (6 instances, x86_64 canonical)

Instances (distinct repos, small gold patches): `pylint-dev__pylint-4551`,
`pallets__flask-5014`, `sympy__sympy-22914`, `django__django-16082`,
`pytest-dev__pytest-6202`, `sphinx-doc__sphinx-8621`. Grade set
`data/swebench_grade_six.jsonl` (official Verified rows). Arms: `solo` and
`coopgitc2` (team 3). Grading is the official `swebench.harness.run_evaluation`.

Result (x86_64, AWS 2026-11-06; canonical grading arch, full image coverage):

| Arm | resolved | n |
| --- | --- | --- |
| solo | 4 | 6 |
| team (3) | 2 | 6 |

Solo resolved django-16082, pytest-6202, sphinx-8621, sympy-22914 (all clean patches).
Team resolved django-16082 and flask-5014; the team's pytest-6202 graded
`missing_module` and sphinx-8621 `no_tests_collected` — team-patch hygiene, a known
open follow-up (not the fixed scratch-leak). On this n=6 set team (2) < solo (4); it
is a plumbing/throughput validation, not a measurement. The pipeline ran end-to-end
on an on-demand box, resume-safe, and self-terminated with no restarts. Run:
`s3://cooperagents-bench-361202841353/cooperagents/remote2/`.

Patch-hygiene fixes (all validated on real runs):
- Submitted diff excludes untracked scratch for SWE-bench (`git add -u`); `patch.txt`
  redirected to `/tmp`.
- `presub-merge` selector no longer ranks by patch size (selects the lead's non-empty
  tree when there is no build/reference signal).
- Team-arm shared-commit leak closed: the `git add -A` in the merge gate
  (`verification.py`) and the harness commit helpers (`_seed_patch`, `_apply_commit`,
  `seed_prior`) is gated on `_include_untracked`, so SWE-bench stages tracked-only on
  those paths too; CooperBench (flag defaults True) is unchanged.

## Findings

### WorkBench — broad team gain after the loop fix, strongest in email

Pooled across the six domains (690 instances), the team arm reaches 0.596 against
solo 0.506 (+17.8%) and solo-iso 0.507 (+17.6%). The per-domain spread is wide but
now mostly positive: email is strongest (0.833), and the format-retry fix flipped the
three previously-negative domains (project_management, customer_relationship_manager,
multi_domain) positive versus solo. Short tool-use tasks (email) gain most from the
best-of-3 `lead_selection` reducer (the lead picks the best of three independent
attempts); longer or more state-dependent tasks gain less. (Pre-fix the gain was
+11.0% and concentrated almost entirely in email — see OLD_RESULTS.md.)

### PlanCraft — small team gain over solo, below the iso-compute control

On the full 110 instances: team 0.191 against solo 0.155 (+23.2%) and solo-iso 0.200
(-4.5%). The team arm exceeds the single-agent baseline but falls just below the
equal-total-compute solo-iso baseline. This is consistent with the paper's finding
that multi-agent does not help the strongly-sequential crafting task.

Caveats on the PlanCraft team arm (they bound the interpretation):

- **Oracle pass@N upper bound.** Each agent runs its own simulator; the reducer
  `select_by((success, -steps))` reads the gym's ground-truth reward, so the team
  passes if *any* of its N episodes passed. That is pass@N with an oracle selector,
  not an operating point a deployed system reaches without the grader.
- **Realized compute is not matched.** PlanCraft episodes terminate early, so a
  single agent granted the full team budget stops well short of it; the team's
  realized compute scales with team_size (team_size = 3: 2.16× the steps / 1.99× the
  cost of solo-iso). The gain is a best-of-N restart-sampling effect.
- **Missing control.** A `solo-best-of-N` arm (N independent single-agent episodes
  with the same outcome selection) is the sampling- and compute-matched baseline;
  until run, the team solves are attributable to best-of-N restart sampling. McNemar
  on the team_size = 3 pairs gives p = 0.062 (borderline).

### BrowseComp — team underperforms solo near the capability floor

On the full 100 instances, after the loop + answer-veto fixes: solo 0.110, solo-iso
0.140, team 0.090. The reducer fix recovered real team answers (team was 0.000 pre-fix
from a visited-doc bug — see OLD_RESULTS.md), but the team arm (0.090) underperforms
both solo arms here. `sap/gemini-2.5-flash` is near its capability floor on this
retrieval-reasoning task and the team structure does not help. Counts are small
(≈9–14 solves / 100), so deltas are directional only.

### Finance-Agent — plumbing fixed, model-capability limited

On the open-50 split, after the keyless-search (DuckDuckGo) and tool-crash fixes:
solo 0.100, solo-iso 0.080, team 0.080 (team was 0.000 pre-fix from HTTP-403 and
coordination-tool crashes). `gemini-flash` often fails to emit a final `submit_answer`
even with the finish-veto — a model-capability limit, not plumbing.

### Coordination occurs only with the FACTORY23 stack

cooperagents' team is its own "5th MA setup": isolated per-agent environments
coordinating in real time over a shared bus, reconciled by a reducer.

- **Bare bus: coordination does not occur.** An instrumented PlanCraft team run
  (team_size = 3) recorded **0 bus messages on every task** — agents never broadcast
  or messaged, even while failing to the step limit.
- **FACTORY23-portable stack: coordination engages.** Adding the portable half of the
  FACTORY23 config (a coordinator delivering loop/stall nudges + a task-board protocol
  + a benchmark `brief()`, all default-on for the team arm) lifts uptake from zero:

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

Per-task records (success, steps, cost, duration, scorer detail, per-agent breakdown,
trajectory pointers) are in each run dir under `tasks.jsonl`.

Known defect: the `solo-iso` arm's per-task **duration** readout is wrong (absurd
values like 12521 s next to 22 s); success/steps/cost are unaffected. See TODO.

## Reproduction

```
uv run python scripts/scaling_box.py --benchmark plancraft --split val.small --models sap/gemini-2.5-flash --team-size 3 --step-limit 15 --out-dir runs/scaling/plancraft
```

Add `--resume <out-dir>` to reuse finished tasks and run only the remainder.

## TODO — open items

- [ ] **Repeats for significance.** All numbers are single runs; run-to-run variation
  on PlanCraft solo was ~2 tasks. Repeat k≥3 for the headline deltas.
- [ ] **`solo-best-of-N` control arm** — the sampling- and compute-matched baseline
  for the team arm; needed to attribute PlanCraft's team gain to structure vs restart
  sampling.
- [ ] **Terminal-Bench-core broader coverage** — only 14 of 77 single-container
  tasks. A 44-task CPU-feasible run was launched on AWS (2026-11-10) but aborted: the
  orchestration catalog had dropped 2.5-flash (see model note), so every call 400'd
  and the box was torn down before producing numbers. Pending a 2.5-flash
  direct-deployment client (or a model-family decision). The heavy (qemu/kernel/ML),
  nested-layout, and network-at-build tasks remain out of scope on a CPU box.
- [ ] **SWE-bench team-patch hygiene** — the team's pytest-6202 (`missing_module`) and
  sphinx-8621 (`no_tests_collected`) on the x86 run are a team-patch-quality follow-up,
  separate from the fixed scratch-leak.
- [ ] **SWE-bench pylint-4551 `no_tests_collected`** — grades this way with a clean
  patch in both arms; a pylint-specific test-collection failure.
- [ ] **Finance-Agent full set** — only the open-50 split is public; the full set and
  the official Vals grader are gated.
- [ ] **Fix the `solo-iso` duration-measurement artifact** (wrong per-task wall-clock).
- [ ] **Per-task `task_events` counts** — the `messages` metric counts send/broadcast
  only, not `task_create`/`task_claim`, so task-board coordination is under-reported.
