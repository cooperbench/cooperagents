# Report Plan: Team Harness Optimization

Draft outline for iteration. Each section lists intended content and data sources.
Style: per CLAUDE.md style guide (standard terminology, neutral headers, no
metaphors, no contrastive framing, no colloquialisms, no anthropomorphism).

## 1. Background

- CooperBench setting
  - Coop/Solo: Giving two agents two tasks, or give the same two tasks to 1 agent
  - Headline results of CooperBench: coop performance is lower than solo by 30%-50%.
  - For Coop, we have an updated setting: Coop+git. It was not statistically better than Coop in 02/2026.
  - In experiments done in 06-07/2026, we added team harness, which was inspired by Claude Code's Agent Teams harness, but more generic for all agent models and harnesses.
  - Results in [Team Harness Ablation report](https://cooperbench-reports.pages.dev/cooperbench/team_harness_ablation_report): with codex + GPT-5.5 on flash-50, solo 48%, coop 26%, coop+git 56%, team 62%.
  - And then, we tried to optimize this harness, but we didn't find meaningful improvements on top of the harness we provided. However, smaller models may still not benefit from either git (as shown in 02/2026 experiments) or harness (this is something we can test now).
  - Question of this report: **whether the same team harness benefit appears for a
  small model (Qwen3.5-9B), and if it does not, whether automatic optimization
  of the harness can recover or exceed it.**

## 2. Evaluation setup

- Agent harness (CooperAgents), mini-swe-v2, Qwen3.5-9B
  served on Modal (vLLM, 32k context, temperature 0).
- Benchmark sets:
    - fixed-10 (CooperBench-flash)
    - qwen-14 (13 pairs where Qwen-9B solo implemented at least one feature in
      calibration, plus 1 pair with zero features from the same repositories),
    - dev-set-2 (14 unused CooperBench-lite pairs)
    - held-out set (14 unused lite pairs certification only).
- Metric: features passed out of 28; pair pass rate. k repeated runs per configuration.
- Measurement rules acquired during the program (single-run noise ±4 features;
  k≥3 for screening; k≥5 for conclusions; attribution checks per mechanism).

## 3. Result of direct team-harness application

- Initial fixed-10 runs: solo 1/10, team 1/10 pairs (floor).
- Unpinned sampling: two identical team configurations differed by 5 features;
  temperature pinning added.
- Calibrated set baselines: solo 11.3/28.
- Team with coordination tools, direct application (Q4): 3.0/28.
  Diagnosis: merge produced conflict artifacts; message tool unused (0 calls).
- Framing for the report: the frontier-model result (team > solo) does not
  transfer directly; the sections after this describe the optimization loop.

## 4. Optimization loop description

- Loop: propose a mechanism → implement behind a flag → run k repetitions on a
  fixed set → attribution check (did the mechanism fire; did its firing relate
  to the outcome) → keep, drop, or park → record in docs/SEAM_BACKLOG.md.
- Idea sources tracked per iteration: prior-round diagnosis, CooperBench
  team-harness toolkit, user proposals, negative-result analysis.
- Infrastructure: EC2 worker fleet (up to 15 concurrent runs), dispatcher
  scripts, result sync, hung-eval handling.

## 5. Iterations (numbered, with idea provenance)

Planned numbering (can be compressed or expanded on request):

| # | Content | Idea source | Outcome |
|---|---|---|---|
| 1 | Baselines + infra corrections (PATH, provider prefix) | initial runs | floor identified |
| 2 | Calibrated set + feature-level metric | floor diagnosis | measurable baselines |
| 3 | Temperature pinning + repetition protocol | variance observation | ±4 noise floor established |
| 4 | Q1 do-no-harm gate | corruption diagnosis | parked (mechanism fired 1/14) |
| 5 | Q2 best-of-2 with mechanical selection | T6 negative result + variance | kept (+2.3) |
| 6 | Q3 hotter second attempt | Q2 tie analysis | dropped |
| 7 | Q4 parallel + coop tools (build-on-prior excluded per user direction) | CooperBench toolkit | 3.0/28; merge diagnosis |
| 8 | Q5 repair-gated merge | Q4 diagnosis | kept; 15.7/28 |
| 9 | Merge-gate variants (Q5f, Q5g, Q10) | efficiency; semantic-leak diagnosis | closed; repair limits identified |
| 10 | Selection stacks (Q7, Q7g, Q8, Q11) | compose Q2 on Q5 | selection +2 replicates; stacks do not exceed Q5+selection |
| 11 | Toolkit with system-prompt billing (TK1–TK5) | user prioritization; TK3 prompt-salience result | usage rises; scores at 15.0 |
| 12 | Allocation tools (TK6 claim mode, TK7 spawn) | user question on untested tools | claim neutral; spawn unused |
| 13 | TK9 board + apply-chain + selection | composition of measured gains | 17.0/28, confirmed k=5 |
| 14 | TK9f + repair-time sweep | user request for lower latency | accuracy loss; long repairs contribute accuracy |
| 15 | Focused repair, best-of-3/4 | user selection of directions | bo4 17.7 at k=3, confirmation in progress |
| 16 | Held-out certification | selection-bias hygiene | scores at solo level on random pairs |
| 17 | C2 live coordinator | user proposal | accuracy neutral; p99 halved; replicated on two sets |
| 18 | p99 efficiency axis + TK9×C2 | user proposal | final front: Q4 → C2 → TK9C2 → TK9 |

## 6. Pareto front construction

- Front definition (accuracy vs efficiency), axis choice (mean, then p99 per
  user direction), and front at four stages of the program.
- Figures: docs/pareto_qwen14.png (p99 axis), docs/durations_qwen14.png (ECDF). Remove the reference/sequential data points from the graph. 
- Final front table with runs, means, percentiles.

## 7. Findings

- Coop << Solo; optimization recovers and exceeds the sequential team on the
  development set. [Decision: sequential appears in Findings as a text-only
  comparison (15.3/28 at 512s mean, p99 1260s); it is excluded from all
  figures.]
- Best-of-N (N=2) adds approximately +2 on stable bases (5 replications).
  - Original unit tests
  - Critic agent-written unit tests
- Repair corrects mechanical merge damage; semantic damage is detected by
  running unit tests and critic agent-written checks (the behavioral gate) and
  remains uncorrected. [Note: no LLM judging is involved in this detection; an
  LLM-as-a-judge selector was evaluated in the GPT-5.5-era program (T6) and
  dropped. Confirm intended wording.]
- Tool usage requires system-prompt guidance;
- Live coordinator: detection and steering verified; improve efficiency but not performance
- TK9 − TK8 gap (17.0 vs 12.7). The two configurations differ by one
  component: TK9 runs the TK8 pipeline twice concurrently and submits the
  candidate with the better unit-test result (Best-of-2); TK8 submits its
  single attempt directly. Best-of-2 added ~+2 on other bases; here the gap is
  +4.3. Candidate mechanism: TK8 single attempts are bimodal (board and tool
  activity competes with implementation for the 50-step budget; some attempts
  spend the budget on bookkeeping), and the unit-test selector discards the
  low-scoring attempt, so the submitted distribution loses the lower mode.
  Verification of this mechanism from selection logs is possible; currently
  Cause unidentified.




## Open items to resolve before writing

- bo4 k=5 confirmation (2 runs in progress) — affects Section 5 row 15 and the
  final front.
- ho-q2-b (last certification run, in progress) — affects Section 7.
- Length target and audience (internal note vs shareable report)?
- Whether to include the GPT-5.5-era seam program (S/T/C/P series) as
  background or keep the report Qwen-only.
