"""Keep bounded worker replays aligned with mini-swe's tool-call rejection loop."""

import json
from types import SimpleNamespace

import pytest
from scripts import coordinator_worker_replay as replay

from cooperagents.env.base import ExecResult
from cooperagents.env.local import LocalEnv


def run_replies(
    monkeypatch,
    replies: list[tuple[str, list | None] | Exception],
    trace_path=None,
    commands: list[str] | None = None,
    environment=None,
) -> tuple[dict, list[list[dict]]]:
    requests: list[list[dict]] = []
    pending = iter(replies)

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            requests.append([dict(message) for message in kwargs["messages"]])
            item = next(pending)
            if isinstance(item, Exception):
                raise item
            content, calls = item
            message = SimpleNamespace(content=content, tool_calls=calls)
            response = SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 7}),
            )
            response.model_dump = lambda mode=None: {
                "choices": [
                    {
                        "message": {
                            "content": content,
                            "tool_calls": [
                                {"id": call.id, "function": {"name": call.function.name, "arguments": call.function.arguments}}
                                for call in calls or []
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 7},
            }
            return response

    class FakeEnv:
        def execute(self, command, **kwargs):
            if commands is not None:
                commands.append(command)
            if command == "submit":
                return ExecResult(" \n COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nfinal answer\n", 0)
            return ExecResult("ok", 0)

        def git_diff(self):
            if commands is not None:
                commands.append("git_diff")
            return ""

        def cleanup(self):
            pass

    monkeypatch.setattr("openai.OpenAI", FakeClient)
    monkeypatch.setattr(
        replay,
        "archive",
        lambda point: (
            [],
            {
                "messages": [
                    {"role": "system", "content": "work"},
                    {"role": "user", "content": "task"},
                ],
                "tools": [],
            },
            {},
        ),
    )
    monkeypatch.setattr(replay, "materialize_actions", lambda *args: ([], "", ""))
    monkeypatch.setattr(replay, "verified_patch", lambda *args: None)
    monkeypatch.setattr(replay, "replay_environment", lambda *args: environment or FakeEnv())
    monkeypatch.setattr(replay, "seed_environment", lambda *args: None)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://unused.invalid")
    point = {
        "id": "train-case",
        "image": "unused",
        "phase": "early",
        "worker_request_seq": 1,
        "replay_model_calls": len(replies),
    }
    result = replay.replay_one(point, {"actions": []}, None, "candidate", trace_path=trace_path)
    return result, requests


def test_text_ack_gets_format_error_then_next_call_can_send(monkeypatch, tmp_path):
    call = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(
            name="send_message",
            arguments='{"recipient":"agent2","content":"I propose a split"}',
        ),
    )
    trace_path = tmp_path / "candidate.trace.jsonl"
    result, requests = run_replies(monkeypatch, [("ACK", None), ("", [call])], trace_path)

    assert len(requests) == 2
    assert result["steps"][0]["content"] == "ACK"
    assert result["steps"][0]["usage"] == {"total_tokens": 7}
    assert result["steps"][0]["accepted"] is False
    assert "No tool calls found in the response" in result["steps"][0]["error"]
    assert "Every response needs to use the 'bash' tool" in result["steps"][0]["error"]
    assert requests[1][-1] == {"role": "user", "content": result["steps"][0]["error"]}
    assert all(message.get("content") != "ACK" for message in requests[1])
    assert result["steps"][1]["accepted"] is True
    assert result["steps"][1]["actions"][0]["arguments"]["recipient"] == "agent2"
    assert result["trace_path"] == str(trace_path)


def test_repeated_text_rejections_use_full_call_budget(monkeypatch):
    result, requests = run_replies(monkeypatch, [("ACK", None)] * 5)
    assert len(requests) == len(result["steps"]) == 5
    assert all(step["accepted"] is False for step in result["steps"])
    assert result["termination"] == {"reason": "call_budget", "step": 5}
    assert result["completion_gate"] == "disabled"


def test_successful_submission_stops_before_later_tools_and_model_calls(monkeypatch):
    submit = SimpleNamespace(
        id="submit",
        function=SimpleNamespace(name="bash", arguments='{"command":"submit"}'),
    )
    later_tool = SimpleNamespace(
        id="later-tool",
        function=SimpleNamespace(name="bash", arguments='{"command":"echo too late"}'),
    )
    commands: list[str] = []
    result, requests = run_replies(
        monkeypatch,
        [("", [submit, later_tool]), ("", [later_tool])],
        commands=commands,
    )

    assert len(requests) == len(result["steps"]) == 1
    assert [action["arguments"]["command"] for action in result["steps"][0]["actions"]] == ["submit"]
    assert result["termination"] == {"reason": "submitted", "step": 1, "submission": "final answer\n"}
    assert result["completion_gate"] == "disabled"
    assert commands == ["submit", "git_diff"]
    assert result["diff_collection"] == "environment.git_diff"


def test_final_diff_includes_committed_and_untracked_worker_files(monkeypatch, tmp_path):
    env = LocalEnv.fresh(workdir=str(tmp_path))
    bash = SimpleNamespace(
        id="bash",
        function=SimpleNamespace(
            name="bash",
            arguments=json.dumps(
                {
                    "command": (
                        "printf 'committed\\n' > committed.txt && git add committed.txt "
                        "&& git commit -qm worker && printf 'untracked\\n' > untracked.txt"
                    )
                }
            ),
        ),
    )

    result, _ = run_replies(monkeypatch, [("", [bash])], environment=env)

    assert "diff --git a/committed.txt b/committed.txt" in result["final_diff"]
    assert "diff --git a/untracked.txt b/untracked.txt" in result["final_diff"]
    assert "+committed" in result["final_diff"]
    assert "+untracked" in result["final_diff"]
    assert result["diff_collection"] == "environment.git_diff"


@pytest.mark.parametrize("wait", [False, True])
def test_send_message_observation_matches_real_no_reply(monkeypatch, wait):
    send = SimpleNamespace(
        id="send",
        function=SimpleNamespace(
            name="send_message",
            arguments=json.dumps({"recipient": "agent2", "content": "Scope split?", "wait": wait}),
        ),
    )
    bash = SimpleNamespace(id="bash", function=SimpleNamespace(name="bash", arguments='{"command":"echo ok"}'))
    result, requests = run_replies(monkeypatch, [("", [send]), ("", [bash])])
    action = result["steps"][0]["actions"][0]
    expected = {"output": "Message sent to agent2", "returncode": 0, "exception_info": ""}

    assert action["observation"] == expected
    assert requests[1][-1] == {"role": "tool", "tool_call_id": "send", "content": json.dumps(expected)}
    if wait:
        assert action["simulation"] == {"outcome": "wait_timeout_no_peer", "timeout_seconds": 60}
    else:
        assert "simulation" not in action


def test_trace_keeps_two_responses_and_tool_results_before_later_failure(monkeypatch, tmp_path):
    send = SimpleNamespace(
        id="send",
        function=SimpleNamespace(name="send_message", arguments='{"recipient":"agent2","content":"Scope split?"}'),
    )
    bash = SimpleNamespace(id="bash", function=SimpleNamespace(name="bash", arguments='{"command":"echo ok"}'))
    trace_path = tmp_path / "candidate.trace.jsonl"

    with pytest.raises(TimeoutError, match="model timed out"):
        run_replies(monkeypatch, [("", [send]), ("", [bash]), TimeoutError("model timed out")], trace_path)

    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in events] == [
        "request",
        "response",
        "tool_action",
        "request",
        "response",
        "tool_action",
        "request",
        "error",
    ]
    assert all(row["actor"] == "worker" for row in events)
    assert [row["data"]["response"]["usage"]["total_tokens"] for row in events if row["event"] == "response"] == [7, 7]
    assert events[2]["data"]["action"]["observation"]["output"] == "Message sent to agent2"
    assert events[5]["data"]["action"]["observation"]["output"] == "ok"
    assert events[-1]["data"]["error_type"] == "TimeoutError"
    with pytest.raises(FileExistsError):
        run_replies(monkeypatch, [("", [send])], trace_path)
