# Configuration recipes — exact invocations per named configuration

Every harness version is a flag combination of the current code (all
mechanisms are TeamSpec flags; nothing was deleted). Base command:

    ENV_FILE=.env.qwen scripts/measure_qwen.sh <label> <flags below>

`measure_qwen.sh` runs solo + team arms on the fixed qwen-14 set; add
`--team-only` to skip the solo arm. Swap in `measure_dev2.sh` /
`measure_heldout.sh` for the other fixed sets. Report names per the
configuration code table in docs/TEAM_HARNESS_OPTIMIZATION.md.

| Configuration (code) | Flags |
|---|---|
| Solo | (solo arm; no team flags) |
| Sequential team | `--team-only` (build-on-prior default) |
| Sequential Best-of-2 (Q2) | `--team-only --best-of-n 2 --select mechanical` |
| Coop (Q4) | `--team-only --no-seed --coop-tools` |
| Coop+Repair (Q5) | `--team-only --no-seed --coop-tools --repair-integrator --repair-steps 50 --apply-merge` |
| Coop+Repair+git (tkgit) | Q5 flags + `--git-share` |
| Coop+git (q4git) | `--team-only --no-seed --coop-tools --git-share` |
| Coordinator (C2) | Q5 flags + `--coordinator` |
| Board+Repair (TK8) | Q5 flags + `--tool-protocol --task-board` |
| Board Best-of-2 (TK9) | TK8 flags + `--best-of-n 2 --select mechanical` |
| Board Bo2 + Coordinator (TK9C2) | TK9 flags + `--coordinator` |
| Best-of-3 / Best-of-4 | TK9 flags with `--best-of-n 3` / `4` |
| Complete Team (teamfull) | `--team-only --no-seed --coop-tools --task-board --team-roles` |

GPT-5.5-era flags (flash-10 program): `--verify-fix` (S5), spec-fidelity
(S8), TDD preamble (T2), completeness review (T3), invariant handoff
(C1/preserve-invariants), `--no-seed` alone (parallel+integrate),
`--decompose`, `--adaptive`, do-no-harm gate (Q1) — see
`scripts/bench_compare.py --help` for the exact flag names.

Environment: temperature pinned via `COOPER_TEMPERATURE=0` (in
`.env.qwen`); k-repetition = re-run the same command with a new label
(`<name>-a/-b/-c`). Selector fast-path is opt-in via
`COOPER_SELECT_FASTPATH=1` (default OFF; it confounded sweeps when
unconditional).

## Per-iteration reproduction

`scripts/repro/reproduce_iteration.sh <1-20>` prints the exact measurement
commands for each report iteration (add `--run` to execute);
`scripts/repro/dispatched_jobs.txt` holds the verbatim job lines harvested
from the fleet queues. Lines the script marks "(reconstructed)" were run
locally rather than dispatched, so their flags are reassembled from the
program record rather than a queue file.
