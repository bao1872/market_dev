"""Req 1/2/3 acceptance: publish unification, worker per-symbol registry wiring,
and the canonical mode reading MDAS directly (no Parquet round-trip).

These tests consume production encoders/helpers. They are tagged pure_unit so the
local PURE_UNIT_TEST gate collects and runs them; none touches a database, model
weights, or the production release root.
"""
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.strategy_integration import release_store
from app.strategy_integration import cli
from app.strategy_integration import worker


def test_predict_cmd_wires_personal_registry(monkeypatch):
    # Req 2: the daily worker must read the activated per-symbol model registry
    # when configured, and must NOT impersonate a personal model otherwise.
    base = worker._predict_cmd('py', '/b', '/i', '2026-08-17', '/e', '/s')
    assert '--source-mode' in base and 'canonical' in base
    assert '--personal-registry' not in base
    wired = worker._predict_cmd('py', '/b', '/i', '2026-08-17', '/e', '/s',
                                registry='/abs/registry.json')
    assert '--personal-registry' in wired and '/abs/registry.json' in wired
    # A relative / empty registry must be ignored (fail-closed to GLOBAL_ONLY).
    ignored = worker._predict_cmd('py', '/b', '/i', '2026-08-17', '/e', '/s',
                                  registry='relative/registry.json')
    assert '--personal-registry' not in ignored


def test_write_active_release_file_verification(tmp_path):
    # Req 1: the publish record must be file-verified; a missing or tampered
    # model artifact must never be recorded.
    bundle = tmp_path / 'model.pkl'
    bundle.write_bytes(b'weights')
    ok = release_store.write_active_release(
        tmp_path, tmp_path / 'batch', 'R' * 64,
        bundle_sha256=release_store.digest_path(bundle), bundle_path=str(bundle))
    rec = json.loads(ok.read_text())
    assert rec['schema'] == 'panji-strategy-active-release-v1'
    assert rec['release_id'] == 'R' * 64
    assert rec['bundle_path'] == str(bundle)
    assert rec['bundle_sha256'] == release_store.digest_path(bundle)

    with pytest.raises(release_store.PublishError):
        release_store.write_active_release(
            tmp_path, tmp_path / 'batch', 'R' * 64, 'deadbeef' * 8, str(bundle))
    missing = tmp_path / 'nope.pkl'
    with pytest.raises(release_store.PublishError):
        release_store.write_active_release(
            tmp_path, tmp_path / 'batch', 'R' * 64, None, str(missing))


def test_resolve_release_batch_finds_by_release_id(tmp_path):
    (tmp_path / '2026-08-17_abc').mkdir()
    (tmp_path / '2026-08-17_abc' / 'manifest.json').write_text(
        json.dumps({'release_id': 'X' * 64}))
    (tmp_path / 'other').mkdir()
    (tmp_path / 'other' / 'manifest.json').write_text(
        json.dumps({'release_id': 'Y' * 64}))
    assert release_store.resolve_release_batch(tmp_path, 'X' * 64).name == '2026-08-17_abc'
    assert release_store.resolve_release_batch(tmp_path, 'Z' * 64) is None
    # Never follows the current symlink.
    (tmp_path / 'link').symlink_to(tmp_path / '2026-08-17_abc', target_is_directory=True)
    assert release_store.resolve_release_batch(tmp_path, 'X' * 64).name == '2026-08-17_abc'


def test_canonical_mode_reads_mdas_directly(monkeypatch):
    # Req 3: the canonical source mode must read MDAS directly and must NOT go
    # through the legacy Parquet export loader (no OFFLINE_FACTOR_DIAGNOSTIC).
    from app.strategy_integration import source as source_mod

    frames = {
        'SH:600000': pd.DataFrame({
            'datetime': pd.date_range('2026-01-01', periods=200, freq='D'),
            'open': 1.0, 'high': 1.1, 'low': 0.9, 'close': 1.0, 'volume': 1e6,
            'adj_factor': 1.0,
        }),
    }
    mdas_report = {
        'eligible_stock_count': 14, 'coverage_ratio_of_eligible': 0.9,
        'pit_factor_series_available': True, 'price_adjustment': 'qfq',
        'adjustment_as_of': '2026-08-17',
        'provenance': {'SH:600000': {'source_bar_hash': 'a' * 64,
                                     'adj_factor_hash': 'b' * 64}},
    }
    async def fake_read(*a, **k):
        return frames, mdas_report
    monkeypatch.setattr(
        'app.strategy_integration.db_export.read_official_qfq_stock_batch', fake_read)
    sentinel = []
    monkeypatch.setattr(source_mod, 'load_canonical_qfq_export',
                        lambda *a, **k: sentinel.append(1) or _raise())

    out_frames, pit, report = cli._load_market(
        SimpleNamespace(source_mode='canonical', as_of='2026-08-17', max_symbols=80), None)
    assert not sentinel, 'canonical build must not use the Parquet export loader'
    assert 'SH:600000' in out_frames
    assert 'adj_factor' not in out_frames['SH:600000'].columns
    assert 'adj_factor' in pit['SH:600000'].columns
    assert report['adjustment_mode'] == 'CANONICAL_MDAS_QFQ'
    assert report['factor_provenance_authoritative'] is True
    assert report['pit_factor_series_available'] is True
    assert len(report['source_sha256']) == 64 and int(report['source_sha256'], 16) >= 0


def _raise():
    raise AssertionError('Parquet export loader must not be called')


def test_admin_publish_unifies_with_worker_path(monkeypatch):
    # Req 1: the admin publish path must produce the SAME complete, file-verified
    # active release record the worker uses, and must swap the snapshot pointer
    # atomically (model pointer + snapshot pointer can never diverge).
    from app.services.access_control_service import AccessContext, require_admin
    from app.api.admin_strategy_integration import router

    from app.strategy_integration import db_export

    src_batch = Path('/tmp/panji_14_snapshot/2026-08-17_89a5be0b28d1dac9')
    if not src_batch.is_dir():
        pytest.skip('historical 14-stock batch not present')
    root = Path(tempfile.mkdtemp(prefix='panji_publish_'))
    batch = root / src_batch.name
    shutil.copytree(src_batch, batch)
    manifest = json.loads((batch / 'manifest.json').read_text())
    manifest['status'] = 'AUDITED'
    (batch / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
    release_id = manifest['release_id']
    eval_sha = manifest['evaluation_sha256']

    monkeypatch.setenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED', '1')
    monkeypatch.setenv('PANJI_STRATEGY_RELEASE_ROOT', str(root))
    # Guard the only DB-touching call; the canonical path is not exercised here.
    monkeypatch.setattr(db_export, 'read_official_qfq_stock_batch',
                        lambda *a, **k: ({}, {}))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: AccessContext(
        user_id='test', account_status='active', roles=['admin'], is_admin=True,
        is_member=False, subscription_active=True, default_route='/admin',
        capabilities={'strategy_research': {'active': True}})
    client = TestClient(app)
    resp = client.post(
        f'/v1/admin/strategy-research/zigzag/model-releases/{release_id}/publish',
        json={'release_id': release_id, 'candidate_report_sha256': eval_sha})
    assert resp.status_code == 200, resp.text
    assert resp.json()['status'] == 'RELEASE_ACTIVATED'

    # Snapshot pointer (current) and model pointer (active_release.json) agree.
    current = root / 'current'
    assert current.is_symlink() and Path(current).resolve() == batch.resolve()
    active = json.loads((root / 'active_release.json').read_text())
    assert active['release_id'] == release_id
    assert active['schema'] == 'panji-strategy-active-release-v1'
    assert active['bundle_path'] == manifest['bundle_path']
    assert active['bundle_sha256'] == manifest['runtime']['bundle_sha256']
    assert active['batch'] == str(batch.resolve())
    assert Path(active['bundle_path']).is_file()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
