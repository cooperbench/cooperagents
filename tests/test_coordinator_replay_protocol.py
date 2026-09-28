"""Keep bounded worker replays aligned with mini-swe's tool-call rejection loop."""

from types import SimpleNamespace

from scripts import coordinator_worker_replay as replay

from cooperagents.env.base import ExecResult


def run_replies(monkeypatch, replies: list[tuple[str, list | None]]) -> tuple[dict, list[list[dict]]]:
    requests: list[list[dict]] = []
    pending = iter(replies)

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            requests.append([dict(message) for message in kwargs["messages"]])
            content, calls = next(pending)
            message = SimpleNamespace(content=content, tool_calls=calls)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 7}),
            )

    class FakeEnv:
        def execute(self, command, **kwargs):
            return ExecResult("ok" if command != "git diff HEAD" else "", 0)

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
    monkeypatch.setattr(replay, "replay_environment", lambda *args: FakeEnv())
    monkeypatch.setattr(replay, "seed_environment", lambda *args: None)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://unused.invalid")
    point = {
        "id": "train-case",
        "image": "unused",
        "phase": "early",
        "worker_request_seq": 1,
        "replay_model_calls": len(replies),
    }
    result = replay.replay_one(point, {"actions": []}, None, "candidate")
    return result, requests


def test_text_ack_gets_format_error_then_next_call_can_send(monkeypatch):
    call = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(
            name="send_message",
            arguments='{"recipient":"agent2","content":"I propose a split"}',
        ),
    )
    result, requests = run_replies(monkeypatch, [("ACK", None), ("", [call])])

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


def test_repeated_text_rejections_use_full_call_budget(monkeypatch):
    result, requests = run_replies(monkeypatch, [("ACK", None)] * 5)
    assert len(requests) == len(result["steps"]) == 5
    assert all(step["accepted"] is False for step in result["steps"])
