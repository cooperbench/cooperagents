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

### Round 8 result — coordination under interdependence (C1), 2026-06-15  ★ FIRST COUPLED WIN

Reframe (user): CooperBench couples pairs on purpose → coupling IS the target; lever = coordination
DURING execution, not partition before. Diagnosis (sequential team per-feature eval, coupled set): the
dominant coupled failure is **f1=F, f2=P** — the SECOND agent breaks the FIRST agent's feature while
editing shared code. Mechanism **C1 (regression-guarded coordination)** = each agent publishes a runnable
check for its feature into `.cb_checks/` (transferred via the shared tree, stripped before grading by
`strip_for_submission`); later agents must keep ALL prior checks green. `TeamSpec.preserve_invariants`,
`bench --preserve-invariants`.

| set | sequential team | + C1 | Δ |
|---|---|---|---|
| coupled set-1 (regression-heavy) | 5/14 | **8/14** | **+3** (all = feature-1 F→P recovery) |
| coupled set-2 (0 f1=F,f2=P pairs) | 9/14 | 8/14 | −1 (overhead, nothing to fix) |
| OLD-10 (mixed) | 5/10 | 4/10 | −1 (overhead) |

- **First attributable coordination win on coupled work, and precisely targeted.** Set-1 +3 = exactly
  the diagnosed regressions flipping; no passing pair lost. Set-2 is the honest control: 0 regression
  pairs → C1 had nothing to fix and its check-writing overhead cost 1 pass.
- **C1 helps iff the set has "later agent breaks earlier feature" regressions.** A real solution to ONE
  hard-coordination failure mode, not universal. Opposite lesson to Rounds 2–4: a coordination mechanism
  DOES move coupled work when it targets a diagnosed failure with a verifiable runtime invariant.
- **Hunt for the NEXT coordination mode (2026-06-15): boundary found.** Diagnosed the other coupled
  failures (`f1=P,f2=F`, `f1=F,f2=F`) from real grader errors: they are per-feature CORRECTNESS bugs
  (pillow palette-sort + missing ValueError; llama_index byte resolution; dspy compression doesn't shrink),
  each failing on its own merits — no "agent 2 broke agent 1", no missing-interface, no merge conflict, and
  SOLO fails them too. So they're agent-capability bound, NOT coordination. **C1 captures essentially the
  whole coordination-addressable surface of coupled work (the regression mode); the residual is capability.**
  Coordination's job here = stop the team from CREATING failures (regressions) a solo agent wouldn't; once
  done, what's left is the agent. (Don't build "coordination" mechanisms for correctness bugs = miscategorized
  capability aids, which already washed as the T-series.)
- **Next (open):** make C1 (a) ADAPTIVE — invoke only when regression risk present, or (b) CHEAPER — use
  the repo's existing test suite as the invariant instead of agent-authored checks, to keep set-1's win
  without set-2/OLD overhead. KEEP behind `--preserve-invariants` (not default) until then.

### Round 9 — where coordination problems live (detector + fresh-task harvest), 2026-06-16

Detector: a failure is COORDINATION-CAUSED iff **solo passes a feature but the team breaks it**.
Harvested 14 fresh pairs (diverse repos, none reused) × {solo, sequential-team, parallel-decompose} +
coupled set-1 = 28 pairs. Coordination-caused feature losses:
- **sequential-seed team: 0/28** (coordination-SAFE — serializes on committed work).
- **parallel decompose: 7/28** (set-1: 5, fresh: 2; split+merge breaks working features).
Fresh-set pass-rates (clean, after fixing infra bugs below): solo 9/14, seq-team 9/14, decompose 7/14.

**Conclusion:** coordination problems are real and findable, and they concentrate in the PARALLEL regime
(merge boundary). The build-on-prior SUBSTRATE is the dominant lever BECAUSE it eliminates them. The open
hard problem = **loss-free parallelism** (parallel speed without the 7/28 integration losses) — where C1's
published-invariant idea should transfer to the integrator (each branch publishes a check; merge must keep
all green, repairing conflicts).

**Infra bugs found + fixed during the harvest (important):**
- `DockerEnv` now retries `docker run` with `--platform linux/arm64` when the native run fails on a missing
  amd64 manifest (many CooperBench images are arm64-only; e.g. `go-chi:task27`). Verified.
- `bench_compare.do_pair` isolates per-pair failures (prints `SKIP`, returns None, filtered) so one bad
  image can't abort the whole run + eval phase.
- **mini-swe `LitellmModel`: removed `AuthenticationError` from `abort_exceptions`.** Azure intermittently
  returns it under transient outages; aborting nuked agents after 1 step → empty patches → SPURIOUS fails
  (a corrupted harvest made decompose look like it beat solo — caught it). Now retried (10× exp backoff).
  Lesson: always check for empty-patch/1-step trajectories before trusting a result.

### Round 10 — loss-free parallelism (guarded merge), 2026-06-16

Built `decompose + preserve_invariants` → each parallel branch publishes a `.cb_checks/` invariant; after
the mechanical merge a GUARDED INTEGRATOR (`harness._run_decomposed`, `guarded_merge` metric) runs all
checks and repairs whatever the merge broke. Tested on coupled set-1 (plain decompose was 2/14, 12/14
conflicts):

| design (coupled set-1) | pass |
|---|---|
| sequential build-on-prior (KEPT BEST) | 5/14 |
| solo | 5/14 |
| guarded-merge decompose | 3/14 |
| plain decompose | 2/14 |

- Mechanism WORKS: guarded integrator recovered merge-broken features, incl. `click/2800 f1_f3` FF→PP
  (worst plain-decompose loss; split+merge had broken both). But double-edged (overhead lost `jinja f8_f9`)
  → nets 3/14, **still below sequential 5/14**.
- **Conclusion: loss-free parallelism only PARTIALLY achievable.** Can't close the gap — residual conflicts
  the integrator can't repair (Round 7 ex-ante write-set limit) + repair overhead. On intrinsically-coupled
  work, sequential build-on-prior remains best; parallel+guard narrows but doesn't overcome serialization's
  structural advantage. Keep guarded-merge behind `--decompose --preserve-invariants` (research, not default).

### Round 11 — adaptive topology (decide sequential vs parallel by observation), 2026-06-17

`TeamSpec.adaptive`, `harness._run_adaptive`, `bench --adaptive`. Run features in PARALLEL from base (each
publishes a `.cb_checks/` invariant); probe the merge (`git apply --check` + run checks); clean → KEEP
parallel (fast), conflict → FALL BACK to sequential build-on-prior (safe), reusing branch 1. The conflict is
the decision — no ex-ante write-set prediction (avoids the Round-7 limit).

Results:
- CooperBench flash-10: solo 5/10, sequential 4/10, **adaptive 5/10** — **all 10 → sequential-fallback**, avg
  775s (slower; pays probe + serialize).
- CooperBench clean-merge-10 (gold-separable): solo 5/10, sequential 4/10, **adaptive 4/10** — **all 10 → fallback**.
- **Parallel path engaged 0/20** on CooperBench: two INDEPENDENT agents collide on shared scaffolding
  (imports/exports/registration) even for gold-clean-merge pairs → CooperBench has NO agent-separable regime.
- Controlled separable demo (2 agents, disjoint files, ~4s each): adaptive takes PARALLEL → **8.0s → 4.1s =
  1.99x faster**, both features intact.

**Conclusion:** adaptive works as designed — sequential's success on coupled work (no regression) + parallel's
efficiency (~2x) on separable work, chosen by observed conflict. Pareto-safe default for DIVERSE workloads; on
all-coupled CooperBench it correctly always serializes (≡ sequential + small probe overhead). Infra: also added
`docker --platform linux/arm64` retry, per-pair SKIP isolation, and mini-swe AuthenticationError retry earlier.

### Round 11b — gold-separability ≠ agent-separability (3-way merge re-test), 2026-06-18

User noted CooperBench is ~75% entangled / 25% not (matches gold-conflict report: 76.5% gold-patch pairs
conflict). Expected adaptive to parallelize the 25%. It does NOT — and it's not a too-strict probe:
replaced `_run_adaptive`'s `git apply --check` with a real base-aware **3-way merge** (`harness._threeway_merge`:
branch-per-delta off base, merge into accumulator; verified to return PARALLEL on genuinely disjoint diffs in
a live DockerEnv). Re-ran on the 10 gold-"not-entangled" (clean-merge) pairs: **still 10/10 sequential-fallback**
(0/30 parallel across all adaptive runs), success 5/10 (= sequential, no regression).

**KEY FINDING: the 75/25 split is a property of the MINIMAL GOLD PATCH, not of agent implementations.** An LLM
agent implementing a feature also touches shared scaffolding (imports, __init__ exports, registration, adjacent
edits) beyond the gold's minimal diff, so two INDEPENDENT agent implementations collide even when the gold
patches merge cleanly. Agent-level separability ≪ gold-level (≈0 here). To exploit the 25% you'd have to stop
agents touching shared scaffolding independently → coordinate on it (=coupled problem) or constrain to regions
(decompose/ownership → incomplete features, Round 7). New: `harness._threeway_merge`, `_apply_commit`.

### Round 12 — generalize: agents choose the topology, domain-agnostic, 2026-06-18

`planner.plan_topology(work, complete_fn)`: a PLANNER AGENT decides the execution topology by reasoning about
subtask relationships in task-agnostic terms (independent→parallel; needs-another's-output→sequential edge;
combine-at-end→fan-in) and emits a dependency DAG + topology label (parallel/sequential/pipeline/hybrid). One
abstraction covers all the patterns asked for: chain=sequential, no-edges=parallel, diamond=parallel-then-join.
Executor (`_run_decomposed`, topo-level scheduling) runs whatever the agent picks; only the integration/conflict
backend (git 3-way merge, `_threeway_merge`) is SWE-specific & pluggable — planning + scheduling are general.
Controlled demo (2×~4s work): parallel 4.0s, sequential 8.1s, pipeline 8.1s. Tests: plan_topology picks
parallel/sequential/pipeline from JSON; falls back to single sequential on bad output. So "agents decide
sequential vs parallel vs parallel-then-sequential" is now a general capability; CooperBench(code) is one
instantiation. New: `planner.plan_topology`, `_infer_topology`, `_default_planner_complete`.

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

## Qwen3.5-9B program — harness discovery for a small open model (2026-07-31)

Goal: rerun the discovery loop with the agent loop held constant (mini-swe) but a
**small open model** — `Qwen/Qwen3.5-9B` served on Modal (`qwen35-9b-32k`, vLLM,
32k ctx, native tool-calls; profile `.env.qwen`, run via
`ENV_FILE=.env.qwen scripts/measure.sh <label>`). Findings are per-regime: they
do NOT transfer back to the GPT-5.5 rows above.

**Infra fixes (affect all models):**
- `DockerEnv.execute` used a login shell (`bash -lc`); `/etc/profile` RESETS
  PATH, hiding image toolchains from the agent — in go-chi images the agent
  could not run `go` at all (observed: `go: command not found`), so it shipped
  build-broken patches blind. Fixed → non-login `bash -c` (image ENV preserved;
  python images verified identical under both). NOTE: old GPT-5.5 Go-pair
  numbers carried this handicap.
- `build_model` provider-prefix: HF-style names (`Qwen/Qwen3.5-9B`) were passed
  to litellm unprefixed ("/" heuristic); now only explicit provider prefixes
  skip the `openai/` prefix.
- `bench_compare --solo-only` added (calibration sweeps).

**Baselines (fixed 10-pair set, step-limit 50):**
| run | solo | team | notes |
|---|---|---|---|
| qwen-base (pre PATH fix) | 1/10 | 1/10 | floor; trajectories real (no degenerate runs) |
| qwen-base2 (PATH fixed) | 2/10 | 1/10 | still floor — set too hard to resolve deltas at n=10 |

**Observed 9B failure modes (distinct from GPT-5.5's spec-mismatch profile):**
- Build-breaking errors (wrong package name, redeclared symbol) — the S5
  verify-and-fix family, inert for GPT-5.5, is worth re-testing here.
- Stuck-loop waste: go_chi/26 solo burned 33/50 steps in a sed loop on cosmetic
  whitespace. A loop-detector/nudge seam is a qwen-specific candidate.

**Calibration sweep (`qwen-calib-solo`, 40 flash pairs, solo-only, 2026-07-31):**
solo pass 3/35 scored (typst×4 unscored — Rust eval under QEMU ≈1h/pair, dropped
from consideration; click/2800 f1_f7 unscored). Combined with fixed-10: **solo
≈11% of flash at pair level** — no ≈30%-solo subset exists in flash; whole-pair
granularity is floor-bound for a 9B.

**Adaptation → the `qwen-14` set + feature-level metric.** New fixed set
(`scripts/measure_qwen.sh`): 5 full-pass + 8 half-pass pairs + 1 hard sibling,
all fast Python evals. Measure at FEATURE level (28 features; `bench_compare`
now prints `features: solo X/2n team Y/2n`) — doubles resolution where pair-level
deltas drown. Both arms measured identically; pair-level still reported.

**`qwen14-base` baseline (13/14 pairs scored; pillow/290 f4_f5 skipped by an
ARG_MAX infra bug, since fixed — giant agent commands now stream over stdin):**
| arm | pairs | features |
|---|---|---|
| solo | 2/13 | **10/26** |
| team (sequential build-on-prior) | 2/13 | **6/26** |

**HEADLINE: team < solo at 9B — the GPT-5.5 team advantage INVERTS.** Diagnosed
the flips (solo≥1 feature → team 0): the dominant mechanism is a later agent
CORRUPTING working code — tiktoken f3_f6: solo PASS → team `SyntaxError:
unmatched ')'` in core.py; dirty_equals: 6 pre-existing IsJson tests broken by
the team. Round 8's regression mode, but at 9B it dominates enough to invert
the sign. The needed invariants are MECHANICAL (syntax/build, existing tests) —
no agent-authored checks required.

### Q1 — do-no-harm gate  ·  status: re-measuring at temp 0  ·  qwen program iteration 1
After each sequential agent, the harness health-checks the tree (go build /
python AST-parse; only DEFINITE defects count — timeout/missing-toolchain reads
healthy). Agent broke a previously-healthy tree → its delta is DISCARDED; next
agent seeds from the last healthy state. `TeamSpec.do_no_harm`,
`bench --do-no-harm`, metric `do_no_harm_discards`. Mechanical, no LLM calls;
team layer filters agent output, agent loop untouched.
- **First run (`qwen14-dnh`): team 11/28 features, 4/14 pairs vs baseline 6/26,
  2/13 — BUT the gate fired only 1/14 pairs (pillow, still 0/2), so the +5 is
  NOT attributable to the mechanism.** The measured "gain" is run-to-run
  sampling variance (no temperature was pinned → provider default ~0.6-1.0).
- **METHOD FIX (applies to the whole qwen program): identical team configs
  varied 6/26 → 11/28 features between runs — small-model variance drowns
  n≈13 seam deltas entirely.** Echoes T6 ("variance is real") but worse at 9B.
  Pinned `COOPER_TEMPERATURE=0.0` in `.env.qwen` (worker `build_model` now
  honors it); re-baselining solo+team at temp 0 (`qwen14-t0`), then re-measuring
  Q1. The unpinned qwen14-base numbers (and the "team<solo inversion" headline)
  are DOWNGRADED to suggestive-only until confirmed at temp 0.
- **Temp-0 baseline (`qwen14-t0`): solo 10/28 features (4/14), team 17/28
  (7/14).** Greedy decoding lifts BOTH arms strongly and RESTORES the team
  advantage (~1.7× solo features, mirroring GPT-5.5's 2×) — the unpinned
  "team<solo inversion" was sampling noise, now confirmed. Variance check
  running (`qwen14-t0b`, identical team repeat) before Q1 re-measure.
- **Temp-0 variance check (`qwen14-t0b`, identical team repeat): 13/28 vs
  17/28 — ±4 features between IDENTICAL configs even at temperature 0** (vLLM
  batching nondeterminism compounds over 50-step trajectories). Noise floor for
  single runs ≈ ±4 features → the qwen protocol is now REPEATED RUNS (k≥3 per
  config, compare means). Baseline reps: 17, 13, (t0c running).
- **Q1 verdict at temp 0: PARKED (not measured).** Failure census of t0/t0b team
  runs: SyntaxError/tree-corruption ≈ 1 pair-instance per run; dominant failures
  are AssertionError (wrong implementation). Q1's addressable surface shrank
  below the noise floor once sampling was pinned — not worth 3×-repeat
  measurement cost. Flag kept (`--do-no-harm`), prior low in this regime.

### Q2 — best-of-2 + MECHANICAL selector  ·  status: implemented, measuring  ·  qwen iteration 2
Harvest the regime's dominant property (±4-feature run-to-run variance) via
sample-and-verify: run the whole team twice (T6 infra), select with a
MECHANICAL verifier — apply each candidate diff in a fresh container, score
(tree_health, repo-visible-tests passed−failed, passed). No LLM judgment (T6's
judge mis-picked; a test-runner is a far better hidden-grader proxy) and no
grader access. `bench --best-of-n 2 --select mechanical`.
- Protocol: 3× `qwen14-bo2-{a,b,c}` (team-only) vs baseline mean of
  {17, 13, 16}/28 features. Cost 2× compute per run + ~2 container-min/pair
  selection.
- **Measured (k=3 vs k=3): Q2 {18,17,18} mean 17.7 sd 0.6 vs baseline
  {17,13,16} mean 15.3 sd 2.1 → +2.3 features AND variance collapse (Q2's
  WORST = baseline's BEST — selection clips the bad tail). t≈1.9 (suggestive,
  not conclusive at k=3). Attribution: 38/42 selections were ties (temp-0
  attempts often converge — little diversity to select from); 4 genuine
  divergences selected mechanically. Cost ~1.9× wall-clock (977s vs 515s avg).
  VERDICT: KEPT as current best team config (sequential + bo2-mechanical);
  supersede-candidate Q3 below.**

### Q3 — diversified second attempt (bo2 + temp-0.7 attempt 2)  ·  status: measuring  ·  qwen iteration 3
Q2's limiter is candidate diversity: at temp 0 both attempts usually converge.
Keep attempt 1 greedy (reproducible floor), sample attempt 2 at temp≈0.7
(diversity), select mechanically. Expected: preserves Q2's tail-clipping while
giving selection real choices; cost unchanged (2×). Implemented:
`TeamSpec.temperature` + `TeamSpec.diversity_temperature` plumbed per-attempt
through the worker; `bench --diversity-temp 0.7`. Measuring: 3×
`qwen14-q3-{a,b,c}` vs Q2 {18,17,18}; chain also runs 2× solo repeats
(`qwen14-t0s2/s3`) to firm up the team>solo claim (solo has 1 run: 10/28).
- **Solo k=3 complete: {10, 11, 13} mean 11.3 → team>solo at 9B is now FIRM**
  (team baseline mean 15.3, Q2 mean 17.7).
- **Q3 partial: {17, 14} — trending BELOW Q2** (hot second attempt appears to
  hurt as much as its diversity helps). Run c crashed (see incident) →
  re-running as `qwen14-q3-c2` before verdict.
- **INCIDENT (method): edited the harness worker-call signature while the q3
  chain was mid-flight; run c's fresh process loaded mismatched code and
  SKIPped all 14 pairs (TypeError). RULE: never edit harness/worker code while
  a measurement chain is running — stage edits until chains drain.**

### Q4 — coop tools (CooperBench team-harness shape)  ·  status: measuring  ·  qwen iteration 4
User-requested: explicit runtime coordination instead of (not on top of) the
sequential substrate. Agents run CONCURRENTLY from base (own containers,
no-seed) with a bus-backed `send_message` tool (vendored mini-swe already had
the tool + per-step inbox drain into `[Message from ...]` observations — wired
to TeamBus via `BusComm`); roster preamble tells each agent who is working in
parallel; standard no-seed tail merges. `TeamSpec.coop_tools`,
`bench --no-seed --coop-tools`. Wall-clock ≈ max(agent) not sum → also a
Pareto-efficiency candidate. Runs: 3× `qwen14-q4-{a,b,c}`.
- **Measured (k=2, run c cancelled as redundant): {2, 4}/28, mean 3.0 at
  ~260s/pair — CATASTROPHIC, and diagnosed at the MERGE, not the agents:**
  7/14 pairs shipped conflict-marker/.rej-polluted trees (SyntaxError on
  import), 4/14 merged to empty; agents' own trajectories were normal.
  Rounds 7–10's parallel-merge failure reproduced at 9B. Coordination never
  engaged: **0 send_message calls across all runs** — the tool is inert
  unprompted at this scale. One pair (datasets/6252) merged clean and PASSED
  both features → the parallel path works exactly when edits don't collide.
  VERDICT: naive parallel = floor reference for the no-seed program; fix the
  merge (Q5), not the agents.

### Q7 — parallel bo2 + repair (front push)  ·  status: staged  ·  qwen iteration 6
Composition play: Q5's repair-gated parallel team × Q2's mechanical selection,
attempts CONCURRENT. Plus efficiency patch (staged in a scratchpad dev copy —
NOT applied while the q5 chain is in flight, per the incident rule):
(a) `_threeway_merge` first in the no-seed tail (auto-resolves non-overlapping
same-file edits → fewer repairs; apply-chain fallback only on real conflicts),
(b) `TeamSpec.repair_step_limit=25` (one uncapped repair ran 44 min),
(c) `_run_best_of_n` runs attempts concurrently on the live path (bo2 ≈ 1
attempt of wall-clock; scripted tests stay sequential).
Predicted point: ~17/28 @ ~600s — would beat the Q2 reference corner on time.
Runs planned: 3× `qwen14-q7-{a,b,c}` = `--no-seed --coop-tools
--repair-integrator --best-of-n 2 --select mechanical --concurrency 3`; then
3× `qwen14-q5f` (Q5 flags on new code) for the efficiency-only point.
- **Measured (k=3 each): Q5f {13,14,12} mean 13.0 @ 370s; Q7 {15,14,16} mean
  15.0 @ 838s. Prediction (17 @ 600s) MISSED — diagnosis: the repair-step CAP
  (25) is the culprit, not the 3-way merge (repair still fired 9-11/14 under
  3-way, but with half the budget repairs got WORSE: base dropped 15.7→13.0).
  SELECTION REPLICATED: +2.0 over its same-code base (Q2 gave +2.3) — the
  composition works; the base regressed under it. Q7-as-run is dominated by
  Q5 (15.7 @ 556s). Efficiency patch verdict: 3-way + concurrency KEPT,
  cap-25 REVERTED (a 44-min repair tail is work, not waste).**
- Ablation running: 3× `qwen14-q5g` (3-way + repair cap 50) → isolates the cap;
  then 3× `qwen14-q7g` (that + selection). `bench --repair-steps N` added.

### Q5 — repair-gated merge  ·  status: measuring  ·  qwen iteration 5
No-seed merge tail now: strip .rej/.orig artifacts (they were shipping inside
integrated diffs!) → `_tree_health` gate → if broken, ONE repair agent runs in
the merged container with a reconcile-both-features brief (`_merge_repair_task`);
clean merges pay nothing. `TeamSpec.repair_integrator`,
`bench --no-seed --coop-tools --repair-integrator`. Runs: 3× `qwen14-q5-{a,b,c}`
vs naive-parallel {2,4} and sequential reference 15.3.
- **Measured (k=3): {15, 16, 16} mean 15.7 sd 0.6 @ ~556s/pair — VERDICT: KEPT,
  first parallel config to match-and-slightly-beat the sequential reference
  (15.3 sd 2.1 @ 512s), with much tighter variance. Repair fired 12/14 pairs
  (run a) — mechanism-attributed (+12.7 features over naive parallel, 3× the
  noise floor). Failure modes remaining: repair itself is hit-or-miss, and one
  repaired tree HUNG the eval suite (infinite loop) — noted as a new failure
  class; the repair brief should warn against busy-loops.** The no-build-on-prior
  program has caught the sequential substrate at N=2; the scaling argument
  (sequential O(N) vs parallel max+repair) now has an empirical anchor.
- Q7 chain launched on the upgraded code (3-way merge, repair cap 25,
  concurrent attempts): 3× q7 then 3× q5f (efficiency-only point).

## PROGRAM REDIRECT (user, 2026-08-01): NO build-on-prior

The user excluded the sequential build-on-prior substrate (it serializes —
O(N) wall-clock with team size). **The discovery target is now the best
PARALLEL (no-seed) team harness.** Build-on-prior configs (team-seq, Q2, Q3)
remain on the Pareto table as REFERENCE points only; `qwen14-q3-c2` was
killed mid-run accordingly (Q3 closed unresolved at {17,14}, dominated anyway).
Honest prior from Rounds 9–11: naive parallel+merge LOSES features on coupled
work; the no-seed regime starts behind sequential. The job: close that gap
with runtime coordination / selection / repair while keeping parallel
wall-clock. Candidate queue (after Q4 measures):
- **Q5 — parallel + adaptive mechanical-repair integrator:** merge, run
  health+repo tests, spawn ONE repair agent only if broken (verify-fix made
  adaptive — pays only on demonstrated breakage).
- **Q6 — contract-first messaging:** before coding, the lead broadcasts the
  shared public names/signatures for both features (S3/P1 over the bus, no
  frozen-stub pre-pass); members must acknowledge and mirror.
- **Q7 — parallel best-of-N:** attempts = whole PARALLEL teams (no seeding
  anywhere), mechanical selection; attempts themselves run concurrently.

## Toolkit program (user-prioritized, 2026-08-03): team-harness tools, PUSHED

Q4's zero send_message calls + the seam history give the design rule: at 9B,
coordination tools must be HARNESS-PUSHED, not offered. Each toolkit capability
is restructured accordingly and measured on the parallel q5g base (k=3 each,
auto-dispatched across the EC2 fleet as machines free up):
- **TK1/Q6 — forced contract** (`--contract-first`): one planner call (same 9B)
  reads both specs and writes the shared-interface contract (names, signatures,
  locations); injected into every brief as a constraint. The pushed form of
  S3/coop-request. Offline-safe (no creds → silently off).
- **TK2/Q9 — live awareness** (`--live-awareness`): `_TeammatePoller` via the
  vendored agent's existing `team_poller` hook — each step, if a TEAMMATE's
  changed-file set changed, inject one `[team] agentX is editing: ...` line.
  The pushed form of fs_mirror/wait_for_message; zero agent initiative, near-
  zero tokens (emits only on deltas).
- Not ported (low prior at N=2, revisit for bigger teams/shared-objective):
  task-list CLI (board is degenerate with fixed 1:1 assignments), spawn tool
  (more 9B agents likely negative), blocking waits (no dependency topology).
Metrics beyond features/time: repair-fire rate (conflict proxy) — the toolkit's
job is to PREVENT collisions the repair pass currently fixes after the fact.

## C2 — live coordinator — FINAL (2026-08-04): a TAIL mechanism, not an accuracy one

C2 = Q5 + coordinator (mechanical loop/stall/collision triggers, LLM-composed
nudges, pushed via team_poller; max 3/agent). Accuracy: NEUTRAL at k=5 on dev2
(+0.4±1.2) — trajectory analysis shows the chain breaks at the LAST link:
detection works (247 real pathologies), steering works (69% of nudged loops
break vs 28% natural), but redirected effort hits the same capability ceiling.
**Tail latency: REAL and replicated on two sets** — breaking loops amputates
runaway trajectories:
- dev2:    p99 2958 → 2178s at equal accuracy
- qwen-14: `qwen14-c2` {15,16,16} = 15.7 (EXACTLY Q5's column) with
  **p99 1262s vs Q5's 2644s — tail halved at zero accuracy cost**, matching
  sequential's predictability (1260s) in a parallel architecture.
**Under the p99 efficiency axis (user-requested), the front is Q4 → C2
(15.7 @ 1262s) → TK9 (17.0 @ 3793s)** — C2 removes solo, TK1, and Q5 from the
front. Deployment guide: C2 for latency-bounded serving; TK9 for max accuracy;
Q5 only if coordination machinery is unwanted. Nudge cap saturates on hard
terrain (fired 42/42 pairs everywhere) — rationing needs a marginal-task
oracle (same limit as selection). Re-test the accuracy claim at ≥27B: the
mechanism's broken link is the model, not the design.

## TEAMFULL — complete-Team cell (2026-08-05): DONE — 13.3 (k=3: 13, 13, 14)

Idea source: user question (was the complete team harness tested?) + user
correction (CooperBench's scratchpad is a shared Docker volume mounted at
/workspace/shared in every agent container — compatible with the
one-container-per-agent constraint). Gap: Iterations 1-19 measured Team
components separately/composed, never the ablation report's Team as one
package. Implemented `team_roles` (TeamSpec flag, `--team-roles`): lead/member
role prompt blocks (CooperBench team-mode analogue), per-run scratchpad volume
`cbt<run>` at /workspace/shared, member exports diff to scratchpad, lead
applies it, LEAD'S TREE IS THE SUBMISSION — no mechanical merge, repair, or
selection. Cell flags: --team-only --no-seed --coop-tools --task-board
--team-roles. e2e test added (test_team_roles_uses_lead_tree_as_submission).
Runs: qwen14-teamfull-{a,b,c} on workers 53.203/56.198/52.227.
RESULT: 13.3/28 (13, 13, 14); mean 498s/pair; p99 1851s (per-run 990/1656/1851).
Above Coop 3.0 and Solo 11.3 with NO harness repair/selection — the lead agent
performing the merge replaces the mechanical merge and recovers most of the
Coop damage. Below Coop+Repair 15.7 / Board Best-of-2 17.0; dominated on the
p99 front by Coordinator (15.7@1262). tkgit also FINAL: 14.7 (14, 15, 15) —
git substrate adds nothing over repair. Attribution within the teamfull
package (scratchpad vs roles vs lead-merge) not measured.
bo4-e NOTE: the original bo4-e run was cut short (6/14 pairs evaluable, 8
features on the partial set — excluded from the k=5 average); bo4-e2 rerun
queued behind bo4-d on 56.105.
BO4 CLOSED (2026-08-05): k=5 final 16.4 (17, 19, 17, 13, 16), pooled p99
4322s — below TK9 17.0@3793 on BOTH axes; k=3 screening value (17.7) not
reproduced (5th such regression). TK9 stands as the confirmed accuracy winner.
ALL program runs complete; front unchanged: Q4 → C2 → TK9C2 → TK9.
Incident during launch: a stale dispatcher (old queue_w114) woke up on the
recycled IP 56.198 and launched a DUPLICATE qwen14-tkgit-b colliding with the
legit relaunch on 54.226 — killed the stale dispatcher + duplicate; rule:
kill leftover dispatchers when their worker is terminated.

## TK9×C2 marriage (2026-08-05): a fourth front point

`qwen14-tk9c2` (TK9 flags + --coordinator, each bo2 attempt gets its own
coordinator): **{16,16,17} = 16.3 @ p99 2852s** (median 706, n=42). The
coordinator trims the AGENT-phase share of TK9's tail (3793→2852, −25%) but
cannot reach the repair/selection share; accuracy −0.7 vs TK9 within
overlapping ranges. NON-DOMINATED → final p99 front:
**Q4 → C2 (15.7 @ 1262) → TK9C2 (16.3 @ 2852) → TK9 (17.0 @ 3793).**
Each front point trades ~0.7 features per ~1000s of p99 — the regime's
exchange rate between accuracy and tail risk, now mapped at four points.

## TK9f — speed package — CLOSED (2026-08-04): the tail IS the accuracy

TK9f = TK9 + repair wall-clock cap 480s + selection health-fast-path +
parallel candidate scoring: **{13, 16, 16} mean 15.0 @ ~870s** (median 736s,
p90 1572s, max 1874s). Faster than Q2 on every percentile — but −2.0 features
vs TK9 leaves it DOMINATED by Q5 (15.7 @ 556s). Conclusion: TK9's slow tail
(long repairs, full verification) is where its accuracy lives; capping it
converts TK9 into an expensive Q5. The accuracy/time front between Q5 and TK9
is real and not arbitrageable by truncation. Final recommended points:
**Q5 for speed/stability, TK9 for accuracy**; `--repair-time` kept for
latency-budgeted deployments that accept the trade.

## FINAL RESULT (2026-08-04): TK9 — CONFIRMED WINNER at k=5

**TK9 = parallel agents + fair-billed tools + shared task board + apply-chain
merge + syntax-gated full repair + best-of-2 mechanical selection:
{17, 19, 16, 17, 16} — mean 17.0, sd 1.2 @ ~1170s/pair.** Every run ≥16; 4/5
exceed Q5's best run. First config to clear the plateau AND survive k=5
confirmation (TK4's 17.0 did not — {17,19,15,12,13}→15.2). Statistically
indistinguishable from the excluded sequential Q2 corner (17.7 @ 977s): the
parallel regime MATCHED the best build-on-prior config on accuracy at ~1.2×
its time — with O(max-agent) instead of O(N) scaling in team size.

**Open mechanism question (flagged, not resolved):** TK9's +4.3 over its own
base TK8 (12.7) exceeds naive E[max-of-2] arithmetic; the board×selection
interaction is superadditive in a way the composition model doesn't predict
(hypothesis: the mechanical selector reliably discards the attempt where board
overhead crowded out implementation; needs targeted analysis of selection
decisions before trusting the story).

Notable closures en route: TK8 (board+apply-chain, NO selection) = 12.7 —
mechanisms cannibalize the shared step budget when stacked without selection;
TK4 board alone = 15.2 (k=5, plateau); TK5 waits 15.0; TK6 claim-mode 14.3/14.3
(N=2/N=3); TK7 spawn: 0 invocations ever (meta-cognitive boundary, not prompt
salience — 4 sibling tools with identical billing were all used heavily).

## QWEN PROGRAM CONSOLIDATION (2026-08-03) — parallel-regime matrix CLOSED

All columns measured at k≥3 on the fixed qwen-14 set, temp-0, fleet-parallel.
Final (features/28 mean @ avg s/pair):

| config | mean | runs | s/pair | verdict |
|---|---|---|---|---|
| Q4 naive parallel | 3.0 | {2,4} | 259 | merge destroys work |
| solo | 11.3 | {10,11,13} | 329 | anchor |
| Q5f 3way+cap25 | 13.0 | {13,14,12} | 370 | speed point |
| Q5g 3way+syntax gate | 13.4 | k=5 | 532 | gate leaks semantics |
| **Q5 apply-chain + full repair** | **15.7** | {15,16,16} | **556** | **WINNER: best mean, sd 0.6** |
| Q10 3way+behavioral gate | 15.0 | {12,16,17} | 526 | = Q5 mean, sd 2.6 |
| TK1 forced contract | 15.0 | {13,16,16} | 590 | toolkit works (compatibility) |
| TK2 live awareness | 15.0 | {15,14,16} | ~500 | cheapest 15-level config |
| Q7g bo2-mech on Q5g | 15.7 | {14,16,17} | ~1030 | selection +2.3, dominated |
| Q8 cross-check bo2 | 15.3 | {16,15,15} | ~1240 | selector sophistication saturated |
| Q11 full stack | 15.0 | {14,15,17,14} | ~1120 | selection fails to add on Q10 base |
| _ref: sequential_ | 15.3 | {17,13,16} | 512 | build-on-prior (excluded) |
| _ref: Q2 seq+bo2_ | **17.7** | {18,17,18} | 977 | unreached from parallel side |

**FINAL FRONT: Q4 → solo → Q5f (13.0@370) → TK2 (15.0@~500) → Q5 (15.7@556).**

**Findings (the 9B-parallel regime, mechanism-diverse):**
1. **A hard ~15-16 plateau replicates across FIVE mechanism families** (repair,
   gates, selection, pushed toolkit, stacks) — the qwen-parallel analogue of the
   GPT-5.5 four-family convergence. Binding constraint: 9B capability, surfacing
   as (a) unrepairable semantic merge damage, (b) selectors saturating at
   repo-test signal, (c) toolkit gains capping at compatibility not prevention.
2. **Best discovered harness: Q5** — parallel agents, apply-chain merge,
   syntax-gated FULL-budget repair. Beats the sequential reference on accuracy
   at comparable wall-clock with the lowest variance measured anywhere (sd 0.6),
   and its cost is O(max agent + repair) vs sequential's O(N) — the scaling
   argument that motivated the no-build-on-prior directive holds at N=2.
3. **Selection adds +2±0.5 on every base it was stacked on (5 replications)**
   EXCEPT on the high-variance Q10 base (Q11) — selection needs a stable base.
   Selector sophistication beyond repo-tests adds nothing (Q8; 9B-authored
   checks too weak to discriminate).
4. **Repair fixes mechanical damage, not semantic damage.** Visible .rej/
   conflict-marker breakage (apply-chain) is 9B-repairable; textually-clean
   semantically-broken 3-way merges are detected (behavioral gate) but NOT
   reliably repaired → detection was never the bottleneck.
5. **Toolkit tools work only when PUSHED, and work via COMPATIBILITY:** forced
   contract and live awareness both hit 15.0 with unchanged repair-fire rates —
   they don't prevent collisions, they make collided code mergeable. Offered
   tools are dead weight (0 uses ever).
6. Method: ±4-feature single-run noise ⇒ k≥3 means everywhere; temp-0 pinned;
   attribution checks (repair-fire rate, selection decisions) on every claim.

## Toolkit round 2 (2026-08-03): fair advertising + the rest of the toolkit

**TK3 — fairly-advertised send_message — CLOSED (k=3): {18, 14, 13} mean 15.0
@ ~550s, ~20 send_message calls/run.** CORRECTS finding 5: Q4's zero tool uses
were PROMPT SALIENCE (bash-mandating system template + buried hint), not
capability — equal system-prompt billing + a first-action protocol flips usage
0 → ~20/run. Chosen messaging then scores exactly like harness-performed
coordination (TK1 15.0) and passive awareness (TK2 15.0): the toolkit
family converges on 15.0 regardless of WHO initiates — the plateau again.
TK3-a's 18/28 is the program's best single parallel run (ceiling exists;
mean doesn't reach it).

**TK5 — blocking request/response — CLOSED (k=3): {16, 15, 14} mean 15.0
@ ~925s.** Waits are used (~14/run) but add nothing and cost ~370s of timeout
idling. Fifth communication mechanism, fifth 15.0.

**TK4 — shared task board — CLOSED at k=5: {17, 19, 15, 12, 13} mean 15.2
sd 2.9 — THE PLATEAU BREAK DID NOT SURVIVE CONFIRMATION.** The k=3 mean of
17.0 (with the program's two best single runs) was the upside tail of a
high-variance config; k=5 lands at the plateau, below Q5 (15.7) with ~5× its
variance. T3's n=10→n=20 wash (GPT-5.5 era) replayed at k=3→k=5 — the
confirmation protocol did its job. Board usage remains heavy (~136 calls/run):
usage and value fully decoupled, again. Sixth toolkit mechanism, sixth
plateau result.

**Measuring — TK4/TK5 (each = TK3 + one toolkit increment, k=3):**
- TK4 `--task-board`: bus task tools (task_create/update/list) via a new
  generic `tool_handlers` dispatch in the vendored agent; fair billing +
  status protocol; board deltas pushed via the poller.
- TK5 `--wait-protocol`: blocking request/response — `send_message wait:true`
  (BusComm.send_and_wait, 60s), billed for use when an agreed name is needed
  before proceeding.

## Pareto front — accuracy (features/28, mean) vs efficiency (avg s/pair)

| config | features | avg time | verdict |
|---|---|---|---|
| solo | 11.3 (k=3) | ~329s | efficiency anchor |
| team sequential (baseline) | 15.3 (k=3) | ~512s | |
| Q2 bo2-mechanical | **17.7** (k=3) | ~977s | accuracy best; attempts still serial |
| Q3 bo2+diversity 0.7 | 15.5 (k=2, partial) | ~883s | trending dominated by Q2 |
| Q4 coop-tools parallel | _measuring_ | _expect ~solo-like_ | |

Planned Pareto move: parallelize Q2's two attempts (currently serial in
`_run_best_of_n`) → same accuracy at ≈½ the wall-clock; would strictly
dominate Q3 and likely leave {solo, team-seq or Q4, Q2-parallel} as the front.

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
