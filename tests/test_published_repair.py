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
@pytest.mark.parametrize("require_commit", [False, True])
def test_official_published_repair_commits_exports_patch_preserves_budget_and_cleans(monkeypatch, tmp_path, version, require_commit):
    cp, ref = source(monkeypatch, tmp_path, version)
    opened = []
    commands = []

    def restore(reference, checkpoint, **kwargs):
        assert reference == ref and checkpoint == cp
        env = ApptainerEnv.from_checkpoint(cp, scratch=kwargs["scratch"])
        env.restore_metrics = dict(
            restore_mode=version, publication_sha256=ref.publication.sha256, training_eligible=False, repair_validated=False
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

    def query(request):
        from cooperagents.repair_commit import COMMIT_INSTRUCTION

        assert any(COMMIT_INSTRUCTION in str(message) for message in request.messages) == require_commit
        command = next(actions)
        commands.append(command)
        return completion(command)

    settings = CompletionSettings(model="fixture-policy", revision="recorded", generation={"max_tokens": 4096}, timeout=30.0)
    binding = CompletionBinding(query, settings, lambda request: pytest.fail("unexpected summary"), settings)
    monkeypatch.setattr(worker.LitellmModel, "_query_inner", lambda *a, **k: pytest.fail("external model call"))
    result = run_repair_checkpoint(
        cp,
        published=ref,
        purpose="difficulty",
        scratch=tmp_path / "scratch",
        run_id="test",
        completion=binding,
        require_commit=require_commit,
    )
    assert "fixed = 1" in result.integrated.patch
    assert result.metrics["restore_mode"] == version
    assert result.metrics["publication_sha256"] == ref.publication.sha256
    assert result.metrics["saved_attempt_limit"] == result.metrics["effective_attempt_limit"] == 1
    assert result.metrics["budget_truncated"] is False
    assert len(commands) == 2 and not opened[0].root.exists()
    assert sha256(cp / "manifest.json") == ref.manifest_sha256
    assert result.metrics["repair_commit_required"] is require_commit
    if require_commit:
        evidence = result.metrics["repair_commit_evidence"]
        assert evidence["committed"] is True and evidence["clean"] is True
        assert evidence["initial_head"] != evidence["final_head"]
    else:
        assert result.metrics["repair_commit_evidence"] is None


def test_publication_mismatch_or_training_purpose_never_starts_agent(monkeypatch, tmp_path):
    cp, ref = source(monkeypatch, tmp_path, "v3")
    monkeypatch.setattr(worker.LitellmModel, "query", lambda *a, **k: pytest.fail("must not call model"))
    for kwargs in [{"purpose": "training"}, {"purpose": "difficulty", "repair_input": cp / "repair-input.json"}]:
        with pytest.raises(ValueError):
            run_repair_checkpoint(cp, published=ref, scratch=tmp_path / "scratch", run_id="bad", **kwargs)
    with pytest.raises(ValueError):
        run_repair_checkpoint(cp.parent, published=ref, purpose="difficulty", scratch=tmp_path / "scratch", run_id="bad")


@pytest.mark.parametrize("version", ["legacy", "v3"])
def test_uncommitted_patch_is_reported_separately_from_task_score(monkeypatch, tmp_path, version):
    cp, ref = source(monkeypatch, tmp_path, version)
    monkeypatch.setattr(
        "cooperagents.published_restore.restore_environment",
        lambda reference, checkpoint, **kwargs: ApptainerEnv.from_checkpoint(cp, scratch=kwargs["scratch"]),
    )
    settings = CompletionSettings(model="fixture", revision="fixed", generation={"max_tokens": 4096}, timeout=30.0)

    def query(request):
        return completion("printf 'fixed = 1\\n' > a.py; echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")

    result = run_repair_checkpoint(
        cp,
        published=ref,
        purpose="difficulty",
        scratch=tmp_path / "scratch",
        run_id="no-commit",
        require_commit=True,
        completion=CompletionBinding(query, settings, query, settings),
    )
    assert "fixed = 1" in result.integrated.patch
    assert result.metrics["repair_commit_evidence"]["committed"] is False
    assert result.metrics["repair_commit_evidence"]["new_commit"] is False
    assert sha256(cp / "manifest.json") == ref.manifest_sha256
