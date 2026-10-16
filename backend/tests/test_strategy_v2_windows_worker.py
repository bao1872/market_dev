"""No network/PG/redis: verify frozen windows and canonical worker command."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import admin_strategy_integration as admin
from app.services.access_control_service import AccessContext, require_authenticated
from app.strategy_integration import rolling_windows as rw
from app.strategy_integration.worker import _train_cmd


def ctx(admin_role=True):
    return AccessContext(user_id='v2-test',account_status='active',
        roles=['admin'] if admin_role else ['member'], is_admin=admin_role,
        is_member=not admin_role, subscription_active=True,
        default_route='/strategy', capabilities={'strategy_research':{'active':True}})


def cfg(as_of='2026-10-16'):
    windows=rw.rolling_windows(date.fromisoformat(as_of))['windows']
    return {'as_of':as_of,'trees':300,**{
        name:{'start':windows[name][0],'end':windows[name][1]}
        for name in ('train','val','test')}}


def test_october_24_6_3_authoritative():
    val=cfg()
    assert val['train']=={'start':'2024-01-01','end':'2025-12-31'}
    assert val['val']=={'start':'2026-01-01','end':'2026-06-30'}
    assert val['test']=={'start':'2026-07-01','end':'2026-09-30'}


def test_canonical_train_cmd_not_diagnostic():
    cmd=_train_cmd('python',Path('/data/bars'),Path('/data/inst'),cfg(),
        Path('/out/job'),Path('/out/export'))
    assert '--as-of' in cmd and cmd[cmd.index('--as-of')+1]=='2026-10-16'
    assert '--source-mode' in cmd and cmd[cmd.index('--source-mode')+1]=='canonical'
    assert '--export-dir' in cmd
    assert '--train-start' not in cmd and '--test-end' not in cmd
    assert '--output-dir' in cmd


def test_cmd_cannot_ignore_custom_ui_windows():
    wrong=cfg();wrong['val']['start']='2025-01-01'
    with pytest.raises(RuntimeError,match='TRAIN_WINDOW_NOT_ROLLING'):
        _train_cmd('python',Path('/a'),Path('/b'),wrong,Path('/c'),Path('/d'))


def test_http_preview_and_training_are_fail_closed(monkeypatch):
    monkeypatch.delenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED',raising=False)
    monkeypatch.delenv('PANJI_STRATEGY_TRAIN_RUNTIME_READY',raising=False)
    app=FastAPI();app.include_router(admin.router)
    app.dependency_overrides[require_authenticated]=lambda:ctx()
    client=TestClient(app)
    assert client.post('/v1/admin/strategy-research/zigzag/train/preview',json=cfg()).status_code==200
    assert client.post('/v1/admin/strategy-research/zigzag/train-runs',json=cfg()).status_code==503
    mismatch=cfg();mismatch['test']['end']='2026-08-31'
    assert client.post('/v1/admin/strategy-research/zigzag/train-runs',json=mismatch).status_code==422
    app.dependency_overrides[require_authenticated]=lambda:ctx(False)
    assert client.post('/v1/admin/strategy-research/zigzag/train-runs',json=cfg()).status_code==403
