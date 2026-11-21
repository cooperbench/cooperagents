"""Exercise the deterministic monitor through real offline worker episodes."""

import json
import shlex
import socket
import threading
import time
from collections import Counter
from importlib.resources import files
from pathlib import PurePosixPath
from typing import NamedTuple

import pytest

from cooperagents.bus.memory import InMemoryBus
from cooperagents.env.local import LocalEnv
from cooperagents.harness import UnifiedHarness
from cooperagents.trajectory import Trajectory, open_events
from cooperagents.types import Assignment, TeamSpec

WORKERS = ("agent1", "agent2")
PREFIX = "[coordinator] "
SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
INTERVAL = 0.05
OBSERVATION_SECONDS = 15 * INTERVAL
WAIT_SECONDS = 5

# Public fallback wording from 4d98be0.
FALLBACK = {
    "LOOP": "You appear to be repeating near-identical commands without progress. Step back: state "
    "your goal in one sentence, then take a DIFFERENT approach (e.g. rewrite the whole file "
    "instead of patching it line by line).",
    "STALL": "Your last several commands returned the same error. Do not retry it again — read the "
    "error carefully and fix its CAUSE, or route around it another way.",
    "COLLISION": "You and your teammate are editing the same file(s): {files}. Confine your edits to "
    "clearly separate regions and reuse their public names, or your merged work will conflict.",
}
TRACKED = ("a-tracked.txt", "z-tracked.txt")
OVERLAP = ("z-untracked.txt", "b-new.txt", *reversed(TRACKED))
EXCLUDED = (".cb_checks/check.txt", ".cb_checks-extra.txt", "patch.txt")
LOOP_HEADS = ("printf %s", "printf '%s'", "printf\t%s", 'printf "%s"', "printf  %s", "printf \t%s")
ERROR = "error: same invalid argument"


def unexpected(*args, **kwargs):
    raise AssertionError("Unexpected network access or unscripted completion")


def response(commands):
    from litellm import ModelResponse

    return ModelResponse(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"tool-{i}",
                            "type": "function",
                            "function": {"name": "bash", "arguments": json.dumps({"command": command})},
                        }
                        for i, command in enumerate(commands)
                    ],
                }
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 1},
    )


def user_text(messages):
    return "\n".join(message.get("content") or "" for message in messages if message["role"] == "user")


def nudge_lines(messages):
    return [line for line in user_text(messages).splitlines() if line.startswith(PREFIX)]


class ObservedTrajectory(Trajectory):
    """Let a scripted peer wait for the first worker's public completion event."""

    def __init__(self, path):
        super().__init__(path)
        self.finished = {agent: threading.Event() for agent in WORKERS}

    def emit(self, actor, event, **data):
        seq = super().emit(actor, event, **data)
        if event == "agent_end" and actor in self.finished:
            self.finished[actor].set()
        return seq


class Episode(NamedTuple):
    events: list[dict]
    nudges: dict[str, list[str]]
    rows: list[dict]


def team_spec(*, coordinator=False, **changes):
    return TeamSpec(
        run_id="deterministic-macro",
        repo="synthetic",
        task_id=1,
        features=[1, 2],
        assignments=[
            Assignment(agent_id=agent, role="member", task=f"Implement the fixture for {agent}.", feature_id=i)
            for i, agent in enumerate(WORKERS, 1)
        ],
        worker="mini_swe",
        model="dummy",
        shared_workspace=True,
        coop_tools=True,
        seed_prior=False,
        deterministic_coordinator=True,
        coordinator=coordinator,
        agent_time_limit=15,
        **changes,
    )


@pytest.fixture
def episode(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://unused.invalid")
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append("network or OpenAI client")
        unexpected()

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)

    import litellm
    import openai

    monkeypatch.setattr(openai, "OpenAI", forbidden)
    seed = LocalEnv.fresh(workdir=str(tmp_path))
    for name in TRACKED:
        seed.write_file(name, "base\n")
    commit = seed.execute("git add . && git commit -qm 'test fixture'")
    assert commit.exit_code == 0, commit.stdout

    def run(first, second=((),), *, composer=None, stagger=False, ready=None):
        # Each phase executes its whole batch before the worker waits in the
        # next model call. The following command gives the worker a new turn
        # in which to drain notices from the monitor.
        assert len(first) == len(second)
        checkpoint = threading.Barrier(len(WORKERS), action=ready.set if ready is not None else None)
        scripts = {
            agent: iter([turn for batch in phases for turn in ((batch, None) if batch else (None,))] + [(SUBMIT,)])
            for agent, phases in zip(WORKERS, (first, second), strict=True)
        }
        run_dir = tmp_path / f"episode-{len(list(tmp_path.glob('episode-*')))}"
        journal = ObservedTrajectory(run_dir / "trajectory.jsonl.gz")
        peer_started = False

        def worker(messages, **_):
            nonlocal peer_started
            task = user_text(messages)
            actors = [agent for agent in WORKERS if f"Implement the fixture for {agent}." in task]
            if len(actors) != 1:
                forbidden_calls.append("unscripted model completion")
                unexpected()
            (agent,) = actors
            if stagger and agent == WORKERS[1] and not peer_started:
                peer_started = True
                assert journal.finished[WORKERS[0]].wait(WAIT_SECONDS), "First worker did not finish"
            commands = next(scripts[agent])
            if commands is None:
                if not stagger:
                    checkpoint.wait(WAIT_SECONDS)
                time.sleep(OBSERVATION_SECONDS)
                commands = ("true",)
            return response(commands)

        monkeypatch.setattr(litellm, "completion", worker)

        def factory(agent):
            return LocalEnv.clone(seed.repo_path, workdir=str(run_dir))

        try:
            result = UnifiedHarness(
                bus=InMemoryBus("deterministic-macro"),
                trajectory=journal,
                step_limit=20,
                deterministic_coordinator_interval_s=INTERVAL,
                coordinator_complete=composer,
            ).run(team_spec(), env_factory=factory)
        finally:
            journal.close()

        assert forbidden_calls == []
        assert {agent: worker.status for agent, worker in result.seeds.items()} == dict.fromkeys(WORKERS, "submitted")
        with open_events(journal.path) as handle:
            rows = list(map(json.loads, handle))
        nudges = {agent: [] for agent in WORKERS}
        for row in rows:
            if row["event"] == "messages" and row["actor"] in nudges:
                nudges[row["actor"]].extend(nudge_lines(row["data"]["messages"]))
        events = [{"agent": event["agent"], "kind": event["kind"]} for event in result.metrics["coordinator_events"]]
        assert Counter(event["agent"] for event in events) == Counter(
            {agent: len(messages) for agent, messages in nudges.items() if messages}
        )
        return Episode(events, nudges, rows)

    return run


def output_command(head, output, returncode=0):
    return f"{head} {shlex.quote(output)}; exit {returncode}"


def loop_commands(heads=LOOP_HEADS, *, error_tail=False):
    return tuple(
        output_command(head, ERROR if error_tail and i >= 2 else f"result {i}") for i, head in enumerate(heads)
    )


def stall_commands(outputs=(ERROR,) * 4, *, returncode=0):
    return tuple(output_command(f"CASE={i} printf %s", output, returncode) for i, output in enumerate(outputs))


def dirty_files(*paths):
    edits = []
    for path in paths:
        parent = shlex.quote(str(PurePosixPath(path).parent))
        edits.append(f"mkdir -p {parent} && printf 'edited\\n' > {shlex.quote(path)}")
    return " && ".join(edits)


def assert_trigger(run, kind, *, agents=(WORKERS[0],)):
    expected = {(agent, kind) for agent in agents} if kind else set()
    assert {(event["agent"], event["kind"]) for event in run.events} == expected


@pytest.mark.parametrize(
    ("heads", "kind"),
    [
        pytest.param((*LOOP_HEADS[:3], *LOOP_HEADS[4:]), None, id="four-matches-in-five-commands"),
        pytest.param((*LOOP_HEADS[:5], LOOP_HEADS[1]), None, id="three-of-six"),
        pytest.param((*LOOP_HEADS[:5], LOOP_HEADS[1], LOOP_HEADS[0]), None, id="fourth-match-outside-window"),
        pytest.param(LOOP_HEADS, "LOOP", id="four-of-six"),
        pytest.param((LOOP_HEADS[0], *LOOP_HEADS[1:3], *LOOP_HEADS[4:], LOOP_HEADS[0]), "LOOP", id="five-of-six"),
        pytest.param((LOOP_HEADS[0],) * 6, "LOOP", id="six-of-six"),
    ],
)
def test_loop_uses_last_six_commands_and_first_two_whitespace_tokens(episode, heads, kind):
    assert_trigger(episode((loop_commands(heads),)), kind)


@pytest.mark.parametrize(
    ("outputs", "returncode", "kind"),
    [
        pytest.param((ERROR,) * 3, 0, None, id="three-errors"),
        pytest.param((ERROR,) * 4, 0, "STALL", id="error-text-with-zero-exit"),
        pytest.param(("command did not finish",) * 4, 23, "STALL", id="nonzero-exit-without-error-text"),
        pytest.param(("command finished",) * 4, 0, None, id="identical-successes"),
        pytest.param(
            ("different earlier error", *(ERROR.ljust(120, "x") + str(i) for i in range(4))),
            0,
            "STALL",
            id="last-four-match-through-char-120",
        ),
        pytest.param(
            (ERROR.ljust(119, "x") + "a",) * 3 + (ERROR.ljust(119, "x") + "b",),
            0,
            None,
            id="mismatch-at-char-120",
        ),
    ],
)
def test_stall_requires_four_matching_error_outputs(episode, outputs, returncode, kind):
    assert_trigger(episode((stall_commands(outputs, returncode=returncode),)), kind)


@pytest.mark.parametrize(
    ("left", "right", "kind"),
    [
        pytest.param((TRACKED[0],), (TRACKED[1],), None, id="disjoint-dirty-tracked-files"),
        pytest.param(EXCLUDED, EXCLUDED, None, id="excluded-paths-only"),
        pytest.param(("new-file.txt",), ("new-file.txt",), "COLLISION", id="untracked-overlap"),
        pytest.param((TRACKED[0],), (TRACKED[0],), "COLLISION", id="tracked-overlap"),
    ],
)
def test_collision_requires_dirty_overlap(episode, left, right, kind):
    run = episode(((dirty_files(*left),),), ((dirty_files(*right),),))
    assert_trigger(run, kind, agents=WORKERS)


@pytest.mark.parametrize(
    ("commands", "winner"),
    [
        pytest.param(loop_commands(error_tail=True), "LOOP", id="loop-before-stall-and-collision"),
        pytest.param(stall_commands(), "STALL", id="stall-before-collision"),
    ],
)
def test_trigger_precedence(episode, commands, winner):
    ready = threading.Event()
    edit = dirty_files(TRACKED[0])

    def compose(*args, **kwargs):
        # At most one early decision can see a partially executed script.
        assert ready.wait(WAIT_SECONDS), "Workers did not reach the observation window"
        return "Change approach."

    run = episode(((edit, *commands),), ((edit,),), composer=compose, ready=ready)
    first, second = ([event["kind"] for event in run.events if event["agent"] == agent] for agent in WORKERS)
    assert first[-2:] == [winner, winner]
    assert second and set(second) == {"COLLISION"}


@pytest.mark.parametrize("composer_raises", [False, True], ids=["unavailable", "raises"])
@pytest.mark.parametrize("kind", ["LOOP", "STALL", "COLLISION"])
def test_fallback_uses_historical_templates(episode, kind, composer_raises):
    calls = []

    def broken_composer(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("scripted composer outage")

    first, second = {
        "LOOP": (loop_commands(), ()),
        "STALL": (stall_commands(), ()),
        "COLLISION": ((dirty_files(*OVERLAP, *EXCLUDED),), (dirty_files(*OVERLAP, *EXCLUDED),)),
    }[kind]
    run = episode((first,), (second,), composer=broken_composer if composer_raises else None)
    assert_trigger(run, kind, agents=WORKERS if kind == "COLLISION" else (WORKERS[0],))
    if composer_raises:
        assert len(calls) == len(run.events)

    # Four overlapping names were created. The first three in sorted order
    # include both tracked and untracked files.
    expected = PREFIX + FALLBACK[kind].format(files="a-tracked.txt,b-new.txt,z-tracked.txt")
    assert {message for messages in run.nudges.values() for message in messages} == {expected}


def test_nudge_cap_and_attribution_are_invariant_to_composer_text(episode):
    expected_events = [{"agent": WORKERS[0], "kind": "LOOP"}] * 3 + [{"agent": WORKERS[1], "kind": "STALL"}] * 3
    for text in ("Try a different command.", "Inspect the inputs before trying again."):
        # After each worker drains its first set of notices, give it another
        # full trigger window and time to receive any additional notices.
        run = episode(
            (loop_commands(), loop_commands()),
            (stall_commands(), stall_commands()),
            composer=lambda *args, text=text, **kwargs: text,
            stagger=True,
        )
        assert run.events == expected_events
        assert run.nudges == {agent: [PREFIX + text] * 3 for agent in WORKERS}


def test_private_bounded_nudges_preserve_solo_prompt_and_tool_family(episode):
    from jinja2 import Template
    from yaml import safe_load

    solo = files("cooperagents.vendor.mini_swe").joinpath("config", "solo.yaml")
    system_prompt = Template(safe_load(solo.read_text(encoding="utf-8"))["agent"]["system_template"]).render()
    opening = "Use a different approach. "
    composed = opening + "x" * 650

    def compose(*args, **kwargs):
        assert not kwargs.get("tools"), "The deterministic composer was asked for tool calls"
        return composed

    run = episode((loop_commands(),), composer=compose)
    assert_trigger(run, "LOOP")
    assert run.nudges[WORKERS[1]] == []
    for message in run.nudges[WORKERS[0]]:
        assert message.startswith(PREFIX + opening)
        assert len(message) <= 400

    last_request_nudges = {}
    for row in run.rows:
        assert (row["actor"], row["event"]) != ("coordinator", "decision")
        if row["event"] != "request":
            continue
        request = row["data"]["request"]
        tools = {tool["function"]["name"] for tool in request.get("tools") or []}
        assert tools.isdisjoint({"update_notebook", "finish_planning"})
        if row["actor"] not in WORKERS:
            assert not tools, f"Unexpected coordinator tool call request: {request}"
            continue
        if "messages" not in request:
            continue
        messages = request["messages"]
        assert [message["content"] for message in messages if message["role"] == "system"] == [system_prompt]
        notices = nudge_lines(messages)
        assert notices == run.nudges[row["actor"]][:len(notices)]
        last_request_nudges[row["actor"]] = notices
    assert last_request_nudges == run.nudges


def test_coordinator_families_are_mutually_exclusive():
    with pytest.raises(ValueError):
        UnifiedHarness(bus=InMemoryBus("invalid")).run(
            team_spec(coordinator=True, coordinator_notebook=False),
            env_factory=unexpected,
        )
