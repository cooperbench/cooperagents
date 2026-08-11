# Team Harness Optimization

Draft v1. All measurement runs are complete and scored.

## Headline results

- Semi-automatic optimization of the team harness (a loop of proposing
  mechanisms, k-repeated measurement, and building on mechanisms with measured
  value) produced team configurations that are more Pareto-optimal than Solo
  for Qwen3.5-9B on the development set: Coordinator reaches 15.7/28 features
  at p99 1262s against Solo's 11.3/28 at p99 1398s (higher accuracy at lower
  tail latency), and Board Best-of-2 reaches 17.0/28. Under direct application,
  the same team harness scored 3.0/28; the complete Team configuration
  (lead/member roles + task board + shared scratchpad, lead-side merge)
  scores 13.3/28 — above Solo, below the optimized configurations.
- The mechanism sets with measured value differ between model scales. For
  GPT-5.5, the ablation report attributes the Team benefit to the shared code
  substrate (scratchpad and task list), and adding a shared git remote raised
  Coop from 26% to 56%. For Qwen3.5-9B, the substrate-type mechanisms (task
  board, git sharing, messaging, activity notes) left scores unchanged — the
  Coop+git cell scored 4.3/28 against Coop's 3.0/28 — and the measured value
  came from merge repair, Best-of-N with unit-test selection, and the
  coordinator (on the efficiency axis). The complete Team package, which adds
  a lead-side integration step on top of the substrate, reaches 13.3/28.

## 1. Background

CooperBench evaluates cooperation between coding agents. The base settings:

- **Solo**: one agent receives two feature tasks from the same repository.
- **Coop**: two agents each receive one of the two feature tasks; their patches
  are evaluated together.

The headline CooperBench result is that Coop performance is 30%–50% lower than
Solo. An updated setting, **Coop+git**, adds a shared git remote; in the
02/2026 experiments it was not statistically better than Coop.

In the 06–07/2026 experiments a **team harness** was added, inspired by Claude
Code's Agent Teams harness but generic over agent models and harnesses. The
[Team Harness Ablation report](https://cooperbench-reports.pages.dev/cooperbench/team_harness_ablation_report)
(codex + GPT-5.5, flash-50) measured: Solo 48%, Coop 26%, Coop+git 56%, Team
62%. The ablation attributes most of the Team value to the shared code
substrate (scratchpad and task list). Subsequent attempts to optimize this
harness for frontier models did not find meaningful improvements.

Question of this report: whether the same team harness benefit appears for a
small model (Qwen3.5-9B), and if it does not, whether automatic optimization of
the harness can recover or exceed it.

## 2. Evaluation setup

- Agent harness: CooperAgents (unified harness); agent loop mini-swe-v2, held
  constant across all configurations.
- Model: Qwen3.5-9B served on Modal (vLLM, 32k context, temperature 0).
- Benchmark sets:
  - **fixed-10**: 10 CooperBench-flash pairs.
  - **qwen-14**: development set. 13 pairs where Solo implemented at least one
    feature during calibration, plus 1 pair with zero features from the same
    repositories. All Python-evaluated repositories.
  - **dev-set-2**: 14 unused CooperBench-lite pairs (selection seed 11).
    Development set for mechanisms proposed after qwen-14 stopped resolving
    differences between configurations.
  - **held-out set**: 14 unused CooperBench-lite pairs (selection seed 7).
    Certification only; no configuration decision was made from these numbers.
- Metric: features passed out of 28 (feature-level); pair pass rate out of 14
  reported secondary. k repeated full runs per configuration; means reported.
- Measurement rules acquired during the program:
  - Single-run noise is ±4 features between identical configurations, at
    temperature 0.
  - k≥3 for screening; k≥5 for conclusions. Five results that reached
    screening threshold at k=3 were not reproduced at k=5; one was (Board Best-of-2).
  - Attribution check per mechanism: whether the mechanism fired, and whether
    firing related to the outcome.

## 3. Result of direct team-harness application

| Observation | Result |
|---|---|
| fixed-10, initial runs | Solo 1/10, team 1/10 pairs |
| Identical team configurations, unpinned sampling | 6/26 vs 11/28 features |
| qwen-14 after temperature pinning | Solo 11.3/28 |
| Team with coordination tools, direct application (Coop) | 3.0/28 |

The Coop configuration corresponds to the ablation report's Coop setting
(message tool, no shared code): two agents in separate containers, a
`send_message` tool, patches combined by a mechanical merge after both agents
finish. Diagnosis of the 3.0/28: the merge left conflict artifacts (reject
files and conflict markers) in 7/14 pairs and produced empty output in 4/14;
the message tool was called 0 times across all runs. The correspondence with
the ablation report holds at both scales: messaging-only Coop scores below
Solo for GPT-5.5 (26% vs 48%) and for Qwen3.5-9B (3.0 vs 11.3 features).

Two infrastructure corrections were required before any measurement was
meaningful: the container shell invocation reset PATH and removed the Go
toolchain from the agent's environment, and the litellm provider prefix
mishandled HuggingFace-style model names. Both affected all earlier runs.

The frontier-model result (Team > Solo) did not appear under direct
application. The remainder of the report describes the optimization loop.

## 4. Optimization loop

Each iteration follows the same procedure:

1. Propose a mechanism, with a recorded idea source.
2. Implement it behind a configuration flag; unit tests for the mechanism.
3. Run k repetitions on a fixed set.
4. Attribution check: whether the mechanism fired, and whether firing related
   to the outcome.
5. Keep, drop, or park the mechanism; record the result in
   `docs/SEAM_BACKLOG.md`.

Idea sources: diagnosis of a prior iteration's failures, the CooperBench
team-harness toolkit, user proposals, and analysis of negative results.

Infrastructure: an EC2 worker fleet (up to 15 concurrent measurement runs),
dispatcher scripts with result synchronization, and handling for evaluation
containers that do not terminate (agent-written code that loops under the test
suite; four occurrences, up to 35 hours).

## 5. Iterations

### Iteration 1 — Baselines and infrastructure corrections

Idea source: initial runs. The fixed-10 baselines scored Solo 1/10 and team
1/10 pairs. Trajectory inspection identified the PATH reset (login shell
sourcing /etc/profile) that removed the Go toolchain from agent containers,
and the provider-prefix handling error for HuggingFace-style model names. Both
corrected. Result: Solo 2/10 after correction; the set remained at floor.

### Iteration 2 — Calibrated set and feature-level metric

Idea source: floor diagnosis of Iteration 1. A Solo-only sweep of the 40
remaining flash pairs measured a pass rate of 3/35 scored pairs. The qwen-14
development set was assembled from pairs with calibration signal, and the
metric changed from pair pass rate (14 outcomes) to features passed (28
outcomes). Result: measurable baselines.

### Iteration 3 — Temperature pinning and repetition protocol

Idea source: two identical team configurations scored 6/26 and 11/28.
Temperature was pinned to 0. Two identical temperature-0 team runs then scored
17/28 and 13/28, establishing the ±4 single-run noise floor. The measurement
protocol became k≥3 repeated runs with means. Baselines under this protocol:
Solo 11.3 (k=3), sequential team 15.3 (k=3).

### Iteration 4 — Tree-State Gate (code Q1). Parked

Idea source: diagnosis of team runs where a later agent left files
syntactically invalid. After each agent, the harness checked the tree (AST
parse / go build) and discarded the agent's delta if a previously healthy tree
became unhealthy. Attribution check: the gate fired in 1/14 pairs; the
measured difference was within noise. Parked.

### Iteration 5 — Sequential Best-of-2 with unit-test selection (code Q2). Kept

Idea source: the GPT-5.5-era program had dropped an LLM-as-a-judge selector
(T6) because its choices did not correlate with hidden-test outcomes; the ±4
noise floor from Iteration 3 indicated large between-run variance available
for selection. Sequential Best-of-2 runs the full team twice and submits the candidate with the
better result on the repository's original unit tests (after a build/AST
check). Result: 17.7 (k=3: 18, 17, 18) vs baseline 15.3, with reduced run
variance. Kept. Cost: 2× compute.

### Iteration 6 — Diversified Best-of-2 (code Q3). Dropped

Idea source: analysis of Sequential Best-of-2 selection decisions showed 38/42 were ties between
near-identical candidates. Diversified Best-of-2 sampled the second attempt at temperature 0.7 to
produce distinct candidates. Result: 15.5 (k=2: 17, 14), below Sequential Best-of-2. Dropped.

### Iteration 7 — Coop with coordination tools (code Q4)

Idea source: the CooperBench team-harness toolkit; sequential execution
(build-on-prior) was excluded from the search space at this point by user
direction, because its wall-clock cost grows linearly with team size. Coop ran
agents concurrently with a `send_message` tool and merged patches after
completion. Result: 3.0 (k=2: 2, 4). Attribution: agent trajectories were
normal; the merge destroyed the work (conflict artifacts in 7/14 pairs, empty
merges in 4/14); 0 message-tool calls. This iteration produced the two
diagnoses that drove Iterations 8 and 11.

### Iteration 8 — Coop+Repair (code Q5). Kept

Idea source: the Coop merge diagnosis; the GPT-5.5-era guarded-merge experiment
(Round 10). After the mechanical merge, the harness checks tree state; if the
merge broke the tree, one repair agent runs in the merged container with a
reconcile-both-features brief. Result: 15.7 (k=3: 15, 16, 16) at 556s mean per
pair; the repair agent ran in 12/14 pairs. Kept. This configuration exceeded
the sequential team (15.3) and became the standard base for later iterations.

### Iteration 9 — Merge and gate variants (codes Q5f, Q5g, Q10). Closed

Idea sources: efficiency (Coop+Repair (step-capped): 25-step repair cap; Coop+Repair (3-way merge): 3-way merge), then
diagnosis of Coop+Repair (3-way merge) (Test-Gated Repair: behavioral gate), then a user request to reduce latency
(repair-time sweep). Results: Coop+Repair (step-capped) 13.0; Coop+Repair (3-way merge) 13.4 (k=5) — 3-way merges that
apply cleanly can still fail the unit tests, and the syntax-only gate does not
route them to repair; Test-Gated Repair (gate = unit tests + critic agent-written checks)
15.0 (k=3: 12, 16, 17) — detection improves, scores do not; repair-time caps
of 240/480/900/1800s scored 14.7/15.0/15.0/13.3 — within noise of each other.
Conclusion of the group: repair corrects mechanical damage (reject files,
conflict markers); test-detected damage remains uncorrected regardless of the
repair budget. Closed.

### Iteration 10 — Selection stacks (codes Q7, Q7g, Q8, Q11)

Idea source: composition of Iteration 5 on Iteration 8. Best-of-2 was run on
four bases. Results: Best-of-2 (step-capped base) 15.0, Best-of-2 (3-way base) 15.7, Critic-Test Best-of-2 15.3, Test-Gated Best-of-2 15.0. Selection added
approximately +2 on each stable base. Critic-Test Best-of-2 replaced the original-unit-test
selector with pooled critic agent-written checks (each agent writes a check
for its feature; all candidates are scored on the union); the change did not
improve selection. None of the stacks exceeded Coop+Repair with selection at equal
compute.

### Iteration 11 — Toolkit with system-prompt billing (codes TK1–TK5)

Idea source: user prioritization of the team-harness toolkit; the Coop
attribution result (0 tool calls). Prompted Messaging identified the cause of the zero usage:
the mini-swe-v2 system template requires a bash call in every response and the
message tool was only mentioned in the task text. With the tool described in
the system prompt and a first-action instruction, usage rose from 0 to
approximately 20 calls per run. Configurations: Interface Contract forced interface contract
(one planner call, injected into briefs) 15.0; Activity Notes pushed file-activity notes
15.0; Prompted Messaging billed messaging 15.0; Task Board shared task board 15.2 (k=5; the k=3
mean of 17.0 was not reproduced); Blocking Requests blocking request/response 15.0 (waits
used, ~370s added). Summary: usage responds to prompt placement; scores did
not change with usage.

### Iteration 12 — Allocation tools (codes TK6, TK7)

Idea source: user question about the untested toolkit entries (task claiming,
spawning). Task Claiming gave all agents the full objective and an unclaimed board task
per feature (`task_claim`); N=2 scored 14.3, N=3 scored 14.3 — a third agent
did not raise the score. Spawn Tool added a `spawn_helper` tool with identical
billing to the used tools; it was invoked 0 times across all runs. The four
sibling tools with identical billing were used heavily, so prompt placement
does not explain the zero; spawning requires the agent to assess its own
workload and was not exhibited at this model size.

### Iteration 13 — Board Best-of-2 (code TK9). Confirmed

Idea source: composition of the three components with measured value (task
board, Coop+Repair's apply-chain merge base, Best-of-2 with unit-test selection).
Result: 17.0 (k=5: 17, 19, 16, 17, 16; sd 1.2) at 1168s mean per pair. This
is the only configuration whose k=3 screening result was reproduced at k=5.
The ablation-comparison configuration Board+Repair (identical pipeline without
Best-of-2) scored 12.7; the 4.3 difference exceeds the ~2 selection adds on
other bases. Candidate mechanism: single Board+Repair attempts are bimodal because
board and tool activity competes with implementation for the 50-step budget,
and the unit-test selector discards the lower-scoring attempt. Cause
unidentified.

### Iteration 14 — Board Best-of-2 (repair-capped) (code TK9f)

Idea source: user request to make Board Best-of-2 faster than the sequential
Best-of-2 reference. A 480s wall-clock cap on repair plus selection shortcuts
scored 15.0 at 870s mean — lower latency than the reference on every
percentile, and 2 features below Board Best-of-2. Combined with the Iteration 9 sweep,
the conclusion: the long-duration repairs contribute the accuracy; capping
them converts Board Best-of-2 into a higher-cost equivalent of Coop+Repair.

### Iteration 15 — Focused repair and Best-of-N, N∈{3,4}

Idea source: user selection of directions. Focused repair (harness collects
reject hunks, conflict-marker locations, failing check output into the repair
brief): 15.5 (k=2); no measurable improvement — providing the damage locations
did not change repair outcomes. Best-of-3: 15.3 (k=3: 19, 14, 13). Best-of-4:
17.7 at k=3 (17, 19, 17); at k=5 the mean is 16.4 (17, 19, 17, 13, 16), with
pooled p99 4322s — below Board Best-of-2 (17.0 at k=5, p99 3793s) on both
axes. The k=3 screening value was not reproduced at k=5, matching the
program's screening-regression pattern; Board Best-of-2 remains the
highest-accuracy configuration.

### Iteration 16 — Held-out certification

Idea source: approximately 50 configurations had been evaluated against
qwen-14; the risk that configuration differences were specific to that set
required a certification set never used for decisions. Results (features/28):
Solo 8.0 (k=3: 11, 7, 6), Coop+Repair 8.0, Board Best-of-2 8.3, Best-of-3 9.7,
Best-of-4 9.3, sequential Best-of-2 reference 10.0 (k=3: 11, 11, 8). On this set the
configuration differences are within noise of each other. The development-set
differences did not transfer to randomly selected pairs; the value measured on
qwen-14 is concentrated on pairs where the model has partial capability.
A second observation: on dev-set-2 (also randomly selected), Solo scored 6.0
and Coop+Repair scored 8.7–9.4, so the team-over-Solo difference on random pairs
varies by set from 0 to approximately +3.

### Iteration 17 — Coordinator (code C2)

Idea source: user proposal. A monitor thread reads agent trajectories during
execution; mechanical triggers (repeated near-identical commands; identical
error observations; overlapping dirty-file sets) decide when to intervene; a
model call composes a short corrective instruction; delivery uses the pushed
observation channel; at most 3 interventions per agent. Attribution results:
interventions fired in every pair (247 total in the k=5 dev-set-2 column);
69% of intervened command-repetition segments ended within 6 commands, against
28% for unintervened segments in control runs. Performance: unchanged at k=5
(9.8 vs 9.4 control on dev-set-2; the k=3 columns had non-overlapping ranges
and did not reproduce). Efficiency: p99 latency 2178s vs 2958s control on
dev-set-2; on qwen-14, 15.7 (k=3: 15, 16, 16 — equal to Coop+Repair) with p99 1262s vs
Coop+Repair's 2644s. The intervention compliance is verified and the score is
unchanged; the agents complete redirected work at the same rate they complete
original work.

### Iteration 18 — p99 efficiency axis; Board Best-of-2 + Coordinator

Idea source: user proposals (efficiency axis change; combination). With p99
per-task latency as the efficiency axis, long-duration repairs and selection
runs are priced into each configuration. Board Best-of-2 + Coordinator (Board Best-of-2 with the coordinator in
each attempt): 16.3 (k=3: 16, 16, 17) at p99 2852s — between Coordinator and Board Best-of-2 on
both axes, and on the front. Final front on qwen-14 (candidates only):

| Front point | Features/28 | p99 (s) |
|---|---|---|
| Coop | 3.0 | 704 |
| Coordinator | 15.7 | 1262 |
| Board Best-of-2 + Coordinator | 16.3 | 2852 |
| Board Best-of-2 | 17.0 | 3793 |

### Iteration 19 — Coop+git cells

Idea source: comparison with the ablation report's configuration matrix
identified that no configuration in this program provided a live shared git
remote (the Coop+git cell). Implemented: a bare repository on a shared docker
volume; a harness thread pushes each agent's working tree to a per-agent
branch every 45s via `git stash create` (the agent's tree and history are
unchanged); the poller fetches teammate branches into each agent's repository
and reports changed files with view/take commands; system-prompt billing per
the Iteration 11 result. Two cells at k=3.

Results: Coop+git (q4git) 4.3/28 (k=3: 4, 7, 2) against Coop's 3.0/28 — within
single-run noise. At GPT-5.5 the ablation report measured Coop 26% and Coop+git
56%; at Qwen3.5-9B the git substrate does not produce a comparable recovery.
Coop+Repair+git (tkgit): 14.7 (k=3: 14, 15, 15) against Coop+Repair's
15.7 — no measured addition from the git substrate on top of repair.

### Iteration 20 — Complete-Team cell

Idea source: user question (was the complete Team configuration tested?) and
user correction (the CooperBench scratchpad is a shared Docker volume mounted
at `/workspace/shared` in every agent container, so it is compatible with the
one-container-per-agent constraint). During Iterations 1–19 the Team
components were measured separately (task board, claiming, messaging, git
substrate) and in compositions with repair and selection, but the ablation
report's Team configuration as one package — lead/member roles + task board +
shared scratchpad, with the lead merging member patches and no harness-side
repair or selection — was not run as a single cell. Implemented now: a
`team_roles` cell mounting a per-run scratchpad volume in both containers;
the member exports its diff to `/workspace/shared/<agent>.patch`; the lead
plans in `/workspace/shared/PLAN.md`, assigns board tasks, applies the member
patch, and the lead's tree is the team submission.

Results: 13.3/28 (k=3: 13, 13, 14); mean 498s per pair; p99 1851s (per-run
p99: 990, 1656, 1851). Comparison points on the same set: Coop 3.0, Solo
11.3, Coop+Repair 15.7, Board Best-of-2 17.0; sequential team 15.3
(text-only reference). The complete Team configuration scores above Coop and
Solo without harness-side repair or selection; the merge is performed by the
lead agent applying the member's scratchpad patch. On the p99 front it is
dominated by Coordinator (15.7 at p99 1262s). Relation to Iteration 19: the
git-substrate cells added no measurable score over their bases, while this
package — the shared-file substrate combined with an explicit lead-side
integration step and role asymmetry — recovers most of the distance between
Coop and the optimized configurations. Attribution among the scratchpad, the
role asymmetry, and the lead merge step within the package has not been
measured.

## 6. Pareto front construction

The front was tracked in accuracy (features/28, k-run mean) against
efficiency. The efficiency axis was mean seconds per pair for Iterations 1–17
and p99 seconds per task from Iteration 18 (user direction). Reference and
sequential configurations are excluded from the figures.

Front at four stages:

1. After Iteration 3: Solo only (11.3 at 329s mean).
2. After Iteration 8: Coop → Solo → Coop+Repair (step-capped) → Coop+Repair.
3. After Iteration 13: Coop → Solo → Coop+Repair (step-capped) → Activity Notes → Coop+Repair → Board Best-of-2 (mean axis).
4. After Iteration 18 (p99 axis): Coop → Coordinator → Board Best-of-2 + Coordinator → Board Best-of-2.

Figures: `docs/pareto_qwen14.png` (final front, p99 axis, per-run points
included), `docs/durations_qwen14.png` (per-task completion time ECDF).

## 7. Findings

- Coop scores below Solo at both model scales (GPT-5.5: 26% vs 48%; Qwen3.5-9B:
  3.0 vs 11.3 features). Optimization recovered the team configurations to
  17.0/28 on the development set. Sequential team comparison (text only):
  sequential 15.3/28 at 512s mean, p99 1260s; the sequential Best-of-2
  reference 17.7/28 at 977s mean; Board Best-of-2 reaches 17.0 without sequential
  execution, and Coordinator reaches the sequential p99 in a concurrent configuration.
- Best-of-N with N=2 adds approximately +2 features on stable bases (5
  replications). Two selector variants measured: the repository's original
  unit tests, and pooled critic agent-written unit tests; the second did not
  improve on the first. N=3 (15.3) is within noise of the base at k=3;
  N=4 scored 17.7 at k=3 and 16.4 at k=5, below Board Best-of-2 (17.0 at k=5).
- Repair corrects mechanical merge damage (reject files, conflict markers).
  Damage detected by running unit tests and critic agent-written checks
  remains uncorrected at any repair budget measured (240s–uncapped).
- Tool usage requires system-prompt description of the tool; usage rose from
  0 to ~20 calls per run with placement changes. Usage and score are
  decoupled: five tool configurations with heavy verified usage all scored
  15.0. The spawn tool was used 0 times under billing identical to used tools.
- Live coordinator: trigger detection and instruction compliance verified
  (69% vs 28% segment termination); performance unchanged; p99 latency
  reduced by 26–52% at equal score on two sets.
- Held-out certification: configuration differences measured on the
  development set are within noise on randomly selected pairs. The
  team-over-Solo difference on random sets varied from 0 to +3 by set.
- Board Best-of-2 − Board+Repair = +4.3, exceeding the ~2 that Best-of-2 adds on other bases. The
  configurations differ only in Best-of-2. Candidate mechanism: Board+Repair single
  attempts are bimodal under budget competition between board activity and
  implementation, and selection removes the lower mode. Cause unidentified.
- Five k=3 screening results (Task Board 17.0; Best-of-4 17.7 → 16.4 at k=5;
  an earlier Best-of-4 column; Board Best-of-2 (repair-capped) run-a; Coordinator
  non-overlapping k=3 columns) were not reproduced at k≥5. One k=3 result was
  reproduced (Board Best-of-2, 17.0 at k=5).

## 8. Costs and reproducibility

- Compute: approximately $100–130 of on-demand EC2 (m6i.4xlarge fleet, up to
  15 concurrent), plus Modal GPU serving (single H100-class container,
  autoscaled to 3 at peak). Spot instances were reclaimed before run
  completion in 3 of 3 attempts and were not used further.
- Scripts: `scripts/measure_qwen.sh` (qwen-14), `scripts/measure_dev2.sh`
  (dev-set-2, seed 11), `scripts/measure_heldout.sh` (held-out, seed 7),
  `scripts/bench_compare.py` (all configuration flags).
- Records: `docs/SEAM_BACKLOG.md` (per-iteration results and decisions); run
  logs under `logs/` per configuration and repetition.
- Model serving: Modal app `qwen35-9b-32k`, `.env.qwen` profile,
  `COOPER_TEMPERATURE=0`.

### Configuration code table

Report names map to the codes used in scripts, logs, and `docs/SEAM_BACKLOG.md`:
Coop=Q4, Coop+Repair=Q5, Coop+Repair (step-capped)=Q5f, Coop+Repair (3-way
merge)=Q5g, Test-Gated Repair=Q10, Test-Gated Best-of-2=Q11, Sequential
Best-of-2=Q2, Diversified Best-of-2=Q3, Tree-State Gate=Q1, Critic-Test
Best-of-2=Q8, Interface Contract=TK1, Activity Notes=TK2, Prompted
Messaging=TK3, Task Board=TK4, Blocking Requests=TK5, Task Claiming=TK6,
Spawn Tool=TK7, Board+Repair=TK8, Board Best-of-2=TK9, Board Best-of-2
(repair-capped)=TK9f, Coordinator=C2, Coop+git=q4git, Coop+Repair+git=tkgit, Complete Team=teamfull.

## Open items

- Board Best-of-2 − Board+Repair mechanism verification from selection logs: not run.
- Attribution within the complete Team package (scratchpad vs role asymmetry
  vs lead merge step): not measured.
