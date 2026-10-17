"""Data-only lab reception, hostile archive rejection, and immutable evidence."""
import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from app.strategy_integration.fourteen_adapter import PACKAGE
from app.strategy_integration.lab_delivery import receive, validate_delivery


def encode(value):
    return json.dumps(value, sort_keys=True).encode()


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path/'package'
    shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns('__pycache__'))
    manifest = json.loads((root/'manifest.json').read_text())
    manifest['price_input_contract'] = 'QFQ_ASOF_V1'
    manifest['split_windows'] = {'train':['2024-01-01','2025-12-31'], 'val':['2026-01-01','2026-06-30'], 'test':['2026-07-01','2026-09-30']}
    payload = encode(manifest)
    (root/'manifest.json').write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    report = {'windows': {'train': {'start':'2024-01-01','end':'2025-12-31'},
                          'val': {'start':'2026-01-01','end':'2026-06-30'},
                          'test': {'start':'2026-07-01','end':'2026-09-30'}},
              'tasks': {task: {split: {'n':100,'precision':.5,'recall':.5,'auc':.6}
                               for split in ('train','val','test')} for task in manifest['models']}}
    # Reception checks identity/coverage only. Numeric golden agreement is a worker test.
    golden = [{'expected': {'signals': [{'task': task} for task in manifest['models']]}}]
    files = {'validation_report.json':encode(report), 'golden_cases.json':encode(golden)}
    for name, data in files.items():
        (root/name).write_bytes(data)
    approval = {'schema':'PANJI_LAB_DELIVERY_V1','manifest_sha256':sha,
                'lab_revision':'c'*64,'approved_for_production':True,
                'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    (root/'delivery.json').write_bytes(encode(approval))
    return root, sha


def archive(root):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as writer:
        for file in root.rglob('*'):
            if file.is_file():
                writer.write(file, str(file.relative_to(root)))
    return buffer.getvalue()


def test_receive_is_idempotent_and_never_activates(delivery, tmp_path):
    root, sha = delivery
    target = tmp_path/'received'
    first = receive(target, archive(root), sha)
    assert first == receive(target, archive(root), sha)
    assert first['status'] == 'RECEIVED_NOT_ACTIVATED'
    assert not (target/'active_release.json').exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'models/../../escape', 'a\\b', 'a//b'])
def test_hostile_archive_path_rejected(tmp_path, name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as writer:
        writer.writestr(name, 'bad')
    with pytest.raises(ValueError, match='UNSAFE_DELIVERY_PATH'):
        receive(tmp_path/'received', buffer.getvalue(), 'a'*64)
    assert not (tmp_path/'escape').exists()


def test_self_consistent_untrusted_engine_is_rejected(delivery):
    root, _ = delivery
    (root/'feature_engine.py').write_text('raise RuntimeError("must never execute")')
    manifest = json.loads((root/'manifest.json').read_text())
    manifest['files']['feature_engine.py'] = hashlib.sha256((root/'feature_engine.py').read_bytes()).hexdigest()
    payload = encode(manifest)
    (root/'manifest.json').write_bytes(payload)
    with pytest.raises(ValueError, match='UNAPPROVED_DELIVERY_RUNTIME'):
        validate_delivery(root, hashlib.sha256(payload).hexdigest())


def test_tampered_report_rejected(delivery):
    root, sha = delivery
    (root/'validation_report.json').write_text('{}')
    with pytest.raises(ValueError, match='EVIDENCE_HASH_MISMATCH'):
        validate_delivery(root, sha)


def test_reused_weights_cannot_replace_lab_approval(delivery, tmp_path):
    root, sha = delivery
    target = tmp_path/'received'
    receive(target, archive(root), sha)
    approval = json.loads((root/'delivery.json').read_text())
    approval['lab_revision'] = 'd'*64
    (root/'delivery.json').write_bytes(encode(approval))
    with pytest.raises(ValueError, match='IDENTITY_CHANGED_EVIDENCE'):
        receive(target, archive(root), sha)


def test_multipart_api_receives_and_deduplicates_delivery(delivery, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.admin_strategy_integration import router
    from app.services.access_control_service import AccessContext, require_admin
    from app.strategy_integration.job_store import read_job
    root, sha = delivery
    jobs = tmp_path/'jobs'
    monkeypatch.setenv('PANJI_STRATEGY_JOB_ROOT', str(jobs))
    monkeypatch.setenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED','1')
    monkeypatch.setattr('app.strategy_integration.runtime_status.read_runtime_status',
                        lambda root: {'can_validate_delivery':True})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: AccessContext(
        user_id='delivery-unit',account_status='active',roles=['admin'],is_admin=True,
        is_member=False,subscription_active=True,default_route='/strategy',
        capabilities={'strategy_research':{'active':True}})
    client = TestClient(app)
    data = {'manifest_sha256':sha,'trade_date':'2026-10-16'}
    response = client.post('/v1/admin/strategy-research/zigzag/deliveries',data=data,
                           files={'file':('delivery.zip',archive(root),'application/zip')})
    assert response.status_code == 202, response.text
    job = read_job(jobs,response.json()['job_id'])
    assert job['kind'] == 'EVALUATE' and job['payload'] == {'delivery_id':sha,'trade_date':'2026-10-16'}
    again = client.post('/v1/admin/strategy-research/zigzag/deliveries',data=data,
                        files={'file':('delivery.zip',archive(root),'application/zip')})
    assert again.json()['job_id'] == job['job_id']
    from app.strategy_integration.lab_delivery import CHUNK_BYTES
    payload = archive(root)
    for index, offset in enumerate(range(0,len(payload),CHUNK_BYTES)):
        response = client.post('/v1/admin/strategy-research/zigzag/deliveries/chunks',
            data={**data,'chunk_index':str(index),'total_bytes':str(len(payload))},
            files={'file':('delivery.zip',payload[offset:offset+CHUNK_BYTES],'application/zip')})
        assert response.status_code == 202, response.text
        assert int(response.request.headers['content-length']) < 1024*1024
    assert response.json()['job_id'] == job['job_id']


def test_chunk_resume_and_proxy_sized_requests(delivery, tmp_path):
    from app.strategy_integration.lab_delivery import CHUNK_BYTES, receive_chunk
    root, sha = delivery
    target = tmp_path/'received'
    payload = archive(root)
    chunks = [payload[i:i+CHUNK_BYTES] for i in range(0,len(payload),CHUNK_BYTES)]
    assert len(chunks) > 1
    assert receive_chunk(target,chunks[0],sha,0,len(payload))['status'] == 'PART_ACCEPTED'
    assert not (target/sha).exists()
    assert receive_chunk(target,chunks[0],sha,0,len(payload))['status'] == 'PART_ACCEPTED'
    bad = bytes([chunks[0][0]^1])+chunks[0][1:]
    with pytest.raises(ValueError,match='IMMUTABLE_DELIVERY_CHUNK_CHANGED'):
        receive_chunk(target,bad,sha,0,len(payload))
    for i, part in enumerate(chunks[1:],1):
        result = receive_chunk(target,part,sha,i,len(payload))
    assert result['status'] == 'RECEIVED_NOT_ACTIVATED'
    assert (target/sha/'manifest.json').is_file()
    assert not (target/'.uploads'/sha).exists()
    assert receive_chunk(target,chunks[-1],sha,len(chunks)-1,len(payload)) == result
    with pytest.raises(ValueError,match='ARCHIVE_CHANGED'):
        receive_chunk(target,bad,sha,0,len(payload))
