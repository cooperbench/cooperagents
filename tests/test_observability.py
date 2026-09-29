"""No services or inference: real SDK spans with an in-memory exporter."""

import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cooperagents.harness import UnifiedHarness
from cooperagents.trajectory import record_call
from cooperagents.types import TeamSpec


def memory_trace(journal=None, *, run_id="test"):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cooperagents.observability import LangfuseTrace

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    client = langfuse.Langfuse(public_key=f"pk-test-{uuid4()}", secret_key="sk-test", tracer_provider=provider, span_exporter=exporter)
    sink = LangfuseTrace(SimpleNamespace(run_id=run_id, repo="repo", task_id="1"), journal, client=client)
    return sink, exporter


def test_parallel_calls_errors_redaction_and_shutdown(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")
    sink, exporter = memory_trace()

    def worker(actor):
        def complete(**request):
            return {
                "choices": [{"message": {"content": "private-test-value"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            }

        record_call(
            partial(sink.emit, actor), complete, model="dummy", messages=[{"content": actor}], api_key="private-test-value", tools=[]
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(worker, ["agent1", "agent2", "coordinator"]))
    with pytest.raises(ValueError, match="original failure"):
        record_call(
            partial(sink.emit, "agent1"), lambda **kw: (_ for _ in ()).throw(ValueError("original failure")), model="dummy", messages=[]
        )
    sink.emit("agent2", "request", request={"command": "unfinished"})
    sink.close()
    sink.close()
    spans = exporter.get_finished_spans()
    roots = {s.name: s for s in spans if s.attributes.get("langfuse.internal.as_root")}
    assert set(roots) == {"agent1", "agent2", "coordinator"}
    for span in spans:
        assert span.attributes["session.id"] == sink.session_id
        if span.name == "completion" and "usage" in span.attributes.get("langfuse.observation.output", ""):
            actor = json.loads(span.attributes["langfuse.observation.input"])["messages"][0]["content"]
            assert span.parent.span_id == roots[actor].context.span_id
            assert json.loads(span.attributes["langfuse.observation.usage_details"]) == {"input": 7, "output": 3, "total": 10}
    assert "private-test-value" not in str([dict(s.attributes) for s in spans])
    assert sum(s.attributes.get("langfuse.observation.level") == "ERROR" for s in spans) == 2


def test_export_failure_does_not_repeat_model_call():
    sink, _ = memory_trace()
    sink.client = SimpleNamespace(
        start_observation=lambda **kw: (_ for _ in ()).throw(RuntimeError()), create_trace_id=lambda **kw: "a" * 32, flush=lambda: None
    )
    calls = []
    result = record_call(partial(sink.emit, "agent1"), lambda **kw: calls.append(1) or "result", model="dummy", messages=[])
    sink.close()
    assert result == "result" and calls == [1]


def test_default_off_and_opt_in_validation(monkeypatch):
    assert UnifiedHarness().langfuse is False
    with pytest.raises(ValueError, match="single mini_swe"):
        UnifiedHarness(langfuse=True).run(
            TeamSpec(run_id="test", repo="repo", task_id=1, features=[1, 2]), env_factory=lambda _: pytest.fail("created environment")
        )
    pytest.importorskip("langfuse")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    spec = TeamSpec(run_id="test", repo="repo", task_id=1, features=[1, 2], worker="mini_swe", shared_workspace=True, coop_tools=True)
    with pytest.raises(ValueError, match="LANGFUSE_PUBLIC_KEY"):
        UnifiedHarness(langfuse=True).run(spec, env_factory=lambda _: pytest.fail("created environment"))


def test_harness_flushes_on_exception_and_does_not_mutate_itself(monkeypatch):
    sink, exporter = memory_trace()
    monkeypatch.setattr("cooperagents.observability.LangfuseTrace", lambda *a: sink)

    def fail(*args, **kwargs):
        sink.emit("agent1", "agent_start", task="synthetic")
        raise RuntimeError("run failed")

    monkeypatch.setattr(UnifiedHarness, "_run_isolated", fail)
    harness = UnifiedHarness(langfuse=True)
    spec = TeamSpec(
        run_id="test", repo="repo", task_id=1, features=[1, 2], objective="task", worker="mini_swe", shared_workspace=True, coop_tools=True
    )
    with pytest.raises(RuntimeError, match="run failed"):
        harness.run(spec, env_factory=lambda _: None)
    assert harness.langfuse and harness.trajectory is None
    assert sink._closed
    assert exporter.get_finished_spans()[0].attributes["langfuse.observation.level"] == "ERROR"


def test_credentials_do_not_enable_tracing(monkeypatch):
    # Block the optional module entirely: the disabled path must not import it.
    import sys

    monkeypatch.setitem(sys.modules, "cooperagents.observability", None)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-present")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-present")
    expected = object()
    monkeypatch.setattr(UnifiedHarness, "_run_isolated", lambda *a: expected)
    spec = TeamSpec(
        run_id="test", repo="repo", task_id=1, features=[1, 2], objective="task", worker="mini_swe", shared_workspace=True, coop_tools=True
    )
    assert UnifiedHarness().run(spec, env_factory=lambda _: None) is expected


def test_restarted_runs_group_turns_without_overwriting_observations():
    attempts = []
    for run_id in ("resume-me", "resume-me", "different-run"):
        sink, exporter = memory_trace(run_id=run_id)
        for actor in ("agent1", "agent2", "coordinator"):
            for turn in range(3):
                record_call(
                    partial(sink.emit, actor),
                    lambda **kw: {"reply": "ok"},
                    model="dummy",
                    messages=[{"role": "user", "content": str(turn)}],
                )
        sink.close()
        spans = exporter.get_finished_spans()
        roots = {s.name: s for s in spans if s.attributes.get("langfuse.internal.as_root")}
        assert set(roots) == {"agent1", "agent2", "coordinator"}
        for root in roots.values():
            turns = [s for s in spans if s.parent and s.parent.span_id == root.context.span_id]
            assert len(turns) == 3
            assert all(s.context.trace_id == root.context.trace_id for s in turns)
        assert all(s.attributes["session.id"] == sink.session_id for s in spans)
        attempts.append((sink.session_id, {name: s.context.trace_id for name, s in roots.items()}, {s.context.span_id for s in spans}))
    assert attempts[0][:2] == attempts[1][:2]
    assert attempts[0][2].isdisjoint(attempts[1][2])
    assert attempts[0][0] != attempts[2][0]
    assert set(attempts[0][1].values()).isdisjoint(attempts[2][1].values())
    assert len(set(attempts[0][1].values())) == 3
