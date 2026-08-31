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

## PROGRAMBENCH TEAM-GOAL PROGRAM (2026-08-11): does self-evolution generalize?

User directive: test whether the self-improvement loop generalizes to the
TEAM-GOAL scenario — one indivisible task, a 2-agent team that must DISCUSS
to divide the work (explicitly NOT redundant full attempts; a bo2 arm was
launched and killed on user correction). Single instance per user
instruction: abishekvashok__cmatrix.5c082c6 (ProgramBench: reconstruct
source from execute-only binary + docs; graded by behavioral test suite via
`programbench eval`).

Setup: scripts/bench_programbench.py; DockerEnv grew repo_path=/workspace,
network=none, user=agent (image defaults to root, which would let agents READ
the execute-only reference binary — fidelity fix). Model Qwen3.5-9B, step
limit 100, temperature 0. Fitness = behavioral tests passed; k reps for
decisions (single-instance noise unknown, calibrate from rep pairs).

Round 0 arms (running): solo (1 agent) · coopgit (2 agents, git-share,
mechanical merge, no repair) · teamfull (roles+board+scratchpad, lead
merges) · divide (roles machinery + NEGOTIATION protocol: first action =
send_message proposal with wait:true, explicit agreement required, agreed
division on board/PLAN.md, each implements ONLY its components; messaging
system-prompt billed per Iteration 11 lesson).

Loop plan: eval round-0 → mine trajectories (did negotiation happen? was the
division honored? did the merged program compile?) → propose mechanism →
implement behind flag → k reps → keep/drop → record here.

INFRA SHAKEOUT (round-0 restarts, 2026-08-11/12):
1. Empty patches: `git add -A` FATALS on the execute-only reference binary
   ("unable to index file 'executable'") → nothing staged, every diff empty.
   Fix: repo-local .git/info/exclude for `executable` + `shared/`.
2. Root-owned shared volumes: agents run as uid "agent" (cleanroom fidelity —
   root could READ the execute-only binary) and could not write PLAN.md /
   patch exports / git-share. Fix: post-create root chown.
3. Context overflow at 1000-step budgets: BOTH agents died with
   ContextWindowExceededError in coopgit-a (steps 86/40) and teamfull-a
   (58/103). Diagnosis: proactive compaction (28k trigger) cannot catch a
   single-step jump — one tool call writing a whole 1600-line cmatrix.c adds
   >10k tokens at once (tool-call ARGUMENTS live in history); prompt went
   18.5k → 32.8k in one turn. Fix: trigger lowered to 20k + reactive
   _emergency_truncate() on the overflow error (mechanical: keep system+task+
   recent turns, clip >20k-char bodies/arguments, re-orientation stub; NO
   model call — a summarize request would itself overflow). Unit-checked.
   Truncated-run submissions archived under runs/round0-attempt2/ and
   evaluated as a truncated-budget reference point.
4. Container keepalive (4h) expired UNDER the 1000-step runs — agents spent
   an hour exec-ing into dead containers before detection ("docker ps: 0
   running, 4 tasks alive"). Fix: keepalive=24h for all ProgramBench
   containers. Also: truncated coopgit-a compile failure = CONFLICT MARKERS
   in cmatrix.c ("<<<<<<< ours") — the CooperBench Coop failure mode
   replicated on the team-goal scenario; teamfull-a = incomplete code.
   Round-0 attempt 4 launched with all fixes.

ROUND 0 RESULTS (2026-08-12, k=1 each, step-limit 1000):
- solo: 0 (EMPTY patch — all 1000 steps = ONE looped command, `sudo -n -u
  root head -c 100 ./executable`; tried to read the protected binary from
  step 1 and never recovered; deterministic at temp 0).
- coopgit: 0 (compile_failed — CONFLICT MARKERS in cmatrix.c again; 2nd
  independent replication of the Coop merge-damage mode; 117min, 1086 steps).
- teamfull: 0 (compile_failed — NO compile.sh in the lead's tree; suspiciously
  short run, 169 steps/33min → premature completion).
- divide: DNF — killed at 14h. Negotiation SUCCEEDED (PLAN.md at 40min);
  then one agent degenerated (only output: a 122KB options.h dumped to the
  scratchpad, silent for the last hour+); the other kept coding; the lead's
  sleep-poll wait loop burned steps at ~40s/step with no wall-clock cap.
  The wait protocol's unbounded tail is itself the finding.

ITERATION 1 (launched): transplant of the two confirmed qwen mechanisms.
- Build gate + repair (runner-level, --repair): after integration, apply the
  patch to a fresh tree; if `bash compile.sh` doesn't produce ./executable,
  ONE repair agent runs in that tree with the build log as evidence
  (= Coop+Repair with the BUILD as the gate). Targets coopgit's markers AND
  teamfull's missing compile.sh.
- Per-agent wall-clock cap (TeamSpec.agent_time_limit, 3600s): bounds the
  divide wait-loop tail.
Arms: coopgit-r / teamfull-r / divide-r (k=1 screen).

ITERATION 1 RESULTS (k=1 screen, % of 506 behavioral tests):
- coopgit-r1: 91 — repair FIRED (52 steps) on a broken merge, gate_after=0.
  Clean attribution: the build-gate+repair mechanism converted round-0's
  conflict-marker failure into the TOP score. Coop+Repair transfers.
- divide-r1: 86 — gate passed without repair; negotiation + division + 1h cap.
- teamfull-r1: 78 — gate passed without repair; ran full hour (1382 steps vs
  round-0's premature 169) — attribution of the behavior change to the time
  cap's prompt notice NOT yet verified.
- solo: 0 (unchanged reference).
Round-0 → Iteration-1: 0/0/0/DNF → 91/86/78. The qwen result structure
replicates on the team-goal scenario: repair-gated integration recovers the
coop team; harness scaffolding beats bare solo (0).
k=3 confirmation launched (r2, r3 for all three arms).

K=3 + ITERATION 2 RESULTS (2026-08-12/13, % of 506 tests):
- divide+gate: {86, 0, 87} mean 57.7 — strongest, most consistent base.
- teamfull+gate: {78, 28, 26} mean 44.0 — builds reliably, often shallow.
- coopgit+repair: {91, 0, 0} mean 30.3 — k=1 high regressed (7th k=1
  regression across both programs).
- coopgitc2 (coordinator) k=1: 85 in 785s — 4.7x FASTER than every other
  arm at comparable score; the qwen efficiency-point signature replicates.
- dividebo2 (best-of-2 teams, mechanical selection) k=1: 73; selection
  demonstrably picked the building team over the broken one ((1,1) vs (-1,0)).
- ATTRIBUTION: every 0 = a FAILED repair submitted anyway (repair fired 4x,
  succeeded 1x; runner trusted gate_after=1 output). Repair reliability is
  the binding constraint, not the bases.

ITERATION 3 (launched, k=3 on divide + coopgit): repair-retry (2 attempts,
fresh evidence) + NEVER trust repair self-report — submit the mechanically
best candidate among {pre-repair, attempt1, attempt2} by pb_score
(build + behavioral probes vs reference binary).

ITERATION 3 RESULTS: coopgit {0, 87, 86} mean 57.7 (was 30.3) — build-zeros
eliminated where repair is feasible (1 irreparable merge remains: both
attempts hit 150-step limit). divide {26, 82, 13} mean 40.3 (was 57.7) —
variance moved to the BUILDS-BUT-SHALLOW class (26/13 = gate passes, weak
behavior; one graded branch errored on i3r3), which build-repair cannot
address. Single-instance k=3 noise ~±25; ordering coopgit-vs-divide not
settled. Failure taxonomy stable across iterations: (a) unbuildable merges →
repair (handled), (b) shallow merges → selection (dividebo2 evidence).

ITERATION 4 (launched, k=3): the Board Best-of-2 analog — 2 full
negotiated-division teams (isolated bus+volumes) → mechanical pb_score
selection → retry-repair tail on the winner. dividebo2-i4r{1,2,3}.
(Also: pkill self-match killed my own shell + the round-0 watcher — the
dispatcher pgrep lesson repeated; use bracket patterns.)

CONSOLIDATED RESULTS (2026-08-13, % of the 769-entry eval.json test list —
earlier integers used `programbench info`'s different denominator; ordering
unchanged). Durations: wall-clock of the full pipeline; nearly all team runs
land in a 3300-4200s band set by the 3600s per-agent cap + merge/repair tail.
- solo: 0 (8268s, 1000-step budget exhausted).
- coopgit i3 (retry-repair): {0, 90.1, 90.0} mean 60.0.
- teamfull (gate): {84.9, 50.1, 47.1} mean 60.7.
- divide i1 (gate): {87.5, 0, 88.9} mean 58.8. divide i3: {45.5, 86.3, 23.7}
  mean 51.8.
- coopgitc2 (coordinator + retry-repair): {86.1, 85.8, DNF~0}. r1 785s/157
  steps; r2 3544s; r3 3837s — r1's 4.7x speed did NOT replicate (single-run
  variance). Coordinator events fired in all reps (LOOP 3-4, COLLISION 2).
- dividebo2 i4 (bo2 of full teams + selection + repair): {80.1, 71.1, 0, 77.2}
  mean 57.1. Selection picked the building team over the broken one both
  times it had signal.

ITERATION 5 (2026-08-13): FITNESS BLINDNESS FOUND AND FIXED — the
builds-but-misbehaves class. coopgitc2-r3's repaired tree scored pb (1,2)
(builds, 2 flag probes match) and was submitted; the ProgramBench evaluator
then hit its 3600s per-branch run_tests cap on EVERY branch attempt
(results_read_failed x2 branches x2 attempts = DNF, effectively 0).
Root cause, isolated by container probes on the three reps' submissions:
r3's binary is an output FIREHOSE — 11.3MB written to the pty in 2s vs the
reference's 88KB (128x): a main loop redrawing without its frame delay. The
flood starves the evaluator's terminal emulation until the hour cap kills
pytest before results.xml is written. (Disambiguation: r2's binary ignores
'q' (quit-probe RC=124) yet graded 85.8 fast — input-handling deviation is
survivable; output-rate deviation is fatal. All three reps' binaries die
with the pty and exit on SIGTERM, so teardown is not the differentiator.)
Fix (scripts/bench_programbench.py), all reference-comparative and
program-general:
- pb_score extended to a lexicographic 4-tuple (build_tier, not_firehose,
  quit_matches_ref, flag_probes): RATE_PROBE (bytes to a pty in 2s; firehose
  = >10x reference, floor 200KB) and QUIT_PROBE (feed 'q', compare exit
  codes) run on reference then candidate.
- build_gate_and_repair: gate is now build AND not-firehose; a flooding
  build triggers repair with rate evidence (BEHAVIOR_REPAIR_TASK — frame
  pacing) instead of build-log evidence. quit mismatch ranks but does not
  gate (r2 evidence).
Retrospective validation on the submitted coordinator patches: r1 (1,1,1,5)
> r2 (1,1,0,3) > r3 (1,0,0,2) — new fitness orders them correctly (and
r3's firehose bit trips as designed) where the old score rated r3 a
healthy (1,2).
MEASURE: coopgitc2-i5r{1,2,3} launched (same flags: 1000 steps, repair,
3600s agent cap) — tests whether the behavior gate converts the coordinator
column's DNF class into repaired or at least demoted candidates.
ITERATION 5 RESULTS — KEPT: coopgitc2-i5 k=3 = {86.0, 85.7, 45.4} mean
72.4 (old-fitness column: {86.1, 85.8, DNF~0} mean ~57).
- i5r1: 86.0 @ 1140s/332 steps, gate passed on first merge (no repair).
  Second sub-cap coordinator finish (now 2 of 6 coordinator runs).
- i5r2: 85.7 @ 2905s/468 steps, no repair.
- i5r3: 45.4 @ 2538s/878 steps — the mechanism's proof case: merge
  unbuildable (-1,0,0,0), repair produced (1,1,0,0) (builds, well-paced,
  0 flag probes), dual gate passed → graded 45.4 instead of the DNF this
  failure class produced under the old fitness. The builds-but-misbehaves
  escape hatch is closed: worst case is now a low score, not an ungradeable
  submission. Coordinator accuracy stability across 5 graded runs:
  86.1/85.8/86.0/85.7 within 0.4.
CAVEAT (self-inflicted): concurrent 16-worker evals starve each other on
the 16-CPU host — solo-i5r1's first eval attempt hit results_read_failed
purely from sharing CPUs with the r3 rerun; its binary probes healthy on
every axis. Killed the r3 rerun (DNF already established); rule: at most
two concurrent programbench evals, never alongside a doomed one.

FLEET EXECUTION (2026-08-14): the 12-node fleet (16c/61g each; old
CooperBench-nagent shard workers, still alive) is provisioned for
ProgramBench: current code + .env.qwen + task image + on-node evaluator.
Tooling in scripts/fleet/: pbrun.sh (dispatch one run, detached, inline
eval, .DONE marker), collect.sh (rsync finished runs back + print scores),
nodes.txt. On-node eval removes the local eval-contention failure mode.
Smoke-validated end-to-end with coopgitc2-i5r4 on 44.249.194.27:
70.2 @ 1404s/474 steps, no repair — third sub-cap coordinator finish.
i5 column at k=4: {86.0, 85.7, 45.4, 70.2} mean 71.8 (old fitness ~57).
Future runs default to the fleet. AWS session expired: instance
management (relaunch/terminate) needs `aws login`; SSH dispatch works.

FULL i5 SWEEP (2026-08-14, all arms, i5 fitness, same services, fleet):
- coordinator (coopgitc2): {86.0, 85.7, 45.4, 70.2} k=4 mean 71.8
- coopgit:   {84.4, 85.3, 30.2} mean 66.6
- dividebo2: {0, 83.1, 86.2} mean 56.4
- solo:      {50.6, 0, 86.7} mean 45.8 (ceiling 86.7 = best single score
  of the program, in 553s — solo ceiling equals team ceiling)
- teamfull:  {0, 71.5, 0} mean 23.8
- divide:    {64.6, 0, 0} mean 21.5
EVERY zero (6 across 18 runs) is the SAME class: unbuildable merge, both
repair attempts hit the 150-step limit, build still failing, all
candidates (-1,·,·,·). NO firehose DNFs anywhere — iteration-5 verdict
holds: every graded run that built was gradeable; zeros are honest.
dividebo2 selection: correct pick both times signal existed
((1,1,1,3) over (1,1,0,1) → 83.1; (1,1,1,3) over (1,1,1,0) → 86.2);
best-of-2-of-broken-teams is still broken (i5r1).
STRUCTURAL FINDING (echoes Round-6 separability): division-of-labor arms
(divide 21.5, teamfull 23.8) now rank BOTTOM — negotiated/dictated split
on an INDIVISIBLE task produces large overlapping patches (35-99KB) whose
merges repair cannot rescue in 150 steps. Redundant-work arms
(coordinator 71.8, coopgit 66.6, bo2 56.4) rank top: on a non-separable
task, redundancy + coordination/selection beats division of labor.
Solo's variance (0-86.7) is the team's whole value proposition here:
teams don't raise the ceiling, they cut the floor.
BINDING CONSTRAINT NOW: the unbuildable-merge/repair-fails class (6/18
zeros, only remaining zero class) — candidates: raise repair step budget,
repair-from-best-agent-tree instead of merged tree, or discard-merge
fallback (submit best single agent tree when merge is irreparable).

MBENCH10 (2026-08-15): 10-task ProgramBench benchmark (first 10
alphabetical instances with images), solo + coordinator, k=1/cell, i5
config. Corrected means after rerunning 7 endpoint-outage-poisoned cells
(the Modal app was deleted mid-batch a second time; redeployed):
- solo 23.2, coordinator 17.2 (per-task: cmatrix 82.6/68.0, walk
  48.5/39.2, tuijournal 45.1/33.8, i3style 26.7/0, fx 21.3/19.7,
  shellharden 8.0/11.8, chroma/srgn/zipfinder/zoxide 0/0 at baseline).
- Structure: on multi-task ground the coordinator LOSES its
  single-instance advantage — solo >= coordinator on 8/10 tasks. The
  cmatrix k>=3 finding (team cuts variance) does not transfer at k=1
  across tasks where the capability floor, not variance, binds.

ITERATION 6 (2026-08-15) — completion gate + env brief, measured on the
5 wave-1 tasks (diagnosis: all four baseline zero modes were detectable
in the agent's own container; post-hoc repair starts cold and failed).
Mechanisms: (A) finish rejected until compile.sh builds a FRESH
./executable in the agent's container (<=3 rejections, error injected as
observation); (B) probed toolchain list + operational no-network
constraint prepended to the task. Clean-cell before/after (solo):
zipfinder 0->44.2 (gate fired 2x then clean submit at step 599 — direct
causal evidence), srgn 0->35.4, zoxide 0->15.3, chroma 0->0 (agent never
attempts to finish; gate cannot fire — iteration-7 candidate: budget-aware
submit nudge), i3style 26.7->8.7 (clean i6d rerun after the escalating-
truncation fix — agent survived 1000 steps; the one honest i6 regression;
final solo i6 5-task mean 20.7 vs baseline 5.3). Coordinator: srgn 0->11.6, others
0->0 (team-zoxide persistently 0 where solo flips — merge overhead eats
the budget solo spent on its std-lib pivot). Solo 5-task mean 5.3 -> ~19.
Infra incidents fixed en route: emergency-truncation clipped tool-call
arguments mid-escape producing invalid JSON (both zoxide-i6 agents dead
at step ~116; now truncates inside parsed JSON values); single
truncation retry could itself overflow (i3style-i6c died at step 324;
now escalates 3 attempts keeping 2->1->0 recent turns); minimal-E0
smoke found cost_tracking crash on unmapped self-hosted model.
MINIMAL E0 (runs/evolve/versions/coopgit-min): 302-line run.py + unchanged
vendored agent (184KB): 2 agents, shared git remote + 45s auto-push sync,
3-way merge with apply-chain fallback. Smoke-validated live (agents probe
the reference binary correctly at 25 steps). Fat snapshot deleted —
evolution now genuinely starts from coop+git.

WHY TEAM < SOLO ON MBENCH10 (diagnosis, 2026-08-15/16): (1) a team run
ends at the SLOWER agent — only 5/20 team agents ever submitted (14 rode
the cap) vs solo finishing early in 4/10 runs; early finish needs BOTH
agents to converge (~p^2). (2) repair fired 9/10 team runs vs 4/10 solo:
the post-hoc 3-way merge of two overlapping trees is the damage source,
and NO AGENT EVER SEES THE MERGED TREE — the first reader is a cold
repair agent. (3) trajectories show the git substrate delivers awareness
without agreement: one agent references shared/<teammate> a handful of
times (0-22 refs, 1-6 file-takes), the other typically ignores it; no
division of labor forms. User correction recorded: division via git IS
the intent; duplicate implementation is a failure mode, not the design.

ITERATION 7 (launched 2026-08-16): PRE-SUBMISSION MERGE — the finishing
agent must merge every teammate branch from the shared remote into its
own tree (conflict markers surfaced to the AGENT, which resolves them
in-context), then pass the fresh-build gate on the MERGED tree
(pb_merge_completion_gate, --presub-merge; agent id written to
/tmp/.agent_id per container; solo/repair envs reduce to the plain gate).
Targets diagnosis items (1)+(2): merge moves from post-hoc mechanical to
in-context agent work. Measuring: coopgitc2 on the 6 tasks where team
trails solo (cmatrix, tuijournal, walk, fx, i3style, zoxide), full i6+i7
stack. teamfull analogue (merge-window reservation) deferred.

TEAM-SIZE SCALABILITY (2026-08-17, coopgitc2 + full i7 stack, k=1/cell):
| task       | t2   | t3    | t4   |
| cmatrix    | 73.2 | 79.2  | 70.1 |
| tuijournal | 48.7 | 48.4  | 33.6 |
| walk       | 46.6 | 42.4  | 50.1 |
| i3style    | 49.5 | 21.3* | 36.7 |
| fx         | 46.4 | 21.2* | 20.2 |
| zoxide     | 13.7 | 19.4* | 11.6 |
| MEAN       | 46.4 | 38.7  | 37.1 |
(* = ran under partial endpoint saturation; other cells clean.)
VERDICT: flat-to-negative scaling. 2 agents is the optimum; the 3rd/4th
agent adds merge surface and coordination cost without new capability.
Clean-cell reading agrees: t3 vs t2 is a wash (+6 cmatrix, -4 walk, flat
tuijournal), t4 loses clearly (3 losses, 1 win, 2 ~flat) despite a healthy
endpoint. Explanation consistent with the program's model: selection over
N trees pays only if trees are independent draws, but the 45s git-share
sync makes team trees CONVERGE (correlated candidates), so extra agents
add little diversity while adding N-1 gate-time merges; token cost scales
linearly, so t3/t4 are dominated on both axes.
Execution incidents (all fixed in code): (1) initial 12-at-once dispatch
saturated the single Modal container (max_inputs=32 never triggered
scale-out); agents entered connection-retry loops the wall-clock cap
could not bound (cap checked only at execute()); 9 runs killed at 8h.
Fixes: wall_deadline now checked at query() entry (vendored agent),
max_inputs 32->16 (scale-out verified live: 3 containers, 20-burst p50
2.8s), staggered dispatch driver (max 3 concurrent). (2) collect.sh
ssh/rsync consumed callers' stdin, truncating a driver's job list —
stdin-proofed. (3) Two wave-1 cells starved in the scale-out transient
(agents ~70s/step, capped at ~50 steps) — rerun clean.

ITERATION 7 RESULTS — KEPT; TEAM OVERTAKES SOLO (2026-08-16).
First pass exposed a design incompleteness: gate diagnostics showed ZERO
rejections and zero conflict rounds (agents' merges pass cleanly — the
45s auto-push sync keeps trees convergent), yet tuijournal/fx regressed
to 0 because the harness still 3-way RE-merged two already-merged trees,
re-creating the damage the gate prevented. Completion of the same idea:
TeamSpec.select_integration — when pre-submission merge is on,
integration SELECTS the best member tree by pb_score instead of
re-merging (one NameError iteration en route: metrics dict not in scope
at the integration branch; all four i7b runs crashed; fixed to stdout
logging).
Final i7 column vs baseline vs solo (6 tasks):
  cmatrix 73.2 (was 68.0; solo 82.6), tuijournal 48.7 (33.8; 45.1),
  walk 46.6 (39.2; 48.5), fx 46.4 (19.7; 21.3), i3style 49.5 (0; 26.7),
  zoxide 13.7 (0; 15.3).
  MEANS: coordinator 26.8 -> 46.4 (+19.6); solo same-6 = 39.9.
  THE TEAM ARM NOW BEATS SOLO (46.4 vs 39.9), winning outright on
  fx (+25.1), i3style (+22.8), tuijournal (+3.6).
Attribution: merge damage was the WHOLE team penalty. With merging done
by an in-context agent and integration by selection over verified trees,
the second agent's work adds value instead of destroying it. Selection
fired 4/4 (chose tree 0 each time; zoxide's [165B vs 77KB] tie-broke to
the empty tree at (-1,·) — repair tail then rebuilt to 13.7; selector
tie-breaking on equal tiers is a known rough edge).
Also launched: solo-i5r{1,2,3} — solo re-run under the SAME current harness
services (repair tail, new fitness, truncation fix); solo-a's 0 predates
those fixes (empty patch after a 1000-step loop). Caveat: solo batch runs
concurrently with the i5 team batch (shared endpoint/CPU), durations
descriptive only.
- solo-i5r1: 50.6 (389/769) @ 430s/132 steps, no repair needed. THE SOLO=0
  RESULT WAS AN INFRASTRUCTURE ARTIFACT — under current harness services a
  single agent produces a real half-scoring submission in 7 minutes. The
  team-vs-solo comparison must be restated against the solo-i5 column
  (r2, r3 pending); the honest claim so far is coordinator ~86 / mean 71.8
  vs solo 50.6 (k=1), not "team vs hard 0".
  (solo-i5r1's first eval attempt DNF'd purely from eval CPU contention —
  binary probes healthy; retry on a free machine graded normally.)

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

TOOL-CALL FORMAT ERRORS ROOT-CAUSED (2026-08-17): 2,370 "No tool calls
found" retry cycles across 94/113 runs were a SERVING-STACK bug, never a
model weakness. Raw-response dump on replayed contexts showed the model's
tool call intact but trapped in reasoning_content — the bf16 serve script
ran --reasoning-parser qwen3 alongside the qwen3_coder tool parser, the
exact combination the qualified config (CooperTrain
configs/qwen3-5-9b.yaml) documents as corrupting tool parsing on vLLM
0.19. Fix: reasoning parser removed, redeployed (old warm container had
to be force-stopped — a redeploy does NOT replace live containers).
Validation (scripts/test_tool_choice_required.py, replaying 8 real
offending contexts): before fix 1/8 responses had tool calls; after fix
8/8. tool_choice="required" was ALSO tested and scored 7/8 — no better
than the fixed baseline and one regression — NOT adopted.
Implication: every bf16-era run paid a step/token tax on this bug
(heaviest: scalability batch, 893 incidents), one more reason the
pending clean re-measurement supersedes those numbers.

REALCMP — VALIDATED TEAM-VS-SOLO ON HEALTHY INFRASTRUCTURE (2026-08-18).
Design: 10 mbench tasks x {coopgitc2 team-size 3 full i7 stack, solo with
gate+brief+repair}, standard limits (1000 steps / 3600s per agent), every
cell gated by scripts/fleet/validate_run.py (first-call delay, latency
medians, stall/format-error counts); resource-damaged cells excluded and
rerun. Preflight (endpoint warm-up + 3x concurrent 22k-token probe + fleet
reachability) gated dispatch; monitor_health.sh watched live.
CLEAN PAIRS (both arms validated): cmatrix 67.6/82.1, srgn 10.1/33.8,
i3style 0/32.8, shellharden 0/27.9, chroma 0/0 — solo mean 15.5, TEAM MEAN
35.3; team wins 4/5, ties 1. Validated singles: solo-tuijournal 42.2,
team-walk 43.0, solo-{fx,zipfinder,zoxide,walk-pending} 0/0/0/-.
Excluded team cells STILL outscored solo even damaged: tuijournal 53.2
(>42.2), zipfinder 38.0 (solo 0), zoxide 17.2 (solo 0), fx 8.2 (solo 0) —
the clean verdict is a conservative LOWER BOUND on the team advantage.
VERDICT: the historical "solo beats team multi-task" result (mbench10:
solo 23.2 vs team 17.2) was an infrastructure artifact; with sound serving
the 3-agent coordinator team decisively beats solo.
Validator lessons encoded en route: median over ALL calls (a productive-
call exemption once left a single outlier as "the median"); starvation =
late first call AND <100 steps (compaction hides early timestamps of
long-running agents); live exec-sampling cannot distinguish fast agents
from hung ones — trajectory timestamps are the only ground truth.
Remaining defect fixed and fleet-synced: proactive compaction's SUMMARIZE
call overflows when one giant observation blows past the window before the
trigger; overflow now falls back to mechanical truncation (unit-tested).
r3 reruns of the 4 excluded team cells + solo-walk in flight to complete
the paired table.

REALCMP DEMOTED TO PROVISIONAL (2026-08-18, user correction). The batch
ran with think-leak polluting compaction summaries (reasoning-parser
removal side effect, found post-hoc): every compacted agent's memory of
its early work opened with the summarizer's chain-of-thought instead of
the FILE-MAP digest. "Both arms equally affected" does NOT make the
comparison fair — two corrupted measurements are two wrong things
compared, and the defect plausibly interacts with the arms differently
(compaction rates differ; polluted memory can CAUSE the loops observed,
e.g. cmatrix-r2 agent3's 82-command probe loop). The realcmp table is
evidence the infrastructure now works, not a measurement of the harness.
DEFINITIVE RERUN queued on the fully hardened stack (clean summaries,
overflow-proof compaction, segments preserved, 180s timeouts,
min_containers=2, validated dispatch).

DEFINITIVE TEAM-VS-SOLO TABLE (2026-08-18, fin batch): 10 tasks x both
arms, fully hardened stack (clean summaries, overflow-proof compaction,
segments preserved, hard 240s completion cap, min_containers=2, staggered
validated dispatch). ALL 20 CELLS RESOURCE-VALIDATED.
| task        | solo | team-t3 |
| cmatrix     | 72.0 | 90.0 |
| tuijournal  |  0.0 | 48.9 |
| walk        |  0.0 | 43.6 |
| zoxide      | 14.0 | 30.2 |
| zipfinder   |  0.0 | 21.6 |
| fx          |  0.0 | 20.6 |
| i3style     | 36.7 | 17.1 |
| srgn        | 10.5 | 11.1 |
| chroma      |  0.0 |  0.0 |
| shellharden |  0.0 |  0.0 |
MEANS: TEAM 28.3, SOLO 13.3 (2.1x). Team wins 7, solo 1 (i3style),
ties 2 (hard zeros). VERDICT: with sound infrastructure and validated
measurement, the 3-agent coordinator team with the full mechanism stack
(env brief, completion+merge gates, selection, repair) decisively beats
solo on ProgramBench — reversing the artifact-era "solo wins multi-task"
and finally answering the original question cleanly.
Cell provenance: replacements used ONLY where originals failed validation
(team-zipfinder starved; solo zoxide/srgn/tuijournal hit the read-timeout
hang, killed at 3h) — the i3style team rerun (41.1) was NOT substituted
for its passing original (17.1): reruns replace invalid cells, never
worse-scoring valid ones.
ROOT CAUSE of the 3h solo hangs (py-spy live capture): litellm/httpx
read timeout does not arm — calls blocked 2h+ in ssl.read awaiting
response headers with timeout=180 correctly passed. Fixed with a hard
future-timeout (240s) around every completion, unit-tested on a
blackhole endpoint.
Loop finding upheld on clean memory: cmatrix-fin3 agent1 looped 1000
steps (96% dup) with CLEAN summaries — pollution was not the sole loop
cause; the duplicate-command guard is the next mechanism, measured
against this baseline.

--- 2026-08-18: OOD batches in flight (t4 scaling + Qwen3.8-27B t3) ---
T4 (team-size 4, fin stack, 9B): 8/10 DONE; partial means over 6 evaluated
cells: t4 30.7 vs t3 28.3 vs solo 22.2 (same-6 subsets). Pattern: t4 lowers
easy-task scores (cmatrix 69.2 vs 72.0), raises hard/high-variance ones
(zipfinder 44.1 vs 21.6, srgn 28.0 vs 11.1). Final table pending fx/walk/
tuijournal/shellharden evals + resource validation.
Q27T3 (Qwen3.8-27B GKE endpoint, t3 fin stack, 10 tasks): wave 1 initially
burned in Azure-401 retry loops. ROOT CAUSE: litellm autoloads ./.env
(python-dotenv, no-override mode) from the job CWD; repo .env holds the Azure
GPT-5.5 profile. .env.qwen was immune because it exports explicit EMPTY
AZURE_OPENAI_BASE_URL/API_KEY (an existing var, even empty, blocks the
dotenv fill; empty is falsy in build_model precedence). .env.qwen38 lacked
those lines -> dotenv injected Azure base+key -> Azure-first precedence won.
FIX: defensive empties added to .env.qwen38 (synced to 11 nodes, verified:
build_model resolves api_base=http://34.63.139.125:8000/v1); poisoned cells
killed, artifacts cleared, redispatched. Lesson: any new env profile MUST
carry explicit empty overrides for every credential family it does not use.

--- 2026-08-19: starvation program — root causes and the standing fix stack ---
Four distinct mechanisms produced "agent starved" (first LLM call delayed
>300s, or mid-run call gaps >> generation time) during the 27B batch:
 1. Launch-surge admission: 30 agents dispatched at once into 1-4 cold pods.
 2. Client timeout below generation time (180s vs multi-minute thinking) —
    timeout-retry storms that kept the server busy with doomed work.
 3. Connection-pool death: autoscaler pod scale-downs AND cloud-LB idle NAT
    drops (~10 min) silently kill pooled keep-alive sockets; the next call
    hangs (httpx read timeout does not arm) until the 600s hard cap.
    The compaction/summarize path bypassed the hard cap entirely -> agents
    wedged forever mid-run, blocking whole cells at thread-join.
 4. Throughput collapse: >~12 concurrent heavy streams (30k prompts, 32k
    thinking generations) per 3 replicas push per-stream speed so low that
    every call exceeds the client timeout. Short-prompt load tests do not
    predict this; measured: cells at 20+ streams got 4 calls/agent/hour.
Standing fixes (all deployed, fleet-wide):
 - starvation_test.py: preflight burst/TTFT/fairness gate at batch concurrency.
 - starvation_demo.py: reproducible mock-server scenarios of modes 1/2 + remedies.
 - COOPER_LLM_TIMEOUT_S (540s) sized to endpoint speed; hard cap 600s above it.
 - summarize_context routed through the hard-timeout wrapper (was unbounded).
 - COOPER_CONN_CLOSE=1: one connection per request; immune to pool death.
 - Replicas pinned (no autoscaler flapping) for batch windows.
 - Heartbeat telemetry (per-call lines + end markers) + live_starvation_check
   (STARVING/STALE/FINISHED) -> starvation_guard auto-kills starving cells and
   requeues them; q27_r4_controller enforces a 3-cell concurrency cap.
Validation of the fix = new runs (r4 wave, t2 stragglers) completing with
clean first-call latencies and passing the resource validator.

--- 2026-08-19: TEAM-SIZE SCALING SERIES COMPLETE (9B, fixed 10 tasks, fin stack) ---
All 40 cells resource-validated (heartbeat-evidence validator where needed).
| task | solo | t2 | t3 | t4 |          MEANS: solo 13.3 -> t2 23.7 -> t3 28.3 -> t4 30.0
| cmatrix     72.0  92.1  90.0  69.2 |  Monotonic with diminishing returns
| tuijournal   0.0  31.8  48.9  48.0 |  (+10.4, +4.6, +1.7 per added agent).
| walk         0.0   0.0  43.6  46.2 |  No size wins a plurality of tasks:
| zoxide      14.0   8.5  30.2  21.5 |  t2 dominates selection-sufficient tasks
| zipfinder    0.0  62.0  21.6  44.1 |  (cmatrix 92.1, zipfinder 62.0), t3 the
| fx           0.0  18.8  20.6  21.8 |  coordination middle (tuijournal, zoxide),
| i3style     36.7   0.0  17.1  21.6 |  t4 the hard tail (srgn 28.0, walk 46.2).
| srgn        10.5  24.2  11.1  28.0 |  chroma/shellharden hard zeros everywhere;
| chroma       0.0   0.0   0.0   0.0 |  i3style-t2 and shellharden-t4 are valid
| shellharden  0.0   0.0   0.0   0.0 |  runs whose EVAL hangs (scored failing).

--- 2026-08-19: 27B OOD TABLE COMPLETE (Qwen3.8-27B thinking, t3 fin stack) ---
All 10 cells resource-valid (r4 wave, 3-cell cap, 4 replicas, conn-close,
540s client timeout, bounded summarize, heartbeat-evidence validation):
i3style 31, srgn 1, all others 0 -> mean 3.2 (9B t3 = 28.3 on same tasks).
FAILURE ANALYSIS (trajectories): serving clean (first calls +2-20s), zero
format errors, competent single tool calls (one agent built a pty TUI test
harness). The binding constraint is thinking-mode call economics: 10k-226k
chars of reasoning per step -> 1-5 min/step -> 33-67 steps per agent-hour
(9B: hundreds). No agent ever reached a submit attempt (all status=limit),
so the completion gate never fired; wall-clock expiry force-merged unbuilt
partial edits -> compile_failed on 6/7 team cells. i3style (small config-DSL
edits, ~40 steps sufficient) scored 31 > 9B-t3's 17.1: where the step budget
fits, 27B capability exceeds 9B. CONCLUSION: the harness's time economics
(3600s wall, submission-triggered gates, fast-call repair) do not transfer
to slow thinking-mode endpoints; a fair 27B evaluation needs ~5x wall clock
(thinking-off would sacrifice the model's capability; rejected).
Solo smoke (cmatrix): serving perfect, ~25-56 calls/hr, no submission in
2.5h; exposed+fixed a latent bug: the solo/basic harness path never passed
agent_time_limit (harness.py run_on_shared call site) -> solo runs had NO
wall deadline (masked on fast 9B).
Pending: B200 endpoint upgrade (user), then re-baseline (preflight +
cadence) and re-run under matched budgets.

--- 2026-08-19: 27B t2 cmatrix GOAL RUN (held B200 us-east1, NVFP4, 135 tok/s/stream) ---
3 independent attempts, harness unchanged (runtime params only: team-size 2,
agent-time-limit 14400, .env.qwen38b200). All resource-clean; all finished in
~2h by SUBMITTING through the completion gate (first 27B runs to do so).
Scores: 94.5 / 84.4 / 97.1 -> best 97.1, median 94.5, both > 9B t2's 92.1.
Cadence on B200: first calls +1-2s, ~110-220 calls/agent-hour (H100: ~40).
CONFIRMS: the earlier 27B zeros were step starvation from thinking-mode call
economics on slow serving, not model or harness weakness. With per-stream
throughput fixed, same model + same harness + same task = best cmatrix score
of the whole program.

--- 2026-08-20: 27B FULL SCALING SERIES COMPLETE (held B200, all 40 cells valid) ---
| task        | solo |  t2  |  t3  |  t4  |   9B: 13.3 -> 23.7 -> 28.3 -> 30.0 (monotonic)
| cmatrix       93.9  94.5  96.1  96.7 |  27B: 37.9 -> 51.3 -> 45.3 -> 50.0 (saturates at t2)
| tuijournal    68.4  61.4  58.1  62.8 |
| walk          64.0  64.6  78.2  64.0 |  CROSS-MODEL ANSWER: team scaling holds in sign and
| zoxide        51.3  24.3  23.6  12.7 |  magnitude (+13.4 solo->t2 at 27B vs +10.4 at 9B) but
| zipfinder      0.0  49.1   0.0  51.5 |  its SOURCE shifts: at 27B all team value comes from
| fx            16.5  23.2  22.9  15.4 |  unlock tasks (zipfinder 0->49, i3style 0->47/65,
| i3style        0.0  47.3  44.4  65.3 |  chroma 0->9, shellharden 23->75); on solo-strong
| srgn          62.4  64.6  59.3  62.4 |  tasks teams are flat or harmful (zoxide 51->24,
| chroma         0.0   9.0   3.2   0.0 |  tuijournal 68->61). Size beyond t2 stops paying.
| shellharden   22.8  74.6  66.8  69.0 |  Consistent with the separability finding at 9B.
cmatrix-t2 = median of 3 replicates (94.5; spread 84.4-97.1 calibrates
single-cell noise ~±6). srgn-t2 = valid r2 rerun (64.6). One NUL-byte agent
crash (fixed in worker) and one 272-team-size shepherd bug (caught at
dispatch, node cleaned) were the only incidents; zero starvation across all
40 cells (heartbeat-verified first calls +1-20s).

--- 2026-08-20: LEADERBOARD CONTEXT (programbench.com, same 10 tasks, official mini-swe runs) ---
Means computed from the 21 per-task leaderboard tables:
GPT5.5(high) 79.5 | Opus4.8(xh) 78.7 | Opus5(xh) 77.3 | GPT5.5(xh) 75.4 |
GPT5.6Sol(xh) 73.5 | Gemini3.6F 73.2 | GPT5.5 72.8 | GLM-5.2 71.3 |
Opus4.7(xh) 70.7 | Gemini3.5F 66.5 | GPT5.6Sol 63.0 | Opus4.7 62.1 |
Gemini3.7F 61.5 | Opus4.6 53.7 | Gemini3.1Pro 52.7 | Sonnet4.6 51.4 |
>> OUR 27B t2 (fin) 51.3 | our t4 50.0 << | GPT5.4 49.1 | our t3 45.3 |
Haiku4.5 40.8 | Gemini3F 40.5 | our solo 37.9 | GPT5.4mini 26.6 | GPT5mini 23.3
READING: the fin team harness lifts open Qwen3.8-27B from below-Haiku (solo
37.9) to Sonnet-4.6/Opus-4.6 territory — past GPT 5.4, Haiku 4.5, Gemini 3
Flash — with model and tasks held constant. Frontier models also show large
single-run variance across their own effort variants (e.g. GPT5.6Sol zoxide
1.1 vs xhigh 90.2; GPT5.5 chroma high 41.7 vs xhigh 13.1), supporting the
±6-10pt noise band for single-cell comparisons.

--- 2026-08-21: TERMINAL-BENCH 3.0 FIN-CONFIG EVALUATION COMPLETE (40/40 valid) ---
coopgitc2-fin-min exact config (teams: repair+gate+brief+presub-merge; solo:
repair), Qwen3.8-27B on held B200, 4h budgets, first 10 single-container tasks.
Official rewards (all-or-nothing): 0 on every cell — no full solves, matching
the frontier profile (leaderboard Opus 4.6: 0% resolved on TB).
PASS-FRACTION table (%):
| task (tests)      | solo |  t2  |  t3  |  t4  |
| bun-sourcemap(36)   63.9  72.2  69.4  19.4 |  MEANS: 15.1 -> 15.9 -> 20.7 -> 16.4
| dedup(13)           53.8  53.8  53.8  53.8 |  t3 leads via the atrx unlock.
| cargo(27)           33.3  33.3  33.3  33.3 |
| atrx-vep(16)         0     0    50.0  37.5 |  THREE REGIMES: capability plateaus
| batched-eval(5)      0     0     0    20.0 |  (dedup/cargo: identical in every arm
| cli-simplex(103)     0     0     0     0   |  and config); variance/merge tasks
| biped(3)             0     0     0     0   |  (bun t4 semantic merge loss through a
| cad-model(8)         0     0     0     0   |  fact-less gate; cli all-or-nothing
| coq(1)               0     0     0     0   |  numerics, smoke t2 hit 58%); team-
| data-anon(8)         0     0     0     0   |  unlock tasks (atrx t3/t4, batched t4).
GATE EFFECT (vs stripped-config ablation): conflict markers eliminated
(bun t3 0->25, t4 0->7; zero markers in all 40 patches); solo/t2 reproduce
within noise; all cells submitted real work (collection stack held).
FAILURE TAXONOMY (teams): semantic merge loss passing a fact-less gate
(bun-t4); budget exhaustion pre-submission (atrx-t2, both agents status=limit,
0 gate attempts); solution-quality variance (cli teams 0/103 with clean 72-83KB
patches vs smoke's 60/103); format-error burn on long-prompt tasks (2 cells,
rate-validator quarantined, re-runs clean).
NEXT (designed, committed, not yet run): TB verification facts — Tier 1
artifact existence+parse gate, Tier 2 instruction-example behavioral probe
(oracle-calibrated offline), harness-owned via verification.*; plus the
in-loop verification hint now in the adapter prompt. Each becomes its own
measured delta.

--- 2026-08-26: TB3 EASY-3 BATCH COMPLETE (12/12 valid; 3 frontier-solvable tasks x solo/t2/t3/t4) ---
Tasks picked from the scraped frontier per-task solve map (react-lead-form
12/12 models solve, gpt2-codegolf 75%, coq-block-bound 70%). Same fin config
+ the in-loop verification hint (adapter HEAD) — config delta vs the 08-21
batch. H100 4-pod endpoint, 12-stream cap, queue shepherd; 3 cells starvation-
invalidated and re-run clean (retries reproduced the originals exactly).
OFFICIAL REWARDS (all-or-nothing):
| task            | solo | t2  | t3  | t4  |
| react-lead-form |  0   | 1.0 |  0  |  0  |  FIRST OFFICIAL TB3 SOLVE (t2).
| gpt2-codegolf   |  0   |  0  |  0  |  0  |
| coq-block-bound |  0   |  0  |  0  |  0  |  (every arm exactly 2/3 tests)
REACT REQUIREMENT-LEVEL GRADIENT: solo 2 behavior gaps (duplicate recording,
stale-file cleanup) -> t2 full pass -> t3 1 gap (idempotent rewrite) -> t4 6
gaps. Team value peaks at t2 and degrades with size — the ProgramBench
saturation shape reproduced at per-requirement granularity; larger teams
merged away more duplicate/conflict semantics.
COQ: all arms (incl. both retries) converge to exactly 2/3 — one proof
obligation beyond the 27B at any team size; team size changes nothing.
GPT2: 0 across arms with three distinct mechanisms: (a) t2/t3 share-sync
starvation on heavy workspaces — model ckpt + safetensors in /app meant
agent branches never received a single sync commit; integration picked the
only nonempty tree (ref-file noise, chosen=0) while agent2's real gpt2.c
(golfed 7361->5106B) died with its container; (b) counterfactual verifier run
on the recovered 5106B source: still fails the <2000B size assert — capability
gap independent of (a); (c) solo submitted a build recipe (gpt2.py/build.py +
.gitignore of build/) instead of the /app/gpt2.c artifact.
EVALUATE() BUG FOUND+FIXED: verifier artifacts were mounted read-only; tasks
whose verifier writes into /app (react) failed with EROFS => false 0. Fix:
stage artifacts to a scratch copy, mount writable. Re-scored the 08-21 batch
(41 cells): no zeros flipped, pass-fractions unchanged — react was the first
task on the write path.
EFFICIENCY: duration roughly flat across arms (3-6h/cell); steps scale with
team size (react 83/222/355/551) — same cost-per-cell shape as ProgramBench.
NEW SEAM DELTAS QUEUED: (S-sync) exclude large binaries from the 45s share
sync so member work propagates on heavy workspaces; (S-select) rank member
trees with the Tier-1/2 verification facts instead of nonemptiness at
select_integration.

--- 2026-08-30: FACTORY-23 PLAIN-SOLO BASELINE COMPLETE (23/23 valid; Tinker-hosted 27B) ---
The 23 ProgramBench tasks from the Factory.ai validation-separation study
(zero overlap with our 10-task slice; large-repo heavyweights). Plain solo
per the official leaderboard convention: NO --repair, mini-swe unmodified,
1000 steps / 4h, Qwen3.8-27B via Tinker OAI endpoint (Qwen/Qwen3.8-27B:
peft:262144, 256K ctx; temp 1.0 / top_p 0.95 / max_tokens 32768).
RESULT: mean behavioral pass rate 0.48%. 21/23 cells = 0, all at the
compile gate (submission's compile.sh fails or patch empty => every hidden
test not_run). Nonzero: stgit 146/2289 (6.4%), bedtools2 51/1093 (4.7%) —
the only two submissions that built. Context: Factory frontier baselines
on these tasks are 9-62% (their system lifts Fable 5 to median 89.3%).
READING: the compile gate is the binding constraint for the 27B at this
scale — the model writes hundreds of KB of source (up to 515KB patches)
that does not build. This is exactly the surface our team-side machinery
(build gate + repair, and the planned pre-authored completion criteria)
targets; the harness deltas on these tasks now have a clean floor to
measure against.
SERVING INCIDENTS (Tinker OAI endpoint, all fixed and committed 6fad49a):
no server-side tool-call parsing (XML fallback parser added); 'context
window' overflow phrasing missed by truncation matcher (widened); 64K
default window (switched to :peft:262144); in-flight 429 storms + slow
per-stream generation under 23-way load (retry cap 10->25, hard call cap
600->1200s). 15/23 cells needed one clean re-run under hardened settings.
COST: round-6 tokens 82M prefill + 24M sample ≈ $386; false starts
(rounds 1-5) ≈ $250-350; EC2 ≈ $60. Probe: sustained 24-concurrent clean
on short calls, but long-prompt full-batch load hits in-flight caps —
Tinker beta is fine for ~12-15 effective streams, not 23 heavy ones.

--- 2026-08-31: FACTORY-23 T2 ARM COMPLETE (23/23 valid, zero invalidations) ---
Same 23 tasks, same model/endpoint/budgets as the 08-30 plain-solo baseline;
t2 = coopgitc2 fin config (repair + completion-gate + env-brief + presub-
merge), 6 concurrent cells (12-stream Tinker cap), 4 waves, ~22h wall.
RESULT: mean 0.95% vs solo 0.48% (2x, small absolute); submissions that
BUILD: 5/23 vs solo 2/23.
Build ledger vs solo: +4 flips to building (7zip 0->1.5, delta 0->5.8,
proj 0->0.1, sox 0->4.6), -1 reversal (bedtools2 4.7->0: integration
produced a non-building tree where solo built — semantic merge loss, same
mechanism as TB3 bun-t4), stgit both built with t2 ahead (6.4->9.8, the
only beyond-the-gate quality comparison available).
READING: the harness's value on large-repo rebuilds concentrates exactly
at the compile gate (predicted by the solo round): repair+gate flipped 4
tasks to building, and on the one dual-build task t2 also passed more
tests. Both arms remain 0 on the 14 heavyweight builds (gdal/ffmpeg/
duckdb class) — model capability, not harness, binds there. The bedtools2
reversal is the standing S-select/S-verify gap: integration selection has
no build-fact ranking on this benchmark path either.
COST: t2 tokens 183M prefill + 60M sample ≈ $899; EC2 ≈ $100. Cumulative
Factory-23 (solo + t2 incl. false starts) ≈ $1.8k.
