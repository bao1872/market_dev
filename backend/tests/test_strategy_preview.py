"""Admin-only candidate selection is scoped to a request, never an active pointer."""
import hashlib

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from app.services.access_control_service import require_authenticated
from app.services.strategy_preview import select_preview
from app.strategy_integration.release_store import atomic_json, published_snapshot_path, resolve_preview_batch, PublishError
from app.services.access_control_service import AccessContext


def ctx(admin):
    return AccessContext(user_id='test', account_status='active', roles=['admin'] if admin else ['member'],
        is_admin=admin, is_member=not admin, subscription_active=True,
        default_route='/strategy', capabilities={'strategy_research':{'active':True}})


@pytest.fixture
def batch(tmp_path, monkeypatch):
    root=tmp_path/'release';folder=root/'candidate';folder.mkdir(parents=True)
    fields={}
    for name,key in [('candidate_snapshot.json','candidate_sha256'),('evaluation_snapshot.json','evaluation_sha256')]:
        atomic_json(folder/name, {'test':'path selection only'})
        fields[key]=hashlib.sha256((folder/name).read_bytes()).hexdigest()
    identity='a'*64
    atomic_json(folder/'manifest.json', {'status':'VALIDATED_STAGING_NOT_DEPLOYED','batch_id':identity,**fields})
    monkeypatch.setenv('PANJI_STRATEGY_RELEASE_ROOT',str(root))
    return root,folder,identity


def test_preview_requires_admin_and_never_leaks_between_requests(batch):
    root,folder,identity=batch
    app=FastAPI()
    @app.get('/read',dependencies=[Depends(select_preview)])
    def read():
        return {'path':str(published_snapshot_path('candidate_snapshot.json','/missing'))}
    app.dependency_overrides[require_authenticated]=lambda:ctx(False)
    client=TestClient(app)
    assert client.get('/read',params={'preview_batch':identity}).status_code==403
    app.dependency_overrides[require_authenticated]=lambda:ctx(True)
    assert client.get('/read',params={'preview_batch':identity}).json()['path']==str(folder/'candidate_snapshot.json')
    assert client.get('/read').json()['path']=='/missing'
    assert client.get('/read',params={'preview_batch':'../candidate'}).status_code==422
    assert not (root/'active_release.json').exists()


def test_preview_rejects_artifact_mutation(batch):
    root,folder,identity=batch
    atomic_json(folder/'evaluation_snapshot.json',{'tampered':True})
    with pytest.raises(PublishError,match='HASH_MISMATCH'):
        resolve_preview_batch(root,identity)
