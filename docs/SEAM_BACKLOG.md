# Co-optimization backlog — team × agent "seam" improvements

**Goal (North Star):** optimize the *team orchestration layer* and the *agent
loop* **as one system**, not separately. This is only possible because the
agent loop (vendored mini-swe, `src/cooperagents/vendor/mini_swe/`) now runs
**inside** the unified harness on a shared bus + shared git tree — so the two
layers can read and write each other's state.

**A "seam" = the boundary between the team layer and the agent loop.** In the
old CooperBench design that boundary was a hard wall (`runner.run(str) ->
patch`). A **seam improvement** exploits the now-open boundary: state that
crosses team↔agent, co-designed across both layers. (A change to the team
alone, or the agent alone, is *not* a seam improvement.)

Work these **one at a time**: implement → re-measure on the fixed 10-pair set →
keep if it helps → record the delta here. That loop is the self-evolving harness.

---

## Baselines (10 flash pairs, GPT-5.5, CooperBench-scored, QEMU-emulated)

Fixed comparison set (same 10 pairs every run):
`dottxt_ai_outlines/1655 [6,7] [7,10]`, `outlines/1706 [4,6] [5,8]`,
`dspy/8394 [3,4] [3,5]`, `go_chi/26 [1,2]`, `go_chi/56 [1,5]`,
`hf_datasets/3997 [2,4]`, `hf_datasets/6252 [4,6]`.

| harness | agent | solo | team | notes |
|---|---|---|---|---|
| CooperBench (published, full flash n=50) | codex | 48% | 62% | codex ≫ mini-swe; different agent |
| CooperAgents unified | builtin toy loop | 20% | 30% | pre-mini-swe |
| **CooperBench team harness** | mini-swe | — | **60% (6/10)** | **like-for-like reference** |
| **CooperAgents unified** | **mini-swe** | **30% (3/10)** | **56% (5/9)** | **MATCHES CooperBench team within noise (Δ 1 pair)** |

**Milestone:** with the same agent/model/eval, the unified harness's team (56%)
matches CooperBench's team harness (60%) within n≈10 noise, and ~doubles solo
(30%). The original "match the team harness" goal is met. Remaining gap to
codex's 62% is agent capability (mini-swe vs codex), not the harness.

Measurement: `scripts/measure.sh <label>` (solo + team, same agent/model/eval).
Decision: `scripts/evaluate_improvement.py --baseline <b> --candidate <c> --judge`
— composite **pass-rate + efficiency + LLM judge** verdict
(`src/cooperagents/eval/{judge,scorecard}.py`).
Reference run: `cooperbench run --setting team -a mini_swe_agent_v2 -m gpt-5.5-hao`.

---

## Backlog

Status: `todo` / `in-progress` / `done` / `dropped`. Record measured team
pass-rate delta (vs the mini-swe baseline above) when done.

### S1 — Region partitioning  ·  status: todo  ·  PRIORITY 1 (now the top lever — see Round 6)
A planner reads both feature specs + repo layout, assigns **disjoint files /
regions** to each agent, and writes that partition into **both** the team
assignment **and** each agent's prompt.
- Seam: team plan → agent prompt.
- Fixes: the Round 6 finding directly — multi-agent value is *separability*, not
  coordination. On coupled pairs (shared files) team=solo and seams don't help
  because the build-on-prior substrate becomes *interference*. S1 attacks the
  actual lever: make the work separable so parallelism pays.
- Lives in: new planner step in `harness._run_isolated` + `mini_swe_worker` prompt.
- Delta: _tbd_

### S2 — Live teammate context  ·  status: done (KEPT — current best seam)  ·  priority: 2
Implemented: `TeamSpec.teammate_context` → each agent after the first sees the
actual prior diff (truncated) and is told to reuse teammates' public names.
**Measured (n=10): team 6/10 (60%) vs baseline 5/10, judge 3.70 vs 3.30 — best
on both Success and Judge.** Suggestive (+1 pair, n=10); confirm at n≈30.
Before/within each agent's loop, inject "files teammates already changed + their
diff + interface notes" pulled from the shared tree / bus.
- Seam: team state → agent mid-loop.
- Fixes: duplication, conflicts, API drift.
- Lives in: `mini_swe_worker` (task preamble + optional step hook) + bus.
- Delta: _tbd_

### S3 — Interface contracts on the bus  ·  status: todo  ·  priority: 3
An agent publishes "I named the field `Params`, signature is X"; the next
agent's prompt is conditioned on it.
- Seam: agent → bus → agent.
- Fixes: cross-feature API mismatch (observed: `URLParams` vs `Params`).
- Lives in: bus (contracts namespace) + worker prompt.
- Delta: _tbd_

### S4 — Spawn as an agent tool  ·  status: todo  ·  priority: 5
Add `spawn_helper` to mini-swe's tool set so the agent itself recruits; the
supervisor reacts (closing the agent-loop ↔ team-growth control loop).
- Seam: agent action → team structure.
- Fixes: oversized / parallelizable sub-tasks.
- Lives in: vendored `models/utils/actions_toolcall.py` (extra tool) + supervisor.
- Delta: _tbd_

### S5 — Team-level verify-and-fix  ·  status: done (available, not default)  ·  priority: 2
Implemented: `TeamSpec.verify_fix` → after the feature agents, the team runs the
agent loop once more as an integration/repair pass ("make both features work and
the project build") on the shared tree. Bench: `--verify-fix`.
- **Measured (cheap probe, go_chi n=2, native):** team 0/2 → 0/2, ~2× time. On
  go_chi/26 it changed *nothing* (f1✓/f2✗ before and after). Why: those failures
  are **API/spec mismatches** (code builds fine; agent named a field
  `URLParams`, hidden test wants `Params`) — out of a build-repair pass's scope.
- **Verdict: not adopted as default** (kept behind `--verify-fix`; revisit only
  for build-error-dominated tasks, untested under emulation). Default off.
After integration, the team reruns the agent loop on the merged tree with one
job: "make it build + pass the visible checks." Budget allocated by the team.
- Seam: team reuses the agent loop as a repair primitive.
- Fixes: build failures (observed), integration breakage.
- Lives in: `harness._run_shared` post-integration step.
- Delta: _tbd_

### S6 — Team-controlled budgets  ·  status: todo  ·  priority: 6
Team allocates steps/cost per agent by role/size (integrator & repair passes get
more) instead of a flat per-agent cap.
- Seam: team policy → agent loop limits.
- Fixes: efficiency; hard tasks starved of steps.
- Lives in: harness step/cost wiring → worker.
- Delta: _tbd_

### S8 — Spec-fidelity instruction at the seam  ·  status: done (dropped — neutral)  ·  priority: 1
Implemented (`TeamSpec.spec_fidelity`, `--spec-fidelity`). **Measured (n=10):
team 5/10 = baseline, judge 3.40 ≈ 3.30.** No success gain (misses are
spec-ambiguous: exact identifier absent from spec). Combo S8+S2 (5/10) < S2 alone
(6/10) → S8 adds nothing. Default off.
**(Added from S5 reflection — targets the dominant observed failure.)** The
recurring failure is **API/spec mismatch**: the agent invents an identifier the
hidden tests don't expect (`URLParams` vs `Params`). The team injects a precise
instruction into every agent's prompt: "the grader's hidden tests reference the
EXACT public names/signatures described in the spec — mirror the spec's
identifiers verbatim; do not rename or invent API surface." Cheap, high-EV.
- Seam: team policy → agent prompt (mirrors the spec into agent behavior).
- Fixes: API/spec-mismatch failures (the most common remaining miss).
- Lives in: `mini_swe_worker` task preamble (+ harness assignment text).
- Delta: _tbd_

### S7 — Action guardrails  ·  status: todo  ·  priority: 4
Team intercepts agent commands that would wipe teammates' work
(`git reset/checkout/clean` on the shared tree).
- Seam: team policy filters agent actions.
- Fixes: clobbered uncommitted work (observed; partly mitigated by commit-between-agents).
- Lives in: `workers/mini_swe_worker.MiniSweEnvAdapter.execute`.
- Delta: _tbd_

---

## Separability hypotheses (Round-6-generated, 2026-06-11)

Round 6 finding: multi-agent value = **separability, not coordination**. On the most-coupled
pairs team=solo and coordination seams don't help, because the build-on-prior substrate becomes
*interference*. These attack the real lever — make coupled work separable, or remove the
interference at the seam. **Measurement:** split the coupled-14 set by coupling type —
*shared-file/disjoint-symbol* (separable at function granularity = the WIN ZONE) vs *shared-symbol*
(genuinely coupled = hard ceiling); expect gains only in the former. Run on coupled-14 AND the old
mixed set (no-regression gate). Key success metric: does **team beat solo** on coupled pairs again.

- **P1 — interface-first scaffolding (contract-first)** (priority 1, highest ceiling): planner emits a
  FROZEN stub (signatures+docstrings) of the shared API both features need, commits it; feature agents
  then run in PARALLEL against the frozen interface so their edits to shared files compose. Converts
  coupled→decoupled (dependency inversion). Lives in: harness pre-pass + `_scaffold_task` + parallel fan-out.
- **P2 — work-unit re-partitioning (S1 at file/symbol granularity)** (priority 1): build a file↔feature
  bipartite graph (spec mentions + repo symbol grep), find disjoint clusters, assign each cluster to ONE
  agent (shared file → single owner) — partition the WORK, not the features. Lives in: planner that
  rewrites assignments in `harness`.
- **P3 — topology-matched execution (dependency-aware)** (priority 2, CHEAPEST): a classifier decides
  independent vs dependent; independent → parallel-on-base + merge (no seeding → no interference),
  dependent → sequential seed. Reuses the existing no-seed merge path + 1 classifier call.
- **P4 — structured/semantic merge** (priority 3): replace `git apply` of the prior diff with hunk/AST
  merge + a conflict-only resolver pass. Attacks interference at integration. Lives in: integrator step.
- **P5 — refactor-for-separability pre-pass** (priority 4, double-edged): a pre-agent splits the shared
  region into separable units before fan-out. High ceiling but edits working code (T3-style regression risk).

### Round 7 result — separability-aware orchestration (G1+G2+G3), 2026-06-13

Implemented `decompose` mode: planner (`cooperagents/planner.py`) re-cuts the task into an
independence-maximizing subtask DAG; `harness._run_decomposed` runs independent subtasks in PARALLEL
(own containers, seeded only along edges) and merges branch deltas. Two planners tested:

| orchestration | OLD-10 | COUPLED-14 |
|---|---|---|
| solo | 30% | 36% |
| sequential team (build-on-prior) | **50%** | 36% |
| decompose — file-level merge | 30% | 21% |
| decompose — region-aware split (owns/write-set) | 30% | **14%** |

- File-level planner MERGES ~every CooperBench pair (pairs are coupled by construction: both features
  from the same PR → always share files) → collapses to ≈solo, loses the sequential team's 50%.
- Region-aware planner (P-series idea: split same-file work by function, disjoint `owns` write-sets,
  ownership boundary in prompt) DOES split (2–3 parallel subtasks/pair) but scores WORSE, and **12/14
  merges still conflicted** (.rej / conflict markers).
- **FUNDAMENTAL LIMIT (the answer to "can we re-divide coupled tasks to have zero conflicts?"):** a
  conflict-free partition needs each subtask's write-set BEFORE coding, but the true write-set is only
  known AFTER coding. The planner predicts regions from the spec; real features need cross-cutting edits
  it can't foresee (shared import, new field, registration line) → agents stray (conflicts) or obey
  ownership (incomplete). **Separability cannot be manufactured ex-ante; it must exist in the task.**
  ⇒ P1–P5 (region/interface/merge tricks) are all defeated by this prediction problem. DROP the
  manufacture-separability program. The only honest path to real parallelism is a benchmark with
  genuinely independent tasks, or a SEQUENTIAL discover-then-partition (= the sequential team we have).

## Tool/workflow hypotheses (LLM-analyst-generated, 2026-06-09)

`eval/analyst.py` read the failing pairs and proposed these. Dominant failure =
**incomplete implementation (4/5)**, seam-addressable. (Agent-capability-bound ≠
dead end: the harness can add tools/workflows to lift effective capability.)

- **T3 — feature-coverage ledger / completeness review** (status: DROPPED — washed at n=20):
  block submission until each feature has evidence (changed symbols / passing probe);
  a reviewer pass enumerates features and fills gaps. Targets incomplete-impl.
  n=10 6/10 vs 5/10 (+1) but n=20 **10/20 = 10/20**: flips 2 fail→pass, regresses 2
  pass→fail (extra pass is double-edged), net zero + slower. See Done log.
- **T1 — API-contract extraction + AST/probe gate** before submit. Targets wrong-API/incomplete.
- **T2 — spec-derived acceptance tests (TDD)** before coding. Targets incomplete.
- **T2 — spec-derived TDD (in-loop self-verification)** (status: DROPPED — wash at n=10):
  prepend a workflow telling each agent to derive acceptance criteria from the spec and run
  throwaway local checks before submitting. n=10 **5/10 = baseline, zero pair flips** (same 5 pass).
  In-loop, not a post-hoc pass, yet still inert. See Done log.
- **T4 — existing-test/convention mining tool** before editing (status: DROPPED — mild regression):
  prepend a workflow telling each agent to inspect existing tests/usages/conventions first.
  n=10 **4/10 (baseline −1)** — the mining step burns step-budget on exploration, costing a pair
  (`outlines/1706 f4_f6`) that baseline/T2 pass. See Done log.
- **T5 — ambiguity triage** (prefer least-new API; flag SPEC-AMBIGUOUS). NOT RUN — same in-loop-prompt
  family as T2/T4 (washed/harmful); T1 is the same post-hoc-pass family as T3 (washed). Prior near zero.
- **T6 — best-of-N self-selection** (status: DROPPED — wash at n=10, 2× cost): run the WHOLE isolated
  team N times (own containers per attempt), then the LLM judge (pairwise, both orders — no hidden-grader
  access) selects the best candidate diff. best-of-2 **5/10 = baseline** with a flip each way (+1706 f5_f8,
  −1655 f6_f7) at 2× compute. Variance (headroom) IS real, but the judge *mis-selected* a pair baseline
  passes — a net-positive selector would need to know which diff passes the hidden tests (= more capable
  than the agent). `TeamSpec.best_of_n`, `harness._run_best_of_n`, `bench --best-of-n N`. See Done log.

## Done log

- **S5 — verify-and-fix — team 0/2→0/2, ~2× time (go_chi n=2) — not adopted (default off), 2026-06-08.**
  Note: integrator pass is inert for API/spec-mismatch failures (code builds; wrong identifier). Revealed that the dominant miss is spec-fidelity → added **S8**.
- **S8 — spec-fidelity prompt — team 5/10→5/10, judge 3.30→3.40 — dropped (neutral), 2026-06-08.** Misses are spec-ambiguous.
- **S2 — live teammate diff context — team 5/10→6/10, judge 3.30→3.70 — KEPT (best on success & judge), 2026-06-08.** Suggestive at n=10; confirm at n≈30. New best team config.
- **T3 — completeness-review reviewer pass — team 10/20 = 10/20 — DROPPED (wash at n=20), 2026-06-09.**
  n=10 showed +1 (6/10 vs 5/10) via its intended mechanism (flipped 2 incomplete-impl pairs:
  `outlines/1706 f4_f6`, `click/2068 f5_f11`) but regressed 2 working pairs (`outlines/1655 f6_f7`,
  `click/2068 f5_f7`) — the extra LLM pass adds as much regression as gain. Confirms the n=10 +1 was noise.
- **Cross-axis convergence (2026-06-09):** both the coordination-prompt seams (S2/S5/S7/S8) and the
  top tool/workflow seam (T3) net to the **same ~50% plateau at n=20**. Binding constraint at this
  scale is agent capability, not the seam. Unified team (50%) ≈ CooperBench team (60%), 2× solo (30%).
- **T2 — spec-derived TDD (in-loop self-verify) — team 5/10 = baseline, 0 flips — DROPPED (wash), 2026-06-10.**
- **T4 — convention-mining (in-loop) — team 4/10 (baseline −1) — DROPPED (mild regression), 2026-06-10.**
  The mining preamble spends step-budget on exploration and costs a pair.
- **T6 — best-of-N (best-of-2) + judge self-select — team 5/10 = baseline, +1/−1 flip, 2× cost — DROPPED, 2026-06-11.**
  Variance/headroom is real but judge selection mis-picks; a net-positive selector ≈ knowing the hidden test.
- **FOUR-FAMILY CONVERGENCE (2026-06-11):** the plateau now holds across four *distinct mechanism
  families* — (a) coordination-prompt seams (S2/S5/S7/S8), (b) post-hoc reviewer/repair passes (T3/S5),
  (c) in-loop workflow preambles (T2/T4), (d) selection over variance (T6). None lifts ~50%. Post-hoc
  passes are double-edged; in-loop preambles are wash-or-harmful (extra exploration eats budget); selection
  is bottlenecked on an oracle no cheaper than the agent. Strong, mechanism-diverse evidence the binding
  constraint is agent capability for mini-swe at this scale — not any single untried seam. Remaining T1/T5
  are variants of already-washed families (near-zero prior); not worth the emulated-run cost.
- **ROUND 6 — coordination-sensitive eval set (2026-06-11): the intuition INVERTS.** Built a graded
  coupling score from gold patches (`scripts/select_coupled.py`: shared files×2 + shared symbols).
  First: the old fixed set was already 8/10 gold-conflict → plateau was NOT a coupling-poor sample.
  Then ran the **14 most-coupled lite pairs** (9 repos): **solo 5/14 = team-baseline 5/14**, and
  **S2 coordination seam 4/14 (−1)**. The team>solo advantage (50% vs 30% on the old mixed set)
  **VANISHES on coupled pairs** — and it's the team that regresses (50%→36%), not solo improving.
  **INSIGHT: multi-agent value = SEPARABILITY, not coordination.** Coupled features → build-on-prior
  substrate becomes interference → cancels parallelism. ⇒ S1 (region-partitioning / disjoint-file
  assignment) is now the top lever; coordination-enrichment seams are aimed the wrong way.
  New artifact: `scripts/select_coupled.py` (reproducible coupling-ranked set selector).
