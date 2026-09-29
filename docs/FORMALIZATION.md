# cooperagents in the Scaling-Agent-Systems formalism

This document describes the cooperagents system in the formal framework of
"Towards a Science of Scaling Agent Systems" (arXiv:2512.08296): the
`S=(A,E,C,Ω)` system tuple and the coordination-metric regression. It records
how each element of that framework maps onto cooperagents, where cooperagents is
a strict extension the framework cannot express, and how each coordination
metric is (or would be) computed from cooperagents' instrumentation.

Summary: cooperagents is expressible as `S = ({Sᵢ}, {Eᵢ}+reconcile, C_mode, Ω_merge)`
— a Hybrid-MAS with a code-integration `Ω` and dynamic `n`, a strict superset of
the paper's five topologies. Four of the five coordination metrics are already
derivable from `RunResult` + evaluator output; two (`c`, `R`) need small
instrumentation additions.

## 1. The paper's formalism (as used here)

System tuple (paper §3.1): `S=(A,E,C,Ω)` — agent set `A={a₁…aₙ}`, shared
environment `E`, communication topology `C`, orchestration policy `Ω`. `|A|=1`
is a Single-Agent System (SAS); `|A|>1` a Multi-Agent System (MAS).

Per-agent tuple: `Sᵢ=(Φᵢ, 𝒜ᵢ, Mᵢ, πᵢ)` — reasoning policy `Φ` (an LLM), action
space `𝒜ᵢ={ToolCall(t,θ)}`, memory `Mᵢ`, decision function `πᵢ:ℋ→𝒜ᵢ`. Loop:
`αᵢ,ₜ=πᵢ(hᵢ,ₜ)`, `oᵢ,ₜ=E(αᵢ,ₜ)`, `hᵢ,ₜ₊₁=fᵢ(hᵢ,ₜ,αᵢ,ₜ,oᵢ,ₜ)`.

Topologies and complexity (`k`=per-agent iterations, `n`=agents, `r`=orchestrator
rounds, `d`=debate rounds, `p`=peer rounds):

| Topology | `C` | `Ω` | Complexity |
| --- | --- | --- | --- |
| SAS | `∅` | n/a | `O(k)` |
| Independent | `∅` | synthesis_only | `O(nk)+O(1)` |
| Centralized | `{(a_orch,aᵢ):∀i}` | hierarchical | `O(rnk)+O(r)` |
| Decentralized | `{(aᵢ,aⱼ):∀i≠j}` | consensus | `O(dnk)+O(1)` |
| Hybrid | star ∪ peer | hierarchical+lateral | `O(rnk)+O(r)+O(p)` |

`Ω` decides: (i) how sub-agent outputs are aggregated, (ii) whether the
orchestrator can override sub-agents, (iii) memory persistence across rounds,
(iv) termination conditions.

Coordination metrics (`T` = total reasoning turns; `S` = success rate):

- Coordination efficiency `E_c = S / (T / T_SAS)`.
- Coordination overhead `O% = (T_MAS − T_SAS) / T_SAS × 100`.
- Error amplification `A_e = E_MAS / E_SAS` (factual error-rate ratio; >1 = error propagation, <1 = correction).
- Message density `c` = inter-agent messages per reasoning turn.
- Redundancy `R` = mean pairwise cosine similarity of agent output embeddings (paper reports ~0.41–0.50 for MAS, 0.00 for SAS).

Equation 1 regresses performance `P` on 10 main effects — `I`, `I²`,
`log(1+T_tools)` (tool count), `log(1+n_a)`, `log(1+O%)`, `c`, `R`, `E_c`,
`log(1+A_e)`, `P_SA` — plus 9 interaction terms; cross-validated `R²=0.513`. All
configurations compute-matched (~4,800 tokens/trial).

## 2. cooperagents instantiation

`A` — agent set. `Agent` loops built by `UnifiedHarness._build_assignments`, with
three provenance classes: seed agents (`assignments`, or one `objective` fanned
to `team_size` as lead+members) = the paper's fixed `n`; spawned helpers created
at runtime (`spawn_helper → supervise()`, capped at `max_agents`) = dynamic `n`;
and integrator/reviewer/repair agents added at the seam (no analogue in the
paper's `A`).

`Sᵢ` — per-agent tuple. `Φ` = model or vendored mini-swe worker (held constant by
design); `𝒜ᵢ` = `{bash, read_file, write_file}` ∪ coordination verbs
`{send_message, broadcast, task_*}` ∪ `{finish}` (+`spawn_helper`), or a `ToolSet`
on the state substrate; `Mᵢ` = transcript with a bounded `_context()` view
(head + recent, truncated observations); `πᵢ` = the `run()` loop.

`E` — environment. DEPARTURE: not a single shared world. Each agent gets its own
`Eᵢ` via `env_factory(agent_id)` (hard constraint: never share a live
workspace). Backends: git tree (`DiffArtifact`) or `StateEnv` (`StateArtifact`).
The loop holds per-agent over `Eᵢ`; cross-agent visibility is reconstructed post
hoc (diff-seeding, teammate-poller, git-share). Faithfully, `E` is a family
`{Eᵢ}` plus a reconciliation operator.

`C` — topology. Realized by the `TeamBus` (messaging inboxes, claimable task
board, spawn queue) plus seeding/poller wiring: `send_message` = directed edge,
`broadcast` = all-to-all, `spawn_helper` = dynamic star recruit, task board =
blackboard, coordinator = star monitor nudges. Mode-dependent (see §3).

`Ω` — orchestration. DEPARTURE: a code-merge/selection/repair operator over
per-agent trees, not vote/synthesis over text. (i) Aggregation = sequential
seed-handoff, `git 3-way merge` (apply-chain `.rej` fallback), best-of-N
selection, or state reducers (`best_of_first_nonempty` / `lead_synthesis`).
(ii) Override = lead/integrator tree wins; `do_no_harm` discards a delta that
broke a healthy tree. (iii) Memory = `seed_prior` carries cumulative diffs;
`preserve_invariants` persists checks. (iv) Termination = step/cost/time limits,
finish-veto, `completion_gate`, `behavioral_gate` — mechanical health gates, not
consensus. This `Ω` can fail (merge conflict) and invoke further agents
(repair); the paper's aggregation cannot fail.

## 3. Topology classification

| cooperagents mode | Paper topology |
| --- | --- |
| `team_size=1` | SAS |
| concurrent no-comms + end-combine / `best_of_first_nonempty` | Independent |
| spawn/supervise star; team-roles lead-merges | Centralized (star built dynamically) |
| `coop_tools` peer mesh + teammate-poller | Decentralized (but `Ω`=merge, not debate) |
| coordinator + task_board + git_share + coop_tools | Hybrid |

Strict extensions with no clean label: sequential seed-handoff (a temporal chain
edge, not a message edge); `_run_decomposed` (a dependency DAG whose edges are
`SubTask.depends_on` with write-set ownership — the work is re-partitioned);
`_run_adaptive` (`C` chosen at runtime from the merge-conflict signal, whereas
the paper fixes `C` ex-ante).

## 4. Complexity (`k` = step budget, default 40; paper SAS k=10)

- Solo (SAS): `O(k)`.
- Sequential seed team: `O(N·k)` total, serialized wall-clock, + per-agent seed apply+commit.
- Concurrent `coop_tools` / decomposed-parallel: `O(k)` wall-clock, `O(N·k)` tokens, + merge `O(N·tree)` + optional repair `O(k_repair)` (default 25).
- Adaptive: `O(k)` probe; on conflict falls back to `O((N−1)·k)`.
- Decomposed DAG: `O(depth·k)` wall-clock + integrator `O(k)`.
- Best-of-N: `O(B·N·k)` total.

The integration/repair/gate term (`O(N·tree)` merge + `O(k_repair)` repair +
`O(tree)` health check) is the cost of reconciling isolated `Eᵢ` that the paper's
shared-`E` forms omit. There is no `r` or `d` (no fixed round/debate loop).

## 5. Coordination metrics

| Metric | Paper definition | cooperagents instantiation | Status |
| --- | --- | --- | --- |
| `E_c` | `S/(T/T_SAS)` | `S` = evaluator success; `T` = team/solo `RunResult.total_steps` | derivable |
| `O%` | `(T_MAS−T_SAS)/T_SAS·100` | `team.total_steps / solo.total_steps − 1` | derivable |
| `A_e` | `E_MAS/E_SAS` | team vs solo per-feature error rate from the evaluator (`do_no_harm_discards` is a related internal signal, not this) | derivable |
| `c` | inter-agent msgs / turn | `len(bus.message_log()) / total_steps` — `message_log()` exists but `coordination_metrics` only consumes `task_events()` today | needs wiring |
| `R` | mean pairwise cosine of output embeddings | cosine over `seeds[*].patch` / `.artifact.text()`; not computed. Proxy: diff-overlap from `_threeway_merge` / COLLISION dirty-set | needs instrumentation |

## 6. Equation-1 predictor vector for a cooperagents team config

For a typical team config (shared mode, `n=3` seeds, one worker + model,
compute-matched):

- Fixed by the agent-held-constant design: `I`, `I²` (one model), `log(1+T_tools)` (catalog ≈ 9–12 tools). These are constants in cooperagents runs, so cooperagents cannot populate Eq.1's capability axis without deliberately varying the model — which conflicts with its own held-constant invariant.
- Measured per run: `n_a` (dynamic via `spawn_metrics.granted`), `O%`, `c`, `E_c`, `A_e` (from the two arms), `P_SA` (solo arm).

cooperagents does not run the Eq.1 regression; it emits the `RunResult` and bus
audit fields from which the predictors are computed.

## 7. Where cooperagents exceeds the framework

1. Isolated per-agent environments `Eᵢ` with post-hoc reconciliation, instead of a single shared `E`.
2. A code-merge/repair `Ω` that can fail and recurse into further agents.
3. Dynamic `n` via spawn (no "agent creates agent" operator in the paper).
4. The team↔agent co-design seam (`TeamSpec` flags that reach into `πᵢ`/prompts); the paper treats each `Sᵢ` as a black box with fixed `πᵢ`.
5. Runtime topology selection (`adaptive`) and work re-partitioning (`decompose`); the paper fixes `C` over a fixed agent set.

## 8. Caveats

- The paper writes coordination efficiency as `E_c` (not `Ecc`).
- The complexity forms carry an additive orchestration term: SAS `O(k)`; Independent `O(nk)+O(1)`; Centralized `O(rnk)+O(r)`; Decentralized `O(dnk)+O(1)`; Hybrid `O(rnk)+O(r)+O(p)`.
- The numeric Appendix-D.2 architecture params (SAS k=10; MAS n=3, r≤5, d=3, k=3) were NOT verified from the HTML source and should be re-checked against the PDF before relying on them. cooperagents' own defaults: `step_limit=40`, `team_size≈3`, `repair_step_limit=25`; it has no `r`/`d` counterpart.
- The exact 9 interaction-term pairings were reconstructed by a fetch summarizer and are not confirmed verbatim; the count (9), the fit (`R²_CV=0.513`), and the 10 main effects are reliable.
- Eq.1 overloads `T`: in the metrics `T` = total reasoning turns, but in Eq.1 `log(1+T)` is tool count.
- `n` is not fixed when spawn is enabled, so `log(1+n_a)` becomes a measured runtime quantity, not a design constant.
