# NLP CPU execution

This path runs the existing two-worker team in independent, writable Apptainer
sandboxes. Messaging uses the existing in-memory bus; Git sharing uses one
job-owned directory. The coordinator and completion gate are enabled. Repair,
presub-merge, helper spawning and longest-patch selection are disabled.

The dummy smoke runs a loopback-only OpenAI-compatible HTTP server with fixed
responses. It uses the real worker and coordinator clients, deliberately fails
then passes the completion gate, collects and merges both worker changes, invokes
the pinned official evaluator, and separately checks both official gold patches.
Dummy benchmark scores do not measure model quality.

## Prepared runtime

Cluster root: `/nlp/scr/chency/projects/cooperagents`.
CooperBench source: `runtime/cooperbench-63b9d44`, pinned to
`63b9d44d9f39a02fccf5bf0052db48a917a011fd`. Apply
`patches/cooperbench-backend.patch` with `patch -p1` in that checkout. This adds
backend-object injection only; official merge, tests and scoring remain upstream.
The evaluator's import dependencies are installed by `job.sh` on a compute node.

`runtime/images.json` maps public task image names to prepared SIF paths. Image
conversion must run inside a CPU allocation, with Apptainer cache and temporary
paths on scratch. To validate all dataset files without building images:

```bash
PYTHONPATH=src COOPERBENCH_DIR=/path/to/cooperbench \
  python scripts/nlp_cluster/prepare.py --check-only --images unused --manifest unused
```

To prepare a selected subset on a compute node, omit `--check-only` and supply
`--subset lite` (or another official subset), `--images` and `--manifest`.
Preparation records SIF SHA256 values. Only prepared and tested images are
qualified; a successful Go task smoke does not establish other toolchains.

## Submit

Commit runtime changes first. From this repository on the Mac:

```bash
python3 scripts/nlp_cluster/submit.py
```

This submits a 4 CPU / 16 GB / 30 minute `john` job, using committed source only.
It creates `code/cooperagents-<run-id>` and `runs/<run-id>`, containing metadata,
variant, submission resources, exact arguments, dependency versions, evaluator
patch, image manifest, logs and status. No credentials are archived.

After the dummy smoke passes and an API key is available, place the exported
`OPENAI_API_KEY` in a private file on the cluster (outside the source/run paths):

```bash
python3 scripts/nlp_cluster/submit.py --mode real --env-file /private/path/model.env
```

The real path loads `configs/qwen35-9b-openrouter.env.example`. Provider acceptance
of reasoning/sampling settings, tool behavior, token limits and model performance
still require a real API smoke. Dummy transport verifies outgoing parameters only.
Use `--pairs repo:task:f1,f2 ...` for explicit real-run pairs; prepare their images
first. The fixed dummy scenario is `go_chi_task:27:3,4`.
