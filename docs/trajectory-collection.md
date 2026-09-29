# Collecting replayable team trajectories

Use `scripts/nlp_cluster/submit.py --collect-trajectories` with the normal real-run
arguments. This enables `--record-trajectory --skip-eval`: worker completion checks
and health-gated repair still run; official CooperBench scoring does not.
The collection mode supports one cooperative mini-SWE team per pair, including
its sequential repair integrators. It does not support best-of-N or decomposition.

Each pair directory contains `trajectory.jsonl.gz`, an append-only, gzip-compressed
journal. Events have a schema version, pair-local sequence number, UTC timestamp,
actor, event type, and payload. A lock orders writes from concurrent actors.
Writes are flushed after every event; truncated or incomplete journals fail the
auditor. Existing journals are never overwritten.

The journal includes:

- Initial tasks, every appended conversation message, and replacement contexts after
  both model-based summarization and emergency truncation.
- Exact SDK-level model arguments (messages, tools, model and sampling), raw SDK
  responses, and failed call attempts. Summarizer calls use the same recorder.
- Worker shell requests and raw environment outputs, messaging/tool outputs,
  and final status/step counts, including `integrator1` and `integrator2` when used.
- Coordinator registration, task/action/budget observations and inspected dirty-file
  outputs, JSON decisions and rejection reasons, model requests/responses, replies,
  queued messages and delivery, worker completion and dropped late messages.
- Notebook versions and full content in `notebook_update`, and short path reminders
  in `notebook_delivery`. These reminders are separate from model-generated `nudge`
  events and do not count as model interventions.

The mounted notebook stays in `coordination/<run_id>/notebook.md` under the pair
directory even when trajectory collection is disabled. `--no-coordinator-notebook`
on the benchmark runner disables both the artifact and its worker channel.
To establish that a worker read an update, inspect its shell result and subsequent
SDK request for the file header/version and content. A reminder or the harness's
initial mount check is not evidence that the worker read or followed the agreement.

Transport credentials and known secret environment values are redacted. This is
an agent-I/O journal, not a packet capture: SDK-internal HTTP retries and provider
internals are not recorded. It cannot reconstruct container state, execute a new
counterfactual action, or reproduce an unobserved response from a killed request.
A provider call still pending at shutdown is reported as incomplete.

## Replay without containers or inference

From the repository (or its saved source snapshot):

```bash
PYTHONPATH=src python -m cooperagents.trajectory /path/to/trajectory.jsonl.gz \
  --at-seq 120 --actor agent1 > replay.json

PYTHONPATH=src python -m cooperagents.trajectory /path/to/trajectory.jsonl.gz \
  --at-time '2026-09-27T03:00:00+00:00' --actor coordinator > coordinator.json
```

Replay restores the conversation visible at that event boundary, each actor's
last event and request/completed call, and outstanding calls. The original journal
retains all earlier events, including messages removed from the active context.
Use `gzip -dc /path/to/trajectory.jsonl.gz` to inspect the full event stream.
Sequence numbers order recording; they do not imply one simultaneous worker's
operation caused another's. Time selection uses the recorded UTC timestamps.

After generation, the cluster launcher runs:

```bash
PYTHONPATH=src python scripts/audit_trajectories.py /path/to/run
```

The auditor requires every pair in `metadata.json`, both workers, every exported
repair agent, matching request/response IDs, matching coordinator nudge counts,
and exact replay of each exported final message history. It writes
`trajectory-audit.json` with per-pair counts, statuses, byte sizes and SHA256.
Worker limits or errors are retained as data and reported separately from journal
integrity; a complete journal does not imply a successful feature implementation.
