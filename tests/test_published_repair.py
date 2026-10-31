"""Exercise the official repair loop; only container creation is replaced locally."""

import json
import shutil
import socket

import pytest

from cooperagents.checkpoint import sha256, verify_checkpoint
from cooperagents.completion import CompletionBinding, CompletionSettings
from cooperagents.env.apptainer import ApptainerEnv
from cooperagents.published import LEGACY, V3, PublishedCheckpoint
from cooperagents.repair import run_repair_checkpoint
from cooperagents.workers import mini_swe_worker as worker
from test_repair_replay import completion, make_checkpoint


def source(monkeypatch, tmp_path, version):
    cp = make_checkpoint(monkeypatch, tmp_path, attempts=1)
    root = tmp_path / ("b" * 64)
    root.mkdir()
    shutil.copytree(cp, root / "checkpoint")
    fmt = {"legacy": LEGACY, "v3": V3}[version]
    (root / "publication.json").write_text(
        json.dumps(
            dict(
                format=fmt,
                key=root.name,
                state="filesystem-verified-repair-unverified",
                metadata={"legacy_manifest_sha256": sha256(cp / "manifest.json")},
            )
        )
    )
    state = verify_checkpoint(cp)
    ref = PublishedCheckpoint.from_dict(
        dict(
            id="fixture",
            node=socket.gethostname(),
            version=version,
            format=fmt,
            publication=dict(path=str(root), key=root.name, sha256=sha256(root / "publication.json")),
            legacy_manifest_sha256=sha256(cp / "manifest.json"),
            image=state["runtime"]["image"],
            image_sha256=state["runtime"]["image_sha256"],
            training_eligible=False,
            repair_validated=False,
        )
    )
    return root / "checkpoint", ref


@pytest.mark.parametrize("version", ["legacy", "v3"])
@pytest.mark.parametrize("purpose", ["difficulty", "training"])
def test_official_published_repair_commits_exports_patch_preserves_budget_and_cleans(monkeypatch, tmp_path, version, purpose):
    cp, ref = source(monkeypatch, tmp_path, version)
    opened = []
    commands = []

    def restore(reference, checkpoint, **kwargs):
        assert reference == ref and checkpoint == cp and kwargs["purpose"] == purpose
        env = ApptainerEnv.from_checkpoint(cp, scratch=kwargs["scratch"])
        env.restore_metrics = dict(
            execution_purpose=purpose, restore_mode=version, publication_sha256=ref.publication.sha256,
            training_eligible=False, repair_validated=False
        )
        opened.append(env)
        return env

    monkeypatch.setattr("cooperagents.published_restore.restore_environment", restore)
    actions = iter(
        [
            (
                "printf 'fixed = 1\\n' > a.py && git add -A && "
                "git -c user.name=fixture -c user.email=fixture@example.invalid commit -qm repair"
            ),
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        ]
    )

    def query(*args, **kwargs):
        command = next(actions)
        commands.append(command)
        return completion(command)

    settings = CompletionSettings(model="fixture-policy", revision="recorded", generation={"max_tokens": 4096}, timeout=30.0)
    binding = CompletionBinding(query, settings, lambda request: pytest.fail("unexpected summary"), settings)
    monkeypatch.setattr(worker.LitellmModel, "_query_inner", lambda *a, **k: pytest.fail("external model call"))
    result = run_repair_checkpoint(cp, published=ref, purpose=purpose, scratch=tmp_path / "scratch", run_id="test", completion=binding)
    assert "fixed = 1" in result.integrated.patch
    assert result.metrics["execution_purpose"] == purpose
    assert result.metrics["training_eligible"] is result.metrics["repair_validated"] is False
    assert result.metrics["restore_mode"] == version
    assert result.metrics["publication_sha256"] == ref.publication.sha256
    assert result.metrics["saved_attempt_limit"] == result.metrics["effective_attempt_limit"] == 1
    assert result.metrics["budget_truncated"] is False
    assert len(commands) == 2 and not opened[0].root.exists()
    assert sha256(cp / "manifest.json") == ref.manifest_sha256


def test_publication_mismatch_or_missing_purpose_never_starts_agent(monkeypatch, tmp_path):
    cp, ref = source(monkeypatch, tmp_path, "v3")
    monkeypatch.setattr(worker.LitellmModel, "query", lambda *a, **k: pytest.fail("must not call model"))
    for kwargs in [{}, {"purpose": "unknown"}, {"purpose": "difficulty", "repair_input": cp / "repair-input.json"}]:
        with pytest.raises(ValueError):
            run_repair_checkpoint(cp, published=ref, scratch=tmp_path / "scratch", run_id="bad", **kwargs)
    with pytest.raises(ValueError):
        run_repair_checkpoint(cp.parent, published=ref, purpose="difficulty", scratch=tmp_path / "scratch", run_id="bad")


@pytest.mark.parametrize("purpose", ["difficulty", "training"])
def test_public_restore_forwards_purpose_and_rejects_tampered_metadata(monkeypatch, tmp_path, purpose):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from cooperagents.env.image_base import ImageBase
    from cooperagents.published import PublicationError
    from cooperagents.published_restore import restore_environment

    cp, ref = source(monkeypatch, tmp_path, "v3")
    base = SimpleNamespace(proof={"image_sha256": ref.image_sha256}, close=Mock())
    prepared = Mock(return_value=base)
    restored = Mock(return_value=object())
    monkeypatch.setattr(ImageBase, "prepare", prepared)
    monkeypatch.setattr("cooperagents.published_restore._restore_with_base", restored)
    assert restore_environment(ref, cp, scratch=tmp_path, purpose=purpose) is restored.return_value
    assert restored.call_args.args[-1] == purpose
    base.close.assert_not_called()
    prepared.reset_mock()
    (cp / "manifest.json").write_text("tampered")
    with pytest.raises(PublicationError, match="identity mismatch"):
        restore_environment(ref, cp, scratch=tmp_path, purpose=purpose)
    prepared.assert_not_called()
