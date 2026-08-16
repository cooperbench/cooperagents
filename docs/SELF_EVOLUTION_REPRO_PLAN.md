# Agent Team Harness Self-Evolution

## Objective

Package the self-evolution procedure used in this repo as a reproducible algorithm:
the procedure will iteratively improve the team harness through a very simple procedure.


```
Procedure Harness-Self-Evolution:

    # Multi-worker extension of the (single-worker) loop in Karpathy's
    # autoresearch repo:
    # https://raw.githubusercontent.com/karpathy/autoresearch/refs/heads/master/program.md
    # (modify -> run -> measure -> keep/reset), generalized to: many ideas
    # in flight at once, k replicate runs per measurement, and a serialized
    # accept step so the mainline stays a single lineage.

    E          <- E0                      # mainline harness config (a flag set)
    Q_ideas    <- seed_ideas()            # priority queue of (idea, base=E0)
    D_results  <- {}                      # idea -> {base, scores[k], decision}
    Workers    <- fleet nodes             # one measurement rep per node
    Log        <- append-only decision log

    # baseline: measure the mainline first
    D_results[E0] <- measure(E0, k=3, Workers)   # k reps in parallel

    while not (Q_ideas.empty() and all workers idle) and budget remains:

        # -- dispatch (async): keep every idle worker busy --------------
        while Workers.has_idle() and not Q_ideas.empty():
            (idea, base) <- Q_ideas.pop_highest_priority()
            flag    <- implement_idea(idea)      # one mechanism, one flag,
                                                 # offline tests must pass
            harness <- base + flag
            for rep in 1..k:                     # k reps fan out across
                Workers.dispatch(harness, rep)   # idle workers

        # -- collect (async): results arrive out of order ---------------
        (harness=base+flag, scores) <- Workers.wait_for_any_result()
        D_results[flag] <- {base, scores}

        # -- accept (serialized): one mainline, no concurrent merges ----
        with mainline_lock:
            if base != E:                        # mainline advanced since
                Q_ideas.push(flag, base=E)       # dispatch: re-test on E
            elif mean(scores) - mean(D_results[E].scores) > noise_band
                 and no_new_failure_class(scores):
                E <- E + flag                    # accept; tag evo-i<N>
                Q_ideas.rebase_pending(E)        # queued ideas now branch
            # else: reject; E unchanged          # from the new mainline

        # -- research (the feedback step) -------------------------------
        classes <- diagnose(artifacts(scores))   # failure classes, sized
        Q_ideas.push(propose(classes, D_results),# new ideas target the
                     base=E, priority=class_size)# largest class; D_results
        Log.append(idea, base, scores, decision, # prevents re-proposing
                   diagnosis)                    # washed mechanisms

    return E, Log
```

Instantiation for this repo:

1. Start from a fixed simple harness: **coopgit** (2 agents, same task,
   git-based sharing, mechanical merge; no coordinator, no repair, no
   selection).
2. Run the evolution loop against a fixed **CooperBench training slice**,
   accepting or rejecting one mechanism per iteration by measured delta.
3. After evolution terminates, evaluate the final harness once on
   **held-out ProgramBench tasks** that were never used during evolution.

The claim under test: the evolution loop itself (diagnose → propose →
implement behind a flag → measure → decide → log) is the transferable
artifact. Prior evidence: mechanisms evolved on CooperBench (repair,
selection, coordinator, time caps) transferred to ProgramBench with measured
gains (SEAM_BACKLOG.md, "PROGRAMBENCH TEAM-GOAL PROGRAM").

## Fixed elements (pinned before the run starts)

| Element | Value | Where pinned |
|---|---|---|
| Agent loop | vendored mini-swe, held constant | `src/cooperagents/vendor/mini_swe/` |
| Model + sampling | Qwen3.5-9B, pinned temperature/top-p | `.env.qwen` |
| Seed harness | coopgit flags only | `docs/CONFIGS.md` entry `E0` |
| Training benchmark | CooperBench fixed 10-pair slice (the existing baseline set) | `docs/SEAM_BACKLOG.md` |
| Rep count per measurement | k=3 (k=1 screening highs regressed 7+ times across programs) | this file |
| Acceptance threshold | mean delta > measured noise band for the slice (CooperBench: ±1 pair; ProgramBench: ±25 points single-instance) | this file |
| Iteration budget | N=10 iterations or 2 consecutive rejected proposals per failure class, whichever first | this file |
| Held-out test set | 10 ProgramBench instances, listed below, frozen now | this file |
| Compute | fleet nodes via `scripts/fleet/` (one run per node, on-node eval) | `scripts/fleet/nodes.txt` |

### Held-out ProgramBench set (frozen)

The `mbench10` set (first 10 alphabetical instances with available cleanroom
images) is already being used for measurement in the current program, and
cmatrix was used for evolution iterations 1–5. Therefore the held-out set is
the **next 10 alphabetical instances with available images, excluding all
mbench10 members and cmatrix**, resolved once by
`scripts/fleet/select_heldout.sh` and committed as
`scripts/fleet/heldout10.txt`. After the file is committed, no run against
these instances is permitted until the final evaluation.

## The evolution loop (one iteration)

Inputs: current harness config `E_i` (a set of flags), artifact archive of
all prior runs, decision log.

1. **Measure or reuse baseline.** If `E_i` has no k=3 measurement on the
   training slice, run it (fleet, one rep per node).
2. **Diagnose.** Read the failure artifacts of `E_i` (bus logs, agent
   trajectories, merge diffs, eval JSONs). Classify every failed pair/task
   into failure classes (examples from prior programs: unbuildable merge,
   builds-but-misbehaves, agent loop/stall, premature termination, context
   overflow).
3. **Propose one mechanism** targeting the largest failure class. The
   proposal must be:
   - implementable as ONE derived code version (one idea per version;
     an args-delta, a code edit, or both);
   - mechanical where possible (gates, caps, probes, selection) — LLM
     passes are permitted but have historically washed (T2/T3/T4/T6);
   - general (multi-agent-system level; no benchmark-specific tricks in the
     mechanism itself — benchmark specifics live in the adapter, below).
4. **Implement** behind the flag; add a unit/e2e test where the mechanism
   is testable offline.
5. **Measure** `E_i + flag` at k=3 on the training slice, same
   model/sampling/flags otherwise.
6. **Decide.** Accept if mean improves beyond the noise band and no new
   failure class appears; else reject. `E_{i+1}` = accepted config or `E_i`.
7. **Log.** Append to the decision log: iteration number, diagnosis,
   proposal text, config diff, per-rep scores, decision, rationale. Commit
   the code + log in one commit tagged `evo-i<N>`.

Termination: iteration budget reached, or two consecutive rejections on the
top failure class.

### Representation of ideas

An idea has three representations, in order of hardening; `implement_idea`
in the pseudocode is the transformation from the first to the second:

1. **Proposal (natural language, logged verbatim).** A short mechanism
   description paired with the diagnosis that motivated it: the target
   failure class and that class's size. Auditable; only executable by the
   LLM operator. Example: "reject the agent's finish until compile.sh
   builds a fresh ./executable — all four zero-score modes were detectable
   in-container while the agent still had budget."
2. **Code-snapshot version (the durable identity).** A harness version is
   a self-contained folder — its own `src/`, `scripts/`, and a manifest
   (name, parent, idea, args, entry point) — created by copying the parent
   version and applying the idea: an args-delta, an arbitrary code edit,
   or both, with `diff -ru` vs the parent recorded as `delta.patch`.
   The code in the folder IS the identity; execution always runs the
   version's own code, so mainline drift cannot change what an old
   version measures. Flags remain as a per-version CLI convenience
   (`manifest.args`), demoted from identity to parameters.
   Rationale for this over the earlier config-as-flags identity:
   (a) reproducibility — a shared codebase's later edits silently change
   the behavior behind an old flag set (observed: a context-truncation
   fix altered every configuration retroactively); (b) range of
   innovations — flags limit ideas to toggles expressible on current
   code, while structural rewrites are exactly the ideas a later
   iteration may need.
3. **Record.** `D_results[flag] = {base config, metrics, decision,
   rationale}` — used to block re-proposal of washed mechanisms and by the
   replay driver.

   Metrics recorded per rep, in three tiers:

   - **Primary (decides acceptance):** benchmark score — CooperBench:
     pairs passed on the training slice; ProgramBench: % of behavioral
     tests passed per instance.
   - **Secondary (reported on the score-vs-time front; never a tiebreak
     for acceptance):** wall-clock duration of the full pipeline, total
     agent steps, and where billing applies, token/dollar cost. Both
     North-Star axes are tracked; acceptance gates only on the primary so
     deltas stay attributable, and efficiency claims are made from the
     Pareto front over accepted configs.
   - **Diagnostic (feeds the next diagnosis; never gates):** failure-class
     counts, gate/repair firings and outcomes (fired, succeeded,
     candidate scores, chosen), coordinator events (LOOP/STALL/COLLISION),
     completion-gate rejections, patch size.

`Q_ideas` therefore holds tuples `(proposal text, target failure class,
priority = class size, base config)`.

Two exclusions, both deliberate:

- **No free parameters inside an idea.** "Repair with step limit 150" bakes
  150 into the mechanism; if the limit itself becomes the question (repair
  failing at its 150-step budget was a later diagnosis), the tuned value is
  a NEW idea with its own flag delta. Every measured delta stays
  attributable to one change.
- **One idea per version.** A child version differs from its parent by
  exactly one idea (however large its code edit); `delta.patch` is that
  idea's footprint. Bundled changes would make the measured delta
  unattributable.

The taxonomy ablation (Reproducibility, item 6) is the degenerate case
where representation 1 collapses to an enum drawn in fixed priority order —
fully mechanical, no proposal text.

### Benchmark adapter interface

Mechanisms are benchmark-independent; each benchmark supplies an adapter:

- `gate(tree) -> pass | (failure_kind, evidence)` — e.g. CooperBench: patch
  applies + build; ProgramBench: build + output-rate probe.
- `fitness(tree) -> comparable tuple` — e.g. ProgramBench `pb_score`
  (build, not-firehose, quit-match, flag-probes), all reference-comparative.
- `submit(tree) -> benchmark submission layout`.

The iteration-5 lesson is recorded as an adapter requirement: a fitness
blind to a behavior class (output flooding) promotes candidates that fail
evaluation; adapters must probe the behaviors the evaluator exercises.

## Reproducibility mechanisms

1. **Code-snapshot versions.** Every harness version is a self-contained
   code folder (see "Representation of ideas"); versions never share live
   code, so no unversioned behavior change can reach a measured
   configuration. `scripts/team_harness_evolve.py` implements
   snapshot/derive/execute; `docs/CONFIGS.md` remains as the historical
   flag-set record of the pre-snapshot programs.
2. **Decision log.** `docs/EVOLUTION_LOG.md`, append-only, one entry per
   iteration in the format of the Done log in SEAM_BACKLOG.md.
3. **Replay driver.** Replay is direct: re-execute the saved version
   folder (`team_harness_evolve.py run --harness <versions>/<name>`) with
   the same instance and k — no checkout or reconstruction step, since the
   folder contains the exact measured code. Scores match within the noise
   band rather than exactly (sampled agent runs).
4. **Pinned endpoints.** Model endpoint and sampling parameters recorded in
   the log at each iteration; endpoint swaps recorded as comparability
   breaks (this happened once: the Modal redeploy between iterations 3 and
   4 of the ProgramBench program).
5. **Artifact retention.** Every run keeps eval JSON, integrated patch,
   metrics, run_meta (committed); trajectories and submission tars retained
   on disk, gitignored.
6. **Proposer transparency.** The proposal step is performed by an LLM
   operator (Claude) reading artifacts. The diagnosis text and proposal are
   logged verbatim, so a replay can audit every branch point even though
   proposal generation is stochastic. A stricter variant (proposals drawn
   from a fixed mechanism taxonomy in priority order) is available as an
   ablation: taxonomy = {build gate, repair, retry-repair + mechanical
   candidate pick, per-agent time cap, coordinator, best-of-2 + selection,
   behavior-aware fitness}, which is the accepted-mechanism sequence from
   the completed programs.

## Phases and deliverables

1. **Phase 0 — freeze.** Commit `heldout10.txt`, `EVOLUTION_LOG.md` header
   with all pinned elements, `E0` (coopgit) config entry. Deliverable: one
   commit; held-out set untouchable from here.
2. **Phase 1 — E0 baseline.** k=3 coopgit on the CooperBench training
   slice. Deliverable: log entry 0.
3. **Phase 2 — evolution.** Up to N=10 iterations of the loop above, on
   the fleet. Deliverable: log entries 1..N, tagged commits, final `E*`.
4. **Phase 3 — held-out transfer.** Run `E*` AND `E0` (both, for the
   delta) once, k=1 per task, on the 10 held-out ProgramBench instances
   via the ProgramBench adapter. Deliverable: transfer table
   (per-task scores, means, `E*` − `E0` delta) appended to the log.
5. **Phase 4 — report.** Write the evolution trajectory (score vs
   iteration on train; train→test transfer) into a report page with the
   existing report tooling.

## Success criteria

- Primary: `E*` mean > `E0` mean on the held-out ProgramBench set (transfer
  of evolved mechanisms to unseen tasks and an unseen benchmark family).
- Secondary: every accepted iteration's training delta exceeds the noise
  band; replay of any tagged iteration lands in the logged score range.
- Reporting is symmetric: if `E*` fails to transfer, that is the result.

## Risks

- Single-instance ProgramBench noise is large (±25); k=1 × 10 held-out
  tasks averages across tasks. Cross-task variance is the quantity the
  transfer claim is about.
- CooperBench training signal plateaued at ~50% at n=20 in the earlier
  program; if the slice saturates early, the loop terminates on the
  2-rejection rule and the transfer test proceeds with whatever `E*` holds.
- Endpoint instability (Modal scale-to-zero, redeploys) is recorded per
  iteration; measurements affected by infrastructure failure are rerun,
  never patched.
