"""No-model direct capture regression on a real historical repair environment.

The live source is reconstructed with the legacy native reader. This checks new
capture/restore behavior, not equivalence to the unavailable historical process.
"""

import argparse
import json
import time
from pathlib import Path

import probe_official_repair
from probe_official_repair import COMMAND, execute

from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.env.image_base import ImageBase
from cooperagents.env.image_channel import ImageChannel
from cooperagents.published import captured_publication
from cooperagents.repair import load_repair_input

REPAIR = COMMAND.replace(
    "python3 -m compileall -q .",
    "python3 -m pip install --no-index --no-deps --force-reinstall /tmp/v3-repair-wheels/*.whl\n"
    "python3 -m pytest -q tests/test_api.py\npython3 -m compileall -q .",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    print(json.dumps({"stage": "native_prepare"}), flush=True)
    env = ApptainerEnv.from_checkpoint(args.checkpoint, scratch=args.scratch)
    try:
        inputs = load_repair_input(args.checkpoint)
        from cooperagents.bus.memory import InMemoryBus
        from cooperagents.completion import CompletionBinding, CompletionSettings
        from cooperagents.harness import run_repair_tail
        from cooperagents.workers.mini_swe_worker import BusComm, run_mini_swe_agent

        download = env.execute(
            "python3 -m pip download --only-binary=:all: --no-deps -d /tmp/v3-repair-wheels "
            '"MarkupSafe==$(python3 -c \'import importlib.metadata; print(importlib.metadata.version("MarkupSafe"))\')"',
            timeout=180,
        )
        (args.output / "dependency-download.json").write_text(json.dumps(dict(stdout=download.stdout, exit_code=download.exit_code)))
        if download.exit_code:
            raise RuntimeError("could not stage exact offline dependency wheel")
        spec = inputs.settings.team_spec("direct-capture", str(inputs.model_config_data["model_name"]))
        spec.repair_step_limit = int(inputs.agent_config["step_limit"])
        spec.repair_time_limit = inputs.time_limit_s
        spec.completion_gate = inputs.gate.build() if inputs.gate else None
        bus = InMemoryBus("direct-capture")
        bus.restore_inboxes({actor: [m.model_dump(by_alias=True) for m in rows] for actor, rows in inputs.pending_messages.items()})

        def forbidden(request):
            raise AssertionError("capture must stop before model completion")

        settings = CompletionSettings(model="offline-qualification", revision="no-model", generation={"max_tokens": 4096}, timeout=30.0)

        def run_agent(actor, task, on_start):
            return run_mini_swe_agent(
                env,
                task=task,
                agent_id=actor,
                role="integrator",
                model_name=spec.model,
                step_limit=spec.repair_step_limit,
                cost_limit=float(inputs.agent_config["cost_limit"]),
                command_timeout=inputs.command_timeout,
                guard_git=inputs.guard_git,
                comm=BusComm(bus, actor),
                tool_protocol=spec.tool_protocol,
                wait_protocol=spec.wait_protocol,
                git_share=spec.git_share,
                completion_gate=spec.completion_gate,
                time_limit_s=inputs.time_limit_s,
                repair_input=inputs,
                completion=CompletionBinding(forbidden, settings, forbidden, settings),
                on_start=on_start,
            )

        start = time.monotonic()
        destination = args.output / "before-integrator1"
        print(json.dumps({"stage": "direct_capture_harness"}), flush=True)
        run_repair_tail(
            env,
            spec=spec,
            assignments=spec.assignments,
            seeds={},
            run_agent=run_agent,
            check_gate=lambda: False,
            gate_checks=[],
            checkpoint_dir=args.output,
            checkpoint_bus=bus,
            stop_after_repair_checkpoint=True,
            first_task=inputs.original_task,
        )
        print(json.dumps({"stage": "capture_complete"}), flush=True)
        elapsed = time.monotonic() - start
        reference = captured_publication(destination)
        (args.output / "capture.json").write_text(json.dumps({"capture_seconds": elapsed, "publication": reference.model_dump()}, indent=2))
        with ImageBase.prepare(env.image, reference.image_sha256, args.scratch) as base:
            with ImageChannel(base.descriptor, base.proof, Path(__import__("tempfile").mkdtemp(prefix="v3-channel-"))) as channel:
                import os

                os.environ["COOPER_REPAIR_BASE_CHANNEL"] = str(channel.path)
                probe_official_repair.COMMAND = REPAIR
                execute(reference, args.scratch, args.output / "official.json")
        result = env.execute(REPAIR, timeout=180)
        (args.output / "live-command.json").write_text(json.dumps({"stdout": result.stdout, "exit_code": result.exit_code}, indent=2))
        if result.exit_code:
            raise RuntimeError("live repair failed")
        from cooperagents.checkpoint import preview_patch
        from cooperagents.patching import strip_test_sections

        live_patch = strip_test_sections(preview_patch(env))
        (args.output / "live.patch").write_text(live_patch)
        if live_patch != (args.output / "official.patch").read_text():
            raise RuntimeError("live and restored exported patches differ")
        fresh = ApptainerEnv(str(env.image), scratch=str(args.scratch))
        try:
            fresh.write_file("/tmp/repair.patch", live_patch)
            checked = fresh.execute(
                "git apply --check /tmp/repair.patch && git apply /tmp/repair.patch && python3 -m pytest -q tests/test_api.py", timeout=180
            )
            (args.output / "independent.json").write_text(json.dumps(dict(stdout=checked.stdout, exit_code=checked.exit_code)))
            if checked.exit_code:
                raise RuntimeError("patch failed independent base application/test")
        finally:
            fresh.cleanup()
    finally:
        env.cleanup()


if __name__ == "__main__":
    main()
