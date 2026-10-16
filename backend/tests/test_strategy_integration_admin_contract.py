"""API admin controls are RBAC protected and writes disabled before deploy."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.admin_strategy_integration import router
from app.services.access_control_service import AccessContext, require_authenticated


def ctx(admin):
    return AccessContext(user_id='test',account_status='active',roles=['admin'] if admin else ['member'],
        is_admin=admin,is_member=not admin,subscription_active=True,
        default_route='/admin/overview' if admin else '/strategy',capabilities={'strategy_research':{'active':True}})


def test_admin_write_controls_fail_closed_before_deploy(monkeypatch):
    monkeypatch.delenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED',raising=False)
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[require_authenticated]=lambda:ctx(True)
    client=TestClient(app)
    req={'as_of':'2026-10-16','train':{'start':'2024-01-01','end':'2025-12-31'},
         'val':{'start':'2026-01-01','end':'2026-06-30'},
         'test':{'start':'2026-07-01','end':'2026-09-30'},'trees':300}
    assert client.post('/v1/admin/strategy-research/zigzag/train/preview',json=req).status_code==200
    assert client.post('/v1/admin/strategy-research/zigzag/train-runs',json=req).status_code==503
    app.dependency_overrides[require_authenticated]=lambda:ctx(False)
    assert client.post('/v1/admin/strategy-research/zigzag/train-runs',json=req).status_code==403
