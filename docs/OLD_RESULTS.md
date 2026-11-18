# Superseded / historical scaling-benchmark results (cooperagents)

Earlier runs and rerun history, moved out of `SCALING_RESULTS.md` on 2026-11-17 to
keep that doc to the most-recent result per benchmark. Nothing here is current — it
is kept for provenance (what changed, and why the headline numbers moved). All runs
used `sap/gemini-2.5-flash` via SAP AI Core. For the current numbers see
[SCALING_RESULTS.md](SCALING_RESULTS.md).

---

## Pre-loop-fix sweep (2026-10-21) — superseded by the post-fix numbers

Full-set runs on three benchmarks, `team_size = 3`. PlanCraft (110) and BrowseComp
(100) ran their full splits. WorkBench ran all six domains (690 instances total).

| Benchmark | n | solo | solo-iso | team (3) | team vs solo | team vs solo-iso |
| --- | --- | --- | --- | --- | --- | --- |
| PlanCraft (val.small) | 110 | 0.155 | 0.191 | 0.164 | +5.9% | -14.3% |
| WorkBench (pooled, 6 domains) | 690 | 0.488 | 0.483 | 0.542 | +11.0% | +12.3% |
| BrowseComp (test) | 100 | 0.010 | 0.010 | 0.020 | +100.0% | +100.0% |

Pre-fix WorkBench per-domain:

| Domain | n | solo | solo-iso | team (3) | team vs solo |
| --- | --- | --- | --- | --- | --- |
| email | 90 | 0.400 | 0.344 | 0.722 | +80.6% |
| calendar | 110 | 0.682 | 0.673 | 0.755 | +10.7% |
| analytics | 120 | 0.508 | 0.508 | 0.592 | +16.4% |
| project_management | 80 | 0.463 | 0.487 | 0.425 | -8.1% |
| customer_relationship_manager | 80 | 0.512 | 0.512 | 0.463 | -9.8% |
| multi_domain | 210 | 0.414 | 0.414 | 0.400 | -3.4% |

Run dirs: `runs/scaling/sweep_plancraft`, `runs/scaling/sweep_browsecomp`, and the
six `runs/scaling/sweep_workbench*` directories.

## Loop-fix re-run (2026-10-26/27/30) — this produced the current numbers

Fixes landed in the shared state loop and the answer-graded path:
- **Format-retry**: a model that lapses into prose mid-task used to be coerced to a
  `finish` and terminate before answering; it is now re-prompted for the required
  JSON (bounded).
- **Connectivity-aware pause**: the retry helper waits out local network loss
  (laptop sleep / no wifi) instead of burning tasks as `steps=0` failures;
  `JSONDecodeError` treated as transient; both answer-graded judges (`LLMJudge`,
  `LLMRubricGrader`) routed through the retry helper.
- **`answer_only` artifact (team-reducer fix)**: answer-graded benchmarks
  (BrowseComp, Finance) now mark the free-text `answer` as the sole deliverable, so
  an answer-less agent is correctly empty (the finish-veto forces a `submit_answer`)
  and `text()` never falls back to the `state` field (visited-docs / sources).
- `_agent_breakdown` now records each agent's `error` (was dropped).

PlanCraft re-run `*_v2` (loop fix); BrowseComp/Finance re-run `*_v3` (loop fix +
answer_only); WorkBench 690 re-run `wb_*_v2` (loop fix), complete 2026-10-30. All
clean, zero-step tasks = 0 (the offline-pause rode out laptop sleeps without
corrupting any task).

Pre-fix → post-fix (post-fix = the current numbers in SCALING_RESULTS.md):

| Benchmark | arm | pre-fix | post-fix | note |
| --- | --- | --- | --- | --- |
| PlanCraft (val.small, 110) | solo | 0.155 | 0.155 | |
| | solo-iso | 0.191 | 0.200 | |
| | team | 0.164 | **0.191** | loop fix lifts team (+23.2% vs solo) |
| BrowseComp (test, 100) | solo | 0.010 | **0.110** | loop fix + answer veto: agents now answer |
| | solo-iso | 0.010 | **0.140** | |
| | team | 0.020 | **0.090** | reducer fix: 0.000 (bug) → 0.090 real passes |
| Finance (open-50) | solo | — | **0.100** | keyless ddg; after tool-crash fix (v4) |
| | solo-iso | — | **0.080** | |
| | team | — | **0.080** | 403/coordination crashes fixed; was 0.000 (tool bug) |
| WorkBench (pooled, 690) | solo | 0.488 | 0.506 | |
| | solo-iso | 0.483 | 0.507 | |
| | team | 0.542 | **0.596** | team benefit **+11.0% → +17.8%** vs solo |

WorkBench per-domain team (pre → post): email 0.722 → **0.833**; calendar 0.755 →
**0.818**; analytics 0.592 → 0.575 (flat); project_management 0.425 → **0.500**
(−8.1% → **+8.1%** vs solo); customer_relationship_manager 0.463 → **0.512** (−9.8%
→ **+5.1%**); multi_domain 0.400 → **0.457** (−3.4% → **+5.5%**). The format-retry
disproportionately helps the TEAM arm — it flipped three previously-negative domains
positive (pm, CRM, multi_domain) and lifted pooled team benefit from +11.0% to
+17.8%. Agents that used to die on a prose lapse now recover and finish, and a team
of three loses more work to that failure mode than a solo agent, so the fix helps
team more.

### Team-reducer bug on answer-graded benchmarks (fixed 2026-10-27)

The team arm of answer-graded benchmarks submitted the **visited-document list, not
an answer**: `StateArtifact.text()` returned `answer or state`, team members rarely
called `submit_answer` (the finish-veto never fired because `state` was always
non-empty), so the reduced artifact had an empty `answer` and `text()` fell back to
the `state` field — judged NO → BrowseComp team 0.000. Root causes (workflow-mapped):
`_harvest` unconditionally populated `state`; `is_empty()`/`text()` let `state` stand
in for a missing answer; `lead_synthesis` then fed visited-doc lists to the lead as
if they were answers. Fix: `StateArtifact(answer_only=True)` for BrowseComp/Finance
makes `answer` the sole deliverable — emptiness and graded text depend on `answer`
alone, the veto forces a `submit_answer`, and answer-less members filter out of the
reducer. Verified live: BrowseComp team **0.000 → 0.090**. State-graded benchmarks
(WorkBench/PlanCraft, scored on `state`) are unaffected (they do not set
`answer_only`).

## PlanCraft — prior full 110-instance runs at other team sizes

| Run | solo | solo-iso | team | team vs solo | team vs solo-iso |
| --- | --- | --- | --- | --- | --- |
| team_size = 2 (`runs/scaling/plancraft_full`) | 0.173 | 0.200 | 0.209 | +21.1% | +4.5% |
| team_size = 3 (`runs/scaling/plancraft_team3`) | 0.191 | 0.191 | 0.236 | +23.8% | +23.8% |

## WorkBench multi_domain — partial-vs-complete ordering effect

An earlier partial run of multi_domain (171 of 210 team tasks) reported team 0.474;
the completed 210-instance run gives 0.400. The remaining 39 tasks resolved mostly
to failures for the team arm, so the partial figure was biased by task ordering.

## Terminal-Bench-core — local arm64 first run (2026-10-23) — superseded by x86_64

The 14-task light subset, first run locally on Apple Silicon arm64. Superseded for
the canonical architecture by the x86_64 AWS run in SCALING_RESULTS.md.

| Arm | n (scored) | success rate |
| --- | --- | --- |
| solo | 14/14 | 0.357 (5/14) |
| solo-iso | 14/14 | 0.286 (4/14) |
| team (3) | 14/14 | 0.286 (4/14) |

team vs solo -20.0%, team vs solo-iso +0.0%. No team benefit. The passing tasks are
the simple ones (hello-world, fix-permissions, organization-json-generator,
heterogeneous-dates, new-encrypt-command). Four tasks produce an empty patch in the
solo/solo-iso arms (nginx-request-logging, sanitize-git-repo, jupyter-notebook-server,
polyglot-c-py) — genuine failures, confirmed on a healthy-endpoint rerun. The team
arm produced a non-empty patch for all 14 tasks but passed only the same four easy
ones. Run dir: `runs/scaling/tbcore_light/`. (The x86_64 rates came out lower — solo
0.357 → 0.214 — so the two archs are not interchangeable.)

## SWE-bench Verified — local arm64 grade (2026-10-23) — superseded by x86_64

The 6-instance set, graded on arm64 images (not leaderboard-canonical; canonical is
x86_64). Superseded by the x86_64 AWS grade in SCALING_RESULTS.md.

| Arm | resolved | n |
| --- | --- | --- |
| solo | 3 | 6 |
| team (3) | 2 | 6 |

Solo (first run) resolved django-16082, pytest-6202, sympy-22914. The team arm was
re-run after the scratch-leak fix; it resolved django-16082 and pytest-6202. All six
team patches are scratch-free — django-16082 is 2311 bytes / 3 files with no
`modify_expressions.py` (the pre-fix team patch was 7967 bytes / 12 files carrying
that scratch helper). Run-to-run variance: sympy-22914 resolved solo but not in the
team rerun. pylint-4551 grades `no_tests_collected` in both arms with a clean patch
(a pylint-specific test-collection failure, not the hygiene leak). Run dir:
`runs/sweb_small/`; grade reports `cooperagents.sweb_small_{solo,team2}.json`.
