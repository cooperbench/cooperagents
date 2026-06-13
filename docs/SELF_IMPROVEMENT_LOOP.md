# Self-improvement loop

The repeatable cycle for co-optimizing the team×agent harness. **Always return
here.** It pairs with [`SEAM_BACKLOG.md`](SEAM_BACKLOG.md) (the idea queue +
baseline table + delta log). One pass = one backlog item, measured against a
fixed benchmark, kept only if it helps.

```
        ┌─────────────────────────────────────────────────────────────┐
        │  0. RESUME: read SEAM_BACKLOG.md (baseline + Done log)        │
        │  1. PICK   the top `todo` item by priority                    │
        │  2. BUILD  implement it; keep CooperBench untouched           │
        │  3. GATE   ruff + format + mypy + pytest must pass            │
        │  4. MEASURE scripts/measure.sh on the fixed 10 pairs          │
        │  5. DECIDE  evaluate_improvement.py → composite verdict       │
        │            (pass-rate + efficiency + LLM judge); keep|drop    │
        │  6. LOG    record team/solo + Δ + cost in SEAM_BACKLOG.md     │
        │  7. REFLECT new failures seen? → add new backlog items        │
        └───────────────────────────────  back to 1  ──────────────────┘
```

---

## Preconditions (once per session)

```bash
cd /home/ubuntu/CooperAgents
uv pip install -e ".[dev,mini]" -q          # core + tests + mini-swe worker deps
test -f .env                                # AZURE_OPENAI_BASE_URL / _API_KEY / _DEPLOYMENT
docker info >/dev/null                       # docker daemon up
docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null  # arm64 emulation
```

Invariants (do not violate):
- **Every agent runs in its OWN container/environment** (hard constraint). Never
  share a live workspace; coordinate via the bus + seeding fresh containers with
  teammates' diffs. The team path `_run_isolated` enforces it.
- **Never edit `CooperBench/`** — it is the task source + evaluator only.
- **Agent held constant = vendored mini-swe** (`src/cooperagents/vendor/mini_swe/`);
  **never swap in a black-box agent (e.g. codex)** — that kills co-optimization
  and makes gains un-attributable. Editing the agent loop (prompts/tools/control
  flow) *is* in scope when it's part of team×agent co-design (that's the seam).
- Co-optimize **team + agent together**; every gain must be attributable to the
  harness with the agent held constant. Don't game the metric (codex's 62% is
  context, not a target).
- Keep the benchmark set **fixed** (same 10 pairs) so deltas are comparable.

---

## The fixed benchmark

10 flash pairs, GPT-5.5, scored by CooperBench (arm64 emulated). One command:

```bash
scripts/measure.sh <run-label>     # writes logs/<run-label>-{solo,team}/..., prints pass-rate
```

It runs both arms (solo + team-shared) with the **same** agent (mini-swe) and
model, so the only variable is the harness/seam. Record the printed
`pass-rate: solo X/10  team Y/10` and `avg time`.

Quality gates that must pass before measuring (cheap, no API):

```bash
uv run ruff check src tests && uv run ruff format --check src tests \
  && uv run mypy && uv run pytest -q
```

---

## Composite evaluation (steps 4–5)

Binary pass/fail on 10 pairs is coarse, so DECIDE uses **three signals**, not
one (`src/cooperagents/eval/{judge,scorecard}.py`):

1. **pass-rate** — CooperBench `both_passed` (ground truth, primary).
2. **efficiency** — wall-clock + LLM calls on tasks both variants solve.
3. **LLM judge** — pairwise candidate-vs-baseline on the submitted diffs, run in
   both orders to cancel position bias (a finer signal that moves even when
   pass-rate ties).

Run it:

```bash
# 4. measure candidate (and baseline if not already on disk)
scripts/measure.sh s1            # -> logs/s1-{solo,team}/...
# 5. composite verdict (judge needs API creds; omit --judge for objective-only)
uv run python scripts/evaluate_improvement.py --baseline base --candidate s1 --judge
```

It prints pass/feature/time deltas + judge win-rate and a **VERDICT: KEEP /
DROP / INCONCLUSIVE**. The verdict rule (`scorecard.compare`):
- pass-rate up >1 pair → **keep**; down >1 pair → **drop** (unless judge strongly
  favors candidate *and* it's faster → **inconclusive**, recheck at n=30).
- pass-rate tied within noise → **judge + efficiency** decide.

Caveats baked in:
- **n=10 is noisy (±~15pp).** A `keep`/`drop` from a tie or 1-pair swing is
  suggestive; confirm promising changes at ~30 pairs (`--all-flash` or a wider
  `measure.sh`) before trusting.
- If kept and it raises the bar, **update the baseline row** in `SEAM_BACKLOG.md`.
- If dropped: `git restore`/revert, mark the item `dropped` with a reason.

---

## Resume protocol (fresh session)

1. Read `SEAM_BACKLOG.md` → current **baseline** row + **Done log** = what's
   been tried and the current bar.
2. `git log --oneline` / `git status` → what's in flight.
3. Run the **quality gates** to confirm a clean base.
4. Continue at loop step 1 (pick next `todo`).

State lives in three places only: the git tree (code), `SEAM_BACKLOG.md`
(deltas/decisions), and `logs/` (raw run artifacts).

---

## Per-iteration template (paste into SEAM_BACKLOG.md Done log)

```
S# — <title> — <kept|dropped> — team <old>→<new> (Δ +/-k), solo <old>→<new>,
     cost ~<tok/$>, n=10, <date>. Note: <what worked / why dropped>.
```

---

## Why this is the "self-evolving harness"

Each pass is the harness improving how a team of agents cooperates, validated on
a real benchmark, with the agent loop and team layer optimized **together** (the
seam). The loop is deterministic and resumable, so it runs across sessions —
and could later be driven autonomously (a meta-agent executing steps 1–7).
```
