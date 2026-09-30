"""Repair-only replay from a verified first-integrator filesystem checkpoint."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from cooperagents.bus.base import TeamBus
from cooperagents.checkpoint import complete_output, sha256, verify_checkpoint
from cooperagents.completion import CompletionBinding, safe_generation
from cooperagents.env.base import Environment, ExecResult
from cooperagents.types import AgentResult, Assignment, RunResult, TeamSpec

if TYPE_CHECKING:
    from cooperagents.trajectory import Trajectory
    from cooperagents.vendor.mini_swe.agents.default import DefaultAgent


class RepairInfrastructureError(RuntimeError):
    """Replay failed to produce a valid repair episode; this is not a zero reward."""


class GateDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["cooperagents.verification.validate"]
    merged: Literal[False] = False
    build_artifact: None = None
    max_rejections: Literal[3] = 3

    def build(self) -> Callable[[Environment], str | None]:
        from cooperagents.verification import validate

        return partial(validate, merged=False, build_artifact=None)


def describe_gate(gate: Callable[[Environment], str | None] | None) -> dict[str, JsonValue] | None:
    from cooperagents.verification import validate

    if gate is None:
        return None
    function = gate.func if isinstance(gate, partial) else gate
    kwargs = gate.keywords if isinstance(gate, partial) else {}
    args = gate.args if isinstance(gate, partial) else ()
    if function is not validate or args or set(kwargs) - {"merged", "build_artifact"}:
        raise ValueError("Repair capture supports only the standard verification.validate gate")
    if kwargs.get("merged", False) is not False or kwargs.get("build_artifact") is not None:
        raise ValueError("Repair capture requires merged=False and build_artifact=None")
    return GateDescriptor(kind="cooperagents.verification.validate").model_dump()


def validate_capture_spec(spec: TeamSpec, bus: TeamBus | None = None) -> None:
    if (
        not spec.shared_workspace
        or not spec.coop_tools
        or spec.seed_prior
        or spec.worker != "mini_swe"
        or spec.best_of_n != 1
        or spec.decompose
        or spec.adaptive
        or spec.team_roles
        or spec.verify_fix
        or spec.select_integration is not None
        or spec.allow_spawn_tool
        or spec.completeness_review
        or spec.task_board
        or spec.claim_mode
        or bus is not None
    ):
        raise ValueError("Repair capture requires a mini_swe coop-tools mechanical merge without unsaved host state")
    if spec.repair_attempts not in (1, 2):
        raise ValueError("Repair capture supports one or two attempts")
    describe_gate(spec.completion_gate)


def prefix_task(spec: TeamSpec, task: str) -> str:
    from cooperagents.harness import _MINE_CONVENTIONS, _SPEC_FIDELITY, _TDD_PREAMBLE

    for enabled, prefix in (
        (spec.spec_fidelity, _SPEC_FIDELITY),
        (spec.tdd_preamble, _TDD_PREAMBLE),
        (spec.mine_conventions, _MINE_CONVENTIONS),
    ):
        if enabled:
            task = prefix + task
    return task


class RepairSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    task_id: int
    features: list[int]
    assignments: list[Assignment]
    attempts: Literal[1, 2]
    focused: bool
    behavioral_gate: bool
    spec_fidelity: bool
    tdd_preamble: bool
    mine_conventions: bool
    tool_protocol: bool
    wait_protocol: bool
    git_share: bool

    def team_spec(self, run_id: str, model: str) -> TeamSpec:
        return TeamSpec(
            run_id=run_id,
            repo=self.repo,
            task_id=self.task_id,
            features=self.features,
            assignments=self.assignments,
            model=model,
            worker="mini_swe",
            shared_workspace=True,
            coop_tools=True,
            seed_prior=False,
            repair_integrator=True,
            repair_attempts=self.attempts,
            focused_repair=self.focused,
            behavioral_gate=self.behavioral_gate,
            spec_fidelity=self.spec_fidelity,
            tdd_preamble=self.tdd_preamble,
            mine_conventions=self.mine_conventions,
            tool_protocol=self.tool_protocol,
            wait_protocol=self.wait_protocol,
            git_share=self.git_share,
        )


class InitialMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user"]
    content: str


class RepairInput(BaseModel):
    """Effective inputs, before the first model request. No historical completion is stored."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    original_task: str
    effective_task: str
    initial_messages: list[InitialMessage]
    agent_config: dict[str, JsonValue]
    model_config_data: dict[str, JsonValue]
    tools: list[dict[str, JsonValue]]
    command_timeout: int = Field(gt=0)
    guard_git: bool
    time_limit_s: int | None = Field(default=None, gt=0)
    gate: GateDescriptor | None
    settings: RepairSettings
    source_hashes: dict[str, str]
    evidence_hashes: dict[str, str] = Field(default_factory=dict)
    legacy_event_sequences: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_effective_input(self) -> RepairInput:
        from cooperagents.vendor.mini_swe.agents.default import AgentConfig
        from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModelConfig
        from cooperagents.vendor.mini_swe.models.utils.actions_toolcall import BASH_TOOL, SEND_MESSAGE_TOOL

        if set(self.agent_config) != set(AgentConfig.model_fields):
            raise ValueError("Incomplete or unknown effective agent configuration")
        agent = AgentConfig.model_validate(self.agent_config, strict=True)
        if agent.wall_deadline is not None or agent.output_path is not None:
            raise ValueError("Repair input may not contain an old deadline or output path")
        if agent.step_limit <= 0 or not math.isfinite(agent.cost_limit) or agent.cost_limit < 0:
            raise ValueError("Invalid repair budget")
        if set(self.model_config_data) != set(LitellmModelConfig.model_fields):
            raise ValueError("Incomplete or unknown effective model configuration")
        model = LitellmModelConfig.model_validate(self.model_config_data, strict=True)
        if model.litellm_model_registry is not None or model.multimodal_regex or model.set_cache_control is not None:
            raise ValueError("Unsupported repair model formatting configuration")
        safe_generation(model.model_kwargs)
        if self.tools != [BASH_TOOL, SEND_MESSAGE_TOOL]:
            raise ValueError("Repair requires the exact bash/send_message tool schemas")
        if [m.role for m in self.initial_messages] != ["system", "user"]:
            raise ValueError("Repair requires the initial system/user pair")
        if not self.effective_task or self.initial_messages[1].content.count(self.effective_task) != 1:
            raise ValueError("Ambiguous repair task in initial user message")
        # The pinned template inserts task exactly once without transformation.
        if agent.instance_template.count("{{task}}") != 1 or "task" in agent.instance_template.replace("{{task}}", ""):
            raise ValueError("Unsupported task-dependent instance template")
        spec = self.settings.team_spec("validate", model.model_name)
        if prefix_task(spec, self.original_task) != self.effective_task:
            raise ValueError("Saved task prefixes differ from effective repair task")
        if not self.source_hashes or any(
            len(v) != 64 or any(c not in "0123456789abcdef" for c in v)
            for v in [*self.source_hashes.values(), *self.evidence_hashes.values()]
        ):
            raise ValueError("Repair input requires source/evidence SHA256 identities")
        return self

    def messages_for(self, task: str) -> list[dict[str, str]]:
        messages = [m.model_dump() for m in self.initial_messages]
        messages[1]["content"] = messages[1]["content"].replace(self.effective_task, task, 1)
        return messages


def capture_repair_input(
    agent: DefaultAgent,
    *,
    task: str,
    spec: TeamSpec,
    assignments: list[Assignment],
    command_timeout: int,
    guard_git: bool,
    time_limit_s: int | None,
) -> RepairInput:
    agent_config = agent.config.model_dump(mode="json")
    agent_config.update(wall_deadline=None, output_path=None)
    model_config = agent.model.config.model_dump(mode="json")
    model_config["model_kwargs"] = {
        k: v for k, v in model_config["model_kwargs"].items() if k not in {"api_base", "api_key", "extra_headers"}
    }
    model_config["litellm_model_registry"] = None
    root = Path(__file__).parent
    files = (
        "harness.py",
        "workers/mini_swe_worker.py",
        "vendor/mini_swe/config/solo.yaml",
        "vendor/mini_swe/agents/default.py",
        "vendor/mini_swe/models/litellm_model.py",
        "vendor/mini_swe/models/utils/actions_toolcall.py",
    )
    return RepairInput(
        version=1,
        original_task=task,
        effective_task=agent.extra_template_vars["task"],
        initial_messages=agent.model._prepare_messages_for_api(agent.messages),
        agent_config=agent_config,
        model_config_data=model_config,
        tools=agent.model._tools,
        command_timeout=command_timeout,
        guard_git=guard_git,
        time_limit_s=time_limit_s,
        gate=describe_gate(spec.completion_gate),
        settings=RepairSettings(
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            assignments=assignments,
            attempts=spec.repair_attempts,
            focused=spec.focused_repair,
            behavioral_gate=spec.behavioral_gate,
            spec_fidelity=spec.spec_fidelity,
            tdd_preamble=spec.tdd_preamble,
            mine_conventions=spec.mine_conventions,
            tool_protocol=spec.tool_protocol,
            wait_protocol=spec.wait_protocol,
            git_share=spec.git_share,
        ),
        source_hashes={name: sha256(root / name) for name in files},
    )


def load_repair_input(checkpoint: Path, sidecar: Path | None = None) -> RepairInput:
    state = verify_checkpoint(checkpoint)
    if state["metadata"].get("boundary") != "repair_agent_start" or state["metadata"].get("attempt") != 1:
        raise ValueError("Repair rollout requires before-integrator1")
    manifest = json.loads((checkpoint / "manifest.json").read_text())
    if "repair-input.json" in manifest["files"]:
        if sidecar is not None:
            raise ValueError("A new checkpoint must use its manifest-covered repair input")
        path = checkpoint / "repair-input.json"
    else:
        if sidecar is None:
            raise ValueError("Legacy checkpoint requires an explicitly validated import sidecar")
        path = sidecar
    result = RepairInput.model_validate_json(path.read_text())
    if result.original_task != state["metadata"]["task"]:
        raise ValueError("Repair input task differs from checkpoint")
    if sidecar is not None and (
        set(result.evidence_hashes) != {"checkpoint_manifest", "journal", "variant", "launch_args"}
        or set(result.legacy_event_sequences) != {"checkpoint_end", "agent_start", "context", "first_sdk_request"}
        or any(v <= 0 for v in result.legacy_event_sequences.values())
        or result.evidence_hashes.get("checkpoint_manifest") != sha256(checkpoint / "manifest.json")
    ):
        raise ValueError("Legacy repair input belongs to another checkpoint")
    return result


def run_repair_checkpoint(
    checkpoint: Path,
    *,
    repair_input: Path | None = None,
    scratch: Path,
    run_id: str,
    completion: CompletionBinding | None = None,
    trajectory: Trajectory | None = None,
) -> RunResult:
    """Restore one independent sandbox and run only new integrator attempts."""
    from cooperagents.bus.memory import InMemoryBus
    from cooperagents.env.apptainer import ApptainerEnv
    from cooperagents.harness import _tree_health, _tree_health_behavioral, run_repair_tail
    from cooperagents.patching import strip_test_sections
    from cooperagents.workers.mini_swe_worker import BusComm, run_mini_swe_agent

    inputs = load_repair_input(checkpoint, repair_input)
    spec = inputs.settings.team_spec(run_id, str(inputs.model_config_data["model_name"]))
    spec.repair_step_limit = int(inputs.agent_config["step_limit"])
    spec.repair_time_limit = inputs.time_limit_s
    bus = InMemoryBus(run_id)
    env = ApptainerEnv.from_checkpoint(checkpoint, scratch=scratch)
    seeds = {}
    checks = []
    started = time.monotonic()
    try:

        class GateEnv:
            def execute(self, command, *, timeout=60):
                result = env.execute(command, timeout=timeout)
                complete_output(result, "Post-repair health", allow_failure=True)
                checks.append(dict(command=command, timeout=timeout, stdout=result.stdout, exit_code=result.exit_code))
                return result

        gate = _tree_health_behavioral if spec.behavioral_gate else _tree_health

        def run_agent(actor, task, on_start):
            return run_mini_swe_agent(
                env,
                task=prefix_task(spec, task),
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
                completion_gate=inputs.gate.build() if inputs.gate else None,
                time_limit_s=inputs.time_limit_s,
                repair_input=inputs,
                completion=completion,
                trace=partial(trajectory.emit, actor) if trajectory else None,
            )

        attempts = run_repair_tail(
            env,
            spec=spec,
            assignments=spec.assignments,
            seeds=seeds,
            run_agent=run_agent,
            check_gate=lambda: gate(GateEnv()),
            gate_checks=checks,
            first_task=inputs.original_task,
        )
        raw_patch = env.git_diff()
        complete_output(ExecResult(raw_patch, 0), "Export repair patch")
        integrated = AgentResult(
            agent_id="team",
            role="integrated",
            status="submitted",
            patch=strip_test_sections(raw_patch),
            cost=sum(r.cost for r in seeds.values()),
            steps=sum(r.steps for r in seeds.values()),
        )
        return RunResult(
            run_id=run_id,
            repo=spec.repo,
            task_id=spec.task_id,
            features=sorted(spec.features),
            seeds=seeds,
            integrated=integrated,
            duration_seconds=time.monotonic() - started,
            metrics=dict(
                repair_attempts=attempts, repair_gate_checks=checks, checkpoint_manifest_sha256=sha256(checkpoint / "manifest.json")
            ),
        )
    finally:
        env.cleanup()


def import_legacy_repair_input(
    checkpoint: Path, *, journal: Path, source: Path, variant: Path, launch_args: Path, destination: Path
) -> RepairInput:
    """Import evidenced v1 inputs into a new run; never rewrite the original capture.

    Only the known collection source is supported. Its startup messages and SDK
    request prove the effective prompt/tools/sampling; pinned source proves the
    templates and defaults that the journal did not record.
    """
    import ast
    import shlex
    import subprocess
    import tomllib

    import yaml
    from jinja2 import StrictUndefined, Template

    from cooperagents.trajectory import open_events, replay
    from cooperagents.vendor.mini_swe.agents.default import AgentConfig
    from cooperagents.vendor.mini_swe.models.litellm_model import LitellmModelConfig
    from cooperagents.workers import mini_swe_worker as worker

    state = verify_checkpoint(checkpoint)
    manifest = json.loads((checkpoint / "manifest.json").read_text())
    if "repair-input.json" in manifest["files"]:
        raise ValueError("New checkpoints do not need legacy import")
    if state["metadata"].get("boundary") != "repair_agent_start" or state["metadata"].get("attempt") != 1:
        raise ValueError("Legacy repair import requires before-integrator1")
    if checkpoint.resolve() == destination.resolve().parent or destination.resolve().is_relative_to(checkpoint.resolve()):
        raise ValueError("Legacy input must be written to a new run, outside the checkpoint")
    # Known source identity, rather than importing/executing arbitrary old code.
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != "514ed98a59c611ee5a38027c8459d6fcbcec92b8":
        raise ValueError("Unsupported legacy collection source commit")
    files = (
        "src/cooperagents/harness.py",
        "src/cooperagents/workers/mini_swe_worker.py",
        "src/cooperagents/vendor/mini_swe/config/solo.yaml",
        "src/cooperagents/vendor/mini_swe/agents/default.py",
        "src/cooperagents/vendor/mini_swe/models/litellm_model.py",
        "src/cooperagents/vendor/mini_swe/models/utils/actions_toolcall.py",
        "scripts/bench_compare.py",
        "scripts/nlp_cluster/job.sh",
        "scripts/nlp_cluster/submit.py",
    )
    hashes = {}
    for name in files:
        path = source / name
        expected = subprocess.check_output(["git", "-C", str(source), "show", f"{commit}:{name}"])
        if path.read_bytes() != expected:
            raise ValueError(f"Legacy source was modified: {name}")
        hashes[name] = sha256(path)

    # Agent defaults must remain identical to the pinned source, independently
    # of changes to the startup/replay method surrounding the config class.
    def agent_config_ast(path):
        node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "AgentConfig")
        return ast.dump(node, include_attributes=False)

    old_agent_path = source / "src/cooperagents/vendor/mini_swe/agents/default.py"
    if agent_config_ast(old_agent_path) != agent_config_ast(Path(worker.__file__).parents[1] / "vendor/mini_swe/agents/default.py"):
        raise ValueError("Legacy agent defaults no longer match the supported source")
    launch = shlex.split(launch_args.read_text())
    config = tomllib.loads(variant.read_text())
    if config.get("completion_gate") is not True or config.get("presub_merge") is not False:
        raise ValueError("Legacy variant must prove the standard non-merging completion gate")
    if "--completion-gate" not in launch or "--presub-merge" in launch:
        raise ValueError("Legacy launch arguments do not prove the standard completion gate")
    run_path = checkpoint / "run.json"
    if "run.json" not in manifest["files"]:
        raise ValueError("Legacy checkpoint is missing its manifest-covered run configuration")
    run = json.loads(run_path.read_text())
    raw_spec = run["spec"].copy()
    raw_spec.pop("coordinator_notebook", None)  # Workers are finished; no live monitor is restored.
    raw_spec["completion_gate"] = GateDescriptor(kind="cooperagents.verification.validate").build()
    raw_spec["assignments"] = [Assignment(**a) for a in raw_spec["assignments"]]
    spec = TeamSpec(**raw_spec)
    validate_capture_spec(spec)
    if spec.contract_first:
        raise ValueError("Legacy contract-first assignments lack saved effective provenance")
    if config.get("repair") is not True or config.get("repair_attempts") != spec.repair_attempts:
        raise ValueError("Legacy variant repair settings differ from the checkpoint")

    def flag_value(flag: str, default: int) -> int:
        if launch.count(flag) > 1:
            raise ValueError(f"Duplicate legacy argument: {flag}")
        return int(launch[launch.index(flag) + 1]) if flag in launch else default

    if "--repair-integrator" not in launch or flag_value("--repair-attempts", 1) != spec.repair_attempts:
        raise ValueError("Legacy launch repair settings differ from checkpoint")
    if flag_value("--repair-steps", 25) != spec.repair_step_limit or (flag_value("--repair-time", 0) or None) != spec.repair_time_limit:
        raise ValueError("Legacy launch repair budgets differ from checkpoint")
    if flag_value("--step-limit", 40) != run["step_limit"]:
        raise ValueError("Legacy launch worker budget differs from checkpoint")
    for flag, expected in (
        ("--no-seed", not spec.seed_prior),
        ("--coop-tools", spec.coop_tools),
        ("--spec-fidelity", spec.spec_fidelity),
        ("--tdd-preamble", spec.tdd_preamble),
        ("--mine-conventions", spec.mine_conventions),
        ("--guard-git", spec.guard_git),
        ("--focused-repair", spec.focused_repair),
        ("--behavioral-gate", spec.behavioral_gate),
        ("--tool-protocol", spec.tool_protocol),
        ("--wait-protocol", spec.wait_protocol),
        ("--git-share", spec.git_share),
    ):
        if (flag in launch) != expected:
            raise ValueError(f"Legacy launch flag differs from checkpoint: {flag}")
    # Source launcher uses command_timeout=300 and the harness cost default=5.
    if run["command_timeout"] != 300 or run["cost_limit"] != 5.0:
        raise ValueError("Legacy harness budgets differ from the pinned launcher")
    replay(journal)  # Checks contiguous sequence and all request/response actor pairs.
    with open_events(journal) as handle:
        events = [json.loads(line) for line in handle]
    captures = [
        e
        for e in events
        if e["actor"] == "integrator1"
        and e["event"] == "checkpoint_end"
        and e["data"].get("boundary") == "repair_agent_start"
        and e["data"].get("files") == manifest["files"]
        and e["data"].get("patch_source") == state["patch_source"]
    ]
    if len(captures) != 1:
        raise ValueError("Legacy journal has no unique matching checkpoint receipt")
    capture = captures[0]
    tail = [e for e in events if e["actor"] == "integrator1" and e["seq"] > capture["seq"]]
    starts = [e for e in tail if e["event"] == "agent_start"]
    contexts = [e for e in tail if e["event"] == "context" and e["data"].get("reason") == "start"]
    requests = [e for e in tail if e["event"] == "request" and "model" in e["data"]["request"]]
    if len(starts) != 1 or len(contexts) != 1 or not requests or contexts[0]["data"]["messages"] != []:
        raise ValueError("Legacy journal lacks a unique complete integrator startup")
    start, context, request_event = starts[0], contexts[0], requests[0]
    if not capture["seq"] < start["seq"] < context["seq"] < request_event["seq"]:
        raise ValueError("Legacy startup event order differs from the pinned source")
    messages_events = [e for e in tail if e["event"] == "messages" and context["seq"] < e["seq"] < request_event["seq"]]
    if len(messages_events) != 1:
        raise ValueError("Legacy initial messages were changed before the first model request")
    messages = messages_events[0]["data"]["messages"]
    request = request_event["data"]["request"]
    effective_task = prefix_task(spec, state["metadata"]["task"])
    if any(
        start["data"].get(k) != value
        for k, value in {
            "task": effective_task,
            "step_limit": spec.repair_step_limit,
            "time_limit_s": spec.repair_time_limit,
            "model": spec.model,
        }.items()
    ):
        raise ValueError("Legacy integrator start differs from checkpoint task/budgets")
    if messages != request.get("messages") or len(messages) != 2 or any("[REDACTED]" in str(m) for m in messages):
        raise ValueError("Legacy initial SDK messages are missing, changed or redacted")
    cfg = yaml.safe_load((source / "src/cooperagents/vendor/mini_swe/config/solo.yaml").read_text())
    system_template = cfg["agent"]["system_template"]
    if spec.tool_protocol:
        system_template += worker._SEND_MESSAGE_SYSTEM
    if spec.wait_protocol:
        system_template += worker._WAIT_SYSTEM
    if spec.git_share:
        system_template += worker._GIT_SHARE_SYSTEM
    agent_cfg = AgentConfig(
        system_template=system_template,
        instance_template=cfg["agent"]["instance_template"],
        step_limit=spec.repair_step_limit,
        cost_limit=run["cost_limit"],
        compaction_token_trigger=cfg["agent"].get("compaction_token_trigger", 28000),
    )
    # Platform fields are extracted from the one pinned system-information block;
    # the entire rendered message must then match, including its Darwin branch.
    import re

    matches = re.findall(r"<system_information>\s*([^\n]+)\s*</system_information>", messages[1]["content"])
    if len(matches) != 1:
        raise ValueError("Legacy initial user message lacks host platform evidence")
    parts = matches[0].split(" ", 2)
    if len(parts) != 3 or " " not in parts[2]:
        raise ValueError("Legacy platform evidence is incomplete")
    version, machine = parts[2].rsplit(" ", 1)
    variables = {"task": effective_task, "system": parts[0], "release": parts[1], "version": version, "machine": machine}
    for message, template in zip(messages, (system_template, agent_cfg.instance_template), strict=True):
        if message["content"] != Template(template, undefined=StrictUndefined).render(**variables):
            raise ValueError("Legacy rendered prompt differs from pinned templates")
    generation = {
        k: v
        for k, v in request.items()
        if k
        not in {
            "model",
            "messages",
            "tools",
            "api_key",
            "api_base",
            "extra_headers",
        }
    }
    model_cfg = LitellmModelConfig(
        model_name=request["model"],
        model_kwargs=safe_generation(generation),
        cost_tracking="ignore_errors",
        litellm_model_registry=None,
        observation_template=cfg["model"]["observation_template"],
        format_error_template=cfg["model"]["format_error_template"],
    )
    expected_name = spec.model if spec.model.startswith(("openai/", "azure/", "anthropic/", "hosted_vllm/")) else f"openai/{spec.model}"
    if model_cfg.model_name != expected_name:
        raise ValueError("Legacy first SDK model differs from saved model provenance")
    settings = RepairSettings(
        repo=spec.repo,
        task_id=spec.task_id,
        features=sorted(spec.features),
        assignments=[Assignment(**a) for a in run["assignments"]],
        attempts=spec.repair_attempts,
        focused=spec.focused_repair,
        behavioral_gate=spec.behavioral_gate,
        spec_fidelity=spec.spec_fidelity,
        tdd_preamble=spec.tdd_preamble,
        mine_conventions=spec.mine_conventions,
        tool_protocol=spec.tool_protocol,
        wait_protocol=spec.wait_protocol,
        git_share=spec.git_share,
    )
    result = RepairInput(
        version=1,
        original_task=state["metadata"]["task"],
        effective_task=effective_task,
        initial_messages=messages,
        agent_config=agent_cfg.model_dump(mode="json"),
        model_config_data=model_cfg.model_dump(mode="json"),
        tools=request["tools"],
        command_timeout=run["command_timeout"],
        guard_git=spec.guard_git,
        time_limit_s=spec.repair_time_limit,
        gate=GateDescriptor(kind="cooperagents.verification.validate"),
        settings=settings,
        source_hashes=hashes,
        legacy_event_sequences={
            "checkpoint_end": capture["seq"],
            "agent_start": start["seq"],
            "context": context["seq"],
            "first_sdk_request": request_event["seq"],
        },
        evidence_hashes={
            "checkpoint_manifest": sha256(checkpoint / "manifest.json"),
            "journal": sha256(journal),
            "variant": sha256(variant),
            "launch_args": sha256(launch_args),
        },
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(result.model_dump_json(indent=2))
    return result
