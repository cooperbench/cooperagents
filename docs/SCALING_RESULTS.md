# Scaling-benchmark results (cooperagents)

Measured success rates for the cooperagents in-place stack on the
Scaling-Agent-Systems benchmarks. Each entry is one completed model-sweep run.
Last updated 2026-10-15.

## Coordination occurrence (cooperagents team, 2026-10-15)

cooperagents' team is its own "5th MA setup": isolated per-agent environments
(own clone of the one shared problem) coordinating in real time over the shared
bus (`send_message`/`broadcast`), reconciled by a reducer. The state path now
injects a teammate-coordination brief so agents are told they are a team, and
each task records a `messages` count (total bus traffic) so coordination is
measured, not assumed.

Finding — **coordination does not occur on PlanCraft.** An 8-instance,
team_size=3 run with coordination wired and prompted recorded **0 bus messages
on every task**, including the hard instances that ran to the step limit and
failed (VAL0274/0381/0214 at ~44-45 steps). The agents never broadcast or
messaged even while failing. The plumbing is verified correct: offline,
`messages>0` the instant an agent broadcasts, and a live **BrowseComp** team
(where the deliverable is a written answer) *did* coordinate (`note_write` +
a jointly-converged correct answer). So this is a task-fit result, not a bug:
on a solo-shaped crafting task the agents ignore coordination regardless of
prompting, so the PlanCraft "team" is functionally **parallel independent
attempts** (pass@N-like) with no coordination to measure. The benchmarks where
cooperagents' coordination actually engages are the text/research ones
(BrowseComp, Finance).

## Compute balancing vs the paper

The paper claims a compute-matched design in its text (consistently since v1):
"we matched maximum total iterations between MAS and SAS … MAS received equal
computational budget through parallel agent processing (smaller per-agent
iterations for n-agent teams), while SAS received proportionally more reasoning
rounds," with "all systems matched for total reasoning tokens (mean ~4,800 per
trial)," plus overhead `O% = (T_MAS-T_SAS)/T_SAS` and efficiency
`E_c = S/(T/T_SAS)` metrics.

The paper's released code (through the latest tag v2.2.3) does **not** implement
this: single-agent is hard-capped at 100 steps while each MAS gets
`n_base_agents(3) x max_rounds(10) x max_iterations_per_agent(25) ≈ 750` LLM
calls — caps are not equalized — and `success_per_1k_tokens` is computed but left
out of the regression. So the matching is a stated methodology, not a shipped
one; this gap is present in all three paper versions, not a drafting artifact.

cooperagents implements the equal-total-compute comparison the paper describes,
via the **`solo-iso` vs `team`** arms: both receive `team_size x step_limit`
total steps (we inflate the solo to the team's total; the paper instead shrinks
each team agent to a shared total — the same equal-total target reached from the
opposite side). `solo` vs `team` remains the raw, unmatched comparison (what the
paper's code actually runs).



Model: `sap/gemini-2.5-flash` via SAP AI Core. Per-agent step limit: 15.
Team reducer: `select_by` (success first, then fewest steps) over independent
per-agent simulators.

Arms:

- `solo` — 1 agent, 15 steps (single-agent baseline).
- `solo-iso` — 1 agent, team_size x 15 steps (step-budget matched to the team).
- `team` — team_size agents, 15 steps each.

### team_size = 2 (2026-10-14, run dir `runs/scaling/plancraft_full`)

| Arm | Success rate | Passed | Avg steps | Cost |
| --- | --- | --- | --- | --- |
| solo | 0.173 | 19/110 | 9.1 | $0.49 |
| solo-iso | 0.200 | 22/110 | 11.4 | $0.66 |
| team | 0.209 | 23/110 | 18.5 | $0.94 |

Relative delta team vs solo: +21.1%. Team vs solo-iso: +4.5%.
Instance agreement: 18 solved by both, 1 solo-only, 5 team-only.

### team_size = 3 (2026-10-15, run dir `runs/scaling/plancraft_team3`)

| Arm | Success rate | Passed | Avg steps | Cost |
| --- | --- | --- | --- | --- |
| solo | 0.191 | 21/110 | 9.4 | $0.49 |
| solo-iso | 0.191 | 21/110 | 12.5 | $0.74 |
| team | 0.236 | 26/110 | 27.1 | $1.47 |

Relative delta team vs solo: +23.8%. Team vs solo-iso: +23.8%.
Instance agreement: 21 solved by both, 0 solo-only, 5 team-only.

## Compute matching

`solo-iso` matches the team in step budget (team_size x 15) but not in realized
compute. Realized compute per task:

| Run | team / solo-iso steps | team / solo-iso cost |
| --- | --- | --- |
| team_size = 2 | 1.62x | 1.44x |
| team_size = 3 | 2.16x | 1.99x |

Cause: a PlanCraft episode terminates on success, an impossible declaration, or
a stuck state, so a single agent granted the full team budget stops well below
it (12.5 of 45 steps at team_size = 3). The team runs team_size independent
episodes to their own terminations, so its realized step count scales with
team_size while solo-iso stays near a single episode length. A realized-compute-
matched single-agent baseline is therefore not reachable by step-budget inflation
on PlanCraft.

Consequence for interpretation: the team agents share a bus and keep the
coordination tools (`send_message`, task board), the same machinery as the git
substrate. PlanCraft provides no mergeable shared artifact — each agent acts in
its own simulator and the deliverable is a per-episode outcome — so the reducer
is selection (`select_by`) rather than synthesis, and the arm reduces to a
best-of-N independent-sampling configuration in outcome (N = team_size, selection
by outcome then step count). The gain reflects running N independent episodes and
keeping the best, together with the realized compute that consumes. The +23.8%
team-vs-solo-iso delta at team_size = 3 is a best-of-3 sampling result at
approximately 2x the realized compute of the solo-iso arm. The result does not
isolate any benefit of multi-agent coordination beyond repeated independent
attempts, because PlanCraft gives coordination no shared substrate to act on. On
the text benchmarks (BrowseComp, Finance) the same harness instead uses
`lead_synthesis`, where a lead composes teammates' contributions into the graded
answer.

The matched control for this effect is a solo-best-of-N arm: N independent
single-agent episodes at 15 steps each with the same outcome selection. That arm
is sampling-matched and realized-compute-matched to the team, and it is not yet
run. Until it is, the +5 solves are attributable to best-of-3 restart sampling,
and a solo-best-of-3 arm is expected to reproduce them.

### Selector is oracle (pass@N upper bound)

The team reducer `select_by((success, -steps))` reads each episode's `success`
flag. That flag is the gym's ground-truth reward (`reward > 0`), which is the
same signal `PlanCraftScorer` grades. The reducer therefore submits a passing
episode whenever one exists, and the team is scored as passing if at least one
of its N independent episodes passed. The team arm is thus pass@N with an oracle
selector, an upper bound rather than an operating point a deployed system could
reach without the grader.

Partial self-verifiability: a crafting success is observable to the agent (the
target item appears in its own inventory), so a realistic selector could recover
it from self-available state; a correct `impossible` declaration depends on
ground truth the agent cannot observe, so selecting on it requires the oracle.

Implications:

- `solo` and `solo-iso` are single-episode pass@1 (no selection). `team` is
  oracle pass@N. The team-vs-solo-iso delta is therefore partly the expected
  pass@N-over-pass@1 effect plus oracle selection, not evidence of coordination.
- A matched `solo-best-of-N` arm using the same selector would land at
  essentially the team's number, since both are pass@N over N independent
  episodes. That arm is the control needed to attribute any residual difference
  to team structure, and it is not yet run.
- A realistic (non-oracle) team number requires a selector that uses only
  self-available signal (for PlanCraft: prefer an episode that produced the
  target item; `impossible` correctness cannot be self-verified).

### Compute efficiency (team_size = 3)

Solves per unit compute, which inverts the raw-rate ordering:

| Arm | Solves | Solves per dollar | Solves per 1000 steps |
| --- | --- | --- | --- |
| solo | 21 | 42.6 | 20.3 |
| solo-iso | 21 | 28.4 | 15.3 |
| team | 26 | 17.7 | 8.7 |

The team arm has the highest absolute success rate and the lowest success rate
per unit compute.

## Measurement integrity

Both runs passed the per-task fault detectors:

- Instance-fingerprint consistency across arms: 0 divergences (detects the
  reused/mutated-instance fault that produced spurious step-count-zero episodes
  in an earlier run).
- Team episodes with zero steps: 0 (detects a spent instance selected by the
  reducer).

Per-task records with success, steps, cost, duration, scorer detail, per-agent
breakdown, and trajectory pointers are in each run dir under `tasks.jsonl`.

## Caveats

- Statistical significance: the arms are evaluated on the same 110 instances, so
  the paired McNemar test applies. For team vs solo-iso at team_size = 3 the
  discordant pairs are 0 solo-iso-only and 5 team-only, giving an exact two-sided
  p = 0.062. The difference is borderline and does not reach the 0.05 level.
- Nested outcomes: at team_size = 3 the solo-iso solves are a strict subset of the
  team solves (0 instances solved by solo-iso but missed by the team). This is
  consistent with the best-of-N mechanism: more independent attempts weakly add
  solves and remove none.
- Missing matched control: a solo-best-of-N arm (N independent 15-step single-agent
  episodes with outcome selection) is the sampling- and compute-matched baseline
  for the team arm and is not yet run. See the Compute matching section.
- Run-to-run variation: the `solo` arm scored 0.173 (team_size = 2 run) and 0.191
  (team_size = 3 run) under an identical configuration, a 2-task difference from
  model sampling. The +5-task team gain is about 2.5x this observed noise level,
  from a single run with no repeats.
- Step-budget saturation: 94 of 330 team agent-episodes at team_size = 3 ended by
  hitting the step limit or erroring rather than submitting, so the per-agent step
  limit is an active constraint on the team arm.
- The grader is PlanCraft's own gym reward (offline, deterministic). The agent is
  the cooperagents builtin loop, which differs from the scaffold in the paper, so
  absolute rates are system-to-system; the relative solo-to-team delta is the
  comparable quantity.

## Reproduction

Full run (team_size = 3):

```
uv run python scripts/scaling_box.py --benchmark plancraft --models sap/gemini-2.5-flash --split val.small --team-size 3 --step-limit 15 --out-dir runs/scaling/plancraft_team3
```

Resume an interrupted run (reuses finished tasks, runs only the remainder):

```
uv run python scripts/scaling_box.py --benchmark plancraft --models sap/gemini-2.5-flash --split val.small --team-size 3 --step-limit 15 --resume runs/scaling/plancraft_team3
```
