"""Publication boundary attacks without starting namespaces or a model."""
import json
import shutil
import socket
from pathlib import Path

import pytest

from cooperagents.checkpoint import sha256, verify_checkpoint
from cooperagents.published import LEGACY, V3, PublicationError, PublishedCheckpoint, _inventory
from cooperagents.published_profiles import PRODUCERS
from test_repair_replay import make_checkpoint


def publication(tmp_path, monkeypatch, version):
    cp = make_checkpoint(monkeypatch, tmp_path, attempts=1)
    root = tmp_path/('c'*64)
    root.mkdir()
    shutil.copytree(cp, root/'checkpoint')
    state = verify_checkpoint(cp)
    fmt = {'legacy': LEGACY, 'v3': V3}[version]
    for name in ['target-index.json.gz', 'delta.tar', 'repo.tar', 'legacy-publication.json']:
        (root/name).write_text('fixture')
    risks = {'review_status':'needs_review', 'training_eligible':False, 'findings':[{'category':'unknown'}]}
    (root/'risks.json').write_text(json.dumps(risks))
    runtime = {'kernel':'fixture', 'uid':100, 'gid':100, 'apptainer':'fixture'}
    receipt = {**PRODUCERS[fmt], 'runtime_qualification':runtime, 'profile':'captured-post-gate-v1',
        'target_inventory_sha256':sha256(root/'target-index.json.gz'),
        'legacy_publication_sha256':sha256(root/'legacy-publication.json'),
        'manifest_sha256':sha256(cp/'manifest.json'), 'image_sha256':state['runtime']['image_sha256'],
        'risk_sha256':sha256(root/'risks.json'), 'training_eligible':False,
        'delta_sha256':sha256(root/'delta.tar'), 'repo_sha256':sha256(root/'repo.tar')}
    (root/'receipt.json').write_text(json.dumps(receipt))
    header = {'format':fmt, 'key':root.name, 'state':'filesystem-verified-repair-unverified',
        'metadata':{'legacy_manifest_sha256':receipt['manifest_sha256'], 'image_sha256':receipt['image_sha256'],
            'receipt_sha256':sha256(root/'receipt.json'), 'profile':receipt['profile'],
            'training_eligible':False, 'repair_validated':False}, 'files':_inventory(root)}
    (root/'publication.json').write_text(json.dumps(header))
    ref = PublishedCheckpoint.from_dict(dict(id='fixture', node=socket.gethostname(), version=version, format=fmt,
        publication={'path':str(root), 'key':root.name, 'sha256':sha256(root/'publication.json')},
        legacy_manifest_sha256=receipt['manifest_sha256'],image=state['runtime']['image'],
        image_sha256=receipt['image_sha256'],training_eligible=False,repair_validated=False))
    monkeypatch.setattr('cooperagents.published.runtime_qualification', lambda:runtime)
    captured=[]
    monkeypatch.setattr('cooperagents.published_profiles.validate_capture',lambda hashes,profile:captured.append((hashes,profile)))
    if version == 'v3':
        for path in [*root.rglob('*'), root]:
            path.chmod(path.stat().st_mode & ~0o222)
    return root,ref,captured


@pytest.mark.parametrize('version',['legacy','v3'])
def test_all_bytes_are_authenticated_before_profile_admission(tmp_path,monkeypatch,version):
    root,ref,captured = publication(tmp_path,monkeypatch,version)
    verified = ref.verify(purpose='difficulty')
    assert verified.reference == ref and verified.risks['findings'] == [{'category':'unknown'}]
    assert len(captured) == 1
    file = root/'repo.tar'
    file.chmod(0o600)
    file.write_text('corrupt')
    with pytest.raises(PublicationError, match='payload'):
        ref.verify(purpose='difficulty')
    assert len(captured) == 1


@pytest.mark.parametrize('mutation',['extra','symlink','manifest','wrong-node','wrong-purpose','writable-v3'])
def test_publication_admission_fails_closed(tmp_path,monkeypatch,mutation):
    root,ref,captured=publication(tmp_path,monkeypatch,'v3')
    if mutation == 'extra':
        root.chmod(0o700);(root/'unlisted').write_bytes(b'x')
    elif mutation == 'symlink':
        root.chmod(0o700);(root/'link').symlink_to(root/'repo.tar')
    elif mutation == 'manifest':
        p=root/'publication.json';p.chmod(0o600);p.write_bytes(b'{}')
    elif mutation == 'wrong-node':
        monkeypatch.setattr('cooperagents.published.socket.gethostname',lambda:'wrong-node')
    elif mutation == 'writable-v3':
        (root/'repo.tar').chmod(0o600)
    with pytest.raises((ValueError,OSError)):
        ref.verify(purpose='training' if mutation=='wrong-purpose' else 'difficulty')
    assert captured == []
