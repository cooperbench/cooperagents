# Factory-23 Reproduction Guide

Full reproduction instructions for the Factory-23 scaling series: plain-solo,
t2, and t3 arms of `coopgitc2-fin-min` (Qwen3.8-27B) on the 23 ProgramBench
tasks studied by Factory.ai's validation-separation report
(https://factory.ai/news/what-it-takes-for-coding-agents-to-complete-large-software-tasks).

Result commits: solo `807a3a3`, t2 `91c5381`, t3 `d93e65a`.
Required harness fixes: `027abdf` (serving robustness) and the evaluate()
writable-mount fix in `16bd076`. Reproduce from `d93e65a` or later.

## 1. Results being reproduced

Mean behavioral pass rate over the 23 tasks (hidden-test pass fraction from
`programbench eval`; B = the submission compiled):

| task | solo | t2 | t3 |
|---|---|---|---|
| gdal | 0.0 | 0.0 | 0.0 |
| 7zip | 0.0 | 1.5 B | 0.0 |
| duckdb | 0.0 | 0.0 | 0.0 |
| pandoc | 0.0 | 0.0 | 5.9 B |
| ffmpeg | 0.0 | 0.0 | 0.0 |
| samtools | 0.0 | 0.0 | 4.2 B |
| tree-sitter | 0.0 | 0.0 | 0.0 |
| ctags | 0.0 | 0.0 | 0.0 |
| cppcheck | 0.0 | 0.0 | 0.0 |
| ast-grep | 0.0 | 0.0 | 0.0 |
| bedtools2 | 4.7 B | 0.0 | 0.0 |
| delta | 0.0 | 5.8 B | 0.0 |
| doxygen | 0.0 | 0.0 | 0.0 |
| lazygit | 0.0 | 0.0 | 0.0 |
| lnav | 0.0 | 0.0 | 1.0 B |
| peco | 0.0 | 0.0 | 0.0 |
| proj | 0.0 | 0.1 B | 0.0 |
| scc | 0.0 | 0.0 | 0.0 |
| solar | 0.0 | 0.0 | 0.0 |
| sox | 0.0 | 4.6 B | 0.0 |
| stgit | 6.4 B | 9.8 B | 28.2 B |
| svgbob | 0.0 | 0.0 | 0.0 |
| typst | 0.0 | 0.0 | 2.8 B |
| **mean** | **0.48** | **0.95** | **1.83** |
| **built** | **2/23** | **5/23** | **5/23** |

Notes on interpretation: ProgramBench zeroes any submission whose
`compile.sh` fails, so "does it build" is near-binary per cell and dominates
the table. Which tasks build is subject to per-cell variance (the t2 and t3
build sets share only stgit). Single-cell deltas within roughly ±5 points
should be treated as noise; the monotone stgit column (6.4 → 9.8 → 28.2) and
the arm means are the reproducible signals.

## 2. Task set

The 23 instances (ProgramBench IDs, dataset in the ProgramBench repo under
`src/programbench/data/tasks/`):

```
osgeo__gdal.0847f12 ip7z__7zip.839151e duckdb__duckdb.bdb65ec
jgm__pandoc.5caad90 ffmpeg__ffmpeg.360a402 samtools__samtools.aa823b5
tree-sitter__tree-sitter.5e23cca universal-ctags__ctags.243595e
danmar__cppcheck.0a5b103 ast-grep__ast-grep.dde0fe0 arq5x__bedtools2.dd57059
dandavison__delta.acd758f doxygen__doxygen.966d98e
jesseduffield__lazygit.1d0db51 tstack__lnav.ee34494 peco__peco.4e58dad
osgeo__proj.75d455c boyter__scc.515f91c paradigmxyz__solar.5190d0e
chirlu__sox.42b3557 stacked-git__stgit.430027d ivanceras__svgbob.6d00ad9
typst__typst.88356d0
```

The Factory article names 9 headline programs plus 14 others; the union is
these 23 unique tasks. Task images pull anonymously from Docker Hub:
`programbench/<id with __ -> _1776_>:task_cleanroom_v6`.

## 3. Model and serving

- Model: `Qwen/Qwen3.8-27B:peft:262144` (the 256K-context serving variant)
  via the Tinker OpenAI-compatible endpoint
  `https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1`.
  The base `Qwen/Qwen3.8-27B` serves a 65,536-token window, which is
  insufficient (prompt + max_tokens must fit; agents exceed it on these
  repos).
- Sampling: temperature 1.0, top_p 0.95, max_tokens 32768 (Qwen3.8 model
  card; matches every other 27B experiment in this repo).
- Env profile (render to `.env.tinker27`; template at
  `runs/factory23_env.template`):

```
OPENAI_BASE_URL=https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1
OPENAI_API_KEY=<TINKER_API_KEY>
AZURE_OPENAI_BASE_URL=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT=Qwen/Qwen3.8-27B:peft:262144
COOPER_TEMPERATURE_FORCE=1.0
COOPER_TOP_P=0.95
COOPER_MAX_TOKENS=32768
COOPER_ENV_FILE=.env.tinker27
COOPER_HARD_TIMEOUT_S=1200
COOPER_LLM_TIMEOUT_S=1100
COOPER_HEARTBEAT_DIR=/tmp/cooper_hb
COOPER_CONN_CLOSE=1
MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=25
```

Every line matters:

- The empty `AZURE_OPENAI_API_KEY=` blocks litellm's dotenv autoload from
  filling a stale Azure key that the worker would prefer over
  `OPENAI_API_KEY` (observed failure: every call 401).
- `COOPER_HARD_TIMEOUT_S=1200`: Tinker per-stream generation under batch
  load produces completions past 600s (measured p99 LLM latency 325–381s,
  max 641s post-fix); a 600s cap kills healthy cells.
- Retry cap 25: the endpoint emits 429 "Too many in-flight requests" storms
  lasting up to ~20 minutes; the default cap of 10 (~7 min of backoff) dies
  inside a storm.
- Any client must send a browser-plausible or tool-style User-Agent;
  Python urllib's default UA is blocked by Cloudflare (HTTP 403 code 1010).
  The harness's openai/litellm clients pass unmodified.

Two harness fixes in `027abdf` are prerequisites, both in the vendored
worker: (a) an XML fallback parser that converts Qwen `<tool_call>` markup
in message content into litellm-shaped tool calls — Tinker's shim does no
server-side tool parsing, so without it every response is a format error;
(b) the context-overflow matcher accepts the phrasing "context window"
(Tinker's 400 detail) so emergency history truncation engages.

Serving capacity, measured: burst-clean to 32 concurrent short calls and
724/724 over 3 sustained minutes at 24-concurrent, BUT full-batch load with
40–60K-token prompts hits in-flight caps. The proven operating point is
about 12 concurrent agent streams. All three arms respect it: 23 solo cells
at once (≈14 effective streams at ~0.6 duty), 6 t2 cells, 4 t3 cells.

## 4. Compute

- Driver box: any Linux host with this repo, docker, and the repo venv.
- Fleet: 6× EC2 m6i.4xlarge (16 vCPU) running docker, with `~/CooperAgents`
  (this repo, synced) and `~/ProgramBench` (the ProgramBench repo, for
  `uv run programbench eval`). Node IPs live in `scripts/fleet/nodes.txt`;
  dispatch is `scripts/fleet/pbrun.sh <ip> <instance> <arm> <rep> [flags]`
  over SSH (`~/.ssh/fleet_key`), collection is `scripts/fleet/collect.sh`.
- Pre-pull the 23 task images across nodes (4 per node) before dispatching;
  a cold gdal-class pull otherwise eats into the cell's wall-clock.

## 5. Arm configurations

Common: `--step-limit 1000 --agent-time-limit 14400` (1000 steps / 4h per
agent).

- **solo** (leaderboard convention, matches ProgramBench's published
  "no harness tuning" protocol): plain mini-swe, NO `--repair`.
  `pbrun.sh <ip> <inst> solo <rep> --step-limit 1000 --agent-time-limit 14400`
- **t2 / t3** (`coopgitc2-fin-min`): add
  `--repair --completion-gate --env-brief --presub-merge --team-size {2|3}`.

Batch drivers (score + validate each landing, throttle concurrency, announce
completion): `runs/factory23_shepherd.sh` (solo, all 23 at once),
`runs/factory23_t2_shepherd.sh` (6 concurrent), `runs/factory23_t3_shepherd.sh`
(4 concurrent). Launch with `nohup bash runs/<driver>.sh >> <log> &`. Exactly
one driver instance may run at a time — kill every PID from
`pgrep -f "<name>.s[h]"` before relaunching, and never `git stash` untracked
files while a driver runs (the `runs/.disp_*` markers and `.val` markers are
live state; losing them causes re-dispatch races).

## 6. Scoring and validation

- Each node's job runs `uv run programbench eval <run_dir>` inline; the
  per-instance `<id>.eval.json` holds `test_results` with statuses. Score =
  passed / total; "built" = any status other than `not_run` present.
- `scripts/fleet/validate_run.py <run_dir>` must PASS for a cell to count.
  Invalidation causes observed in this series, all infrastructure: stalled
  or starved LLM calls, terminal 429/timeout errors. An invalidated cell is
  deleted (locally and on its node, including `runs/.disp_<rep>`) and the
  driver redispatches it fresh; retries reproduced original results in every
  checked case.
- An eval line `ERROR: RuntimeError` (e.g. a containerd `docker commit`
  race) is an eval failure and never a real 0: rerun
  `uv run programbench eval <run_dir>` on the node and re-collect. stgit-t3's
  28.2% was initially masked by exactly this.

## 7. Expected effort and cost

Token usage (measured, Tinker 256K rates $2.48/M prefill, $7.46/M sample):
solo 82M+24M ≈ $386; t2 183M+60M ≈ $899; t3 324M+108M ≈ $1,606. Wall-clock
per arm ≈ 8h / 22h / 31h at the concurrency caps above. EC2 ≈ $320 total for
the series. First-time integration against a new serving endpoint should
budget extra for false starts (ours: ≈ $300 across auth/context/tool-parsing
discoveries, all now fixed in-tree).

## 8. Step-by-step

```bash
# 0. Prerequisites: repo at d93e65a+, fleet nodes up, ProgramBench checkout
#    on nodes, TINKER_API_KEY in hand.
# 1. Render .env.tinker27 from runs/factory23_env.template (fill key),
#    keep the defensive empties and timeout/retry lines exactly as above.
# 2. Refresh scripts/fleet/nodes.txt with node IPs; sync:
for ip in $(cat scripts/fleet/nodes.txt); do
  rsync -az -e "ssh -i ~/.ssh/fleet_key" src scripts .env.tinker27 ubuntu@$ip:CooperAgents/
done
# 3. Smoke (mandatory before each arm's batch; costs cents):
#    a 6-step solo cell and a 6-step t2 cell on one node; confirm zero
#    "No tool calls found" in the trajectories.
# 4. Launch the arm's shepherd; watch its log for CELL VALID/INVALID lines.
# 5. On INVALID: purge that cell everywhere + its .disp marker; the shepherd
#    redispatches. On eval RuntimeError: re-run programbench eval, recollect.
# 6. After BATCH COMPLETE: stop the nodes, recompute the table:
#    for each run dir, parse */*.eval.json (passed/total, built).
```

## 9. Provenance of the numbers in Section 1

Run directories (committed metrics/logs; artifacts pruned):
`runs/pb-solo-<task>-f23solo*`, `runs/pb-coopgitc2-<task>-f23t2`,
`runs/pb-coopgitc2-<task>-f23t3`. Detailed findings and incident log:
`docs/SEAM_BACKLOG.md` entries dated 2026-08-30 through 2026-09-02.
