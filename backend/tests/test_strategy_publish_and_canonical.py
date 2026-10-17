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

from app.strategy_integration import cli, release_store, worker


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
    # STALE_TEST: the native 14-task path retains the authoritative factor
    # alongside MDAS QFQ/raw bars for decision-local PIT reconstruction.
    assert out_frames['SH:600000'] is pit['SH:600000']
    assert 'adj_factor' in out_frames['SH:600000'].columns
    assert 'adj_factor' in pit['SH:600000'].columns
    assert report['adjustment_mode'] == 'CANONICAL_MDAS_QFQ'
    assert report['factor_provenance_authoritative'] is True
    assert report['pit_factor_series_available'] is True
    assert len(report['source_sha256']) == 64 and int(report['source_sha256'], 16) >= 0


def _raise():
    raise AssertionError('Parquet export loader must not be called')


def test_admin_publish_rejects_legacy_model_even_if_historically_audited(monkeypatch, tmp_path):
    """STALE_TEST: old pickle activation is retired; historical reads remain covered below."""
    from app.api.admin_strategy_integration import router
    from app.services.access_control_service import AccessContext, require_admin
    # Deliberately invalid legacy publication input: reject before touching pointers.
    root = tmp_path/'releases'
    batch = root/'legacy'
    release_id, eval_sha = 'a'*64, 'b'*64
    release_store.atomic_json(batch/'manifest.json', dict(status='AUDITED',release_id=release_id,
        evaluation_sha256=eval_sha,bundle_path=str(tmp_path/'old.pkl'),runtime={'bundle_sha256':'c'*64}))
    monkeypatch.setenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED','1')
    monkeypatch.setenv('PANJI_STRATEGY_RELEASE_ROOT',str(root))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: AccessContext(
        user_id='test',account_status='active',roles=['admin'],is_admin=True,
        is_member=False,subscription_active=True,default_route='/admin',capabilities={'strategy_research':{'active':True}})
    response = TestClient(app).post(f'/v1/admin/strategy-research/zigzag/model-releases/{release_id}/publish',
        json={'release_id':release_id,'candidate_report_sha256':eval_sha})
    assert response.status_code == 409
    assert 'PRODUCTION_FOURTEEN_PACKAGE_REQUIRED' in response.json()['detail']
    assert not (root/'active_release.json').exists()


_SRC_BATCH = Path('/tmp/panji_14_snapshot/2026-08-17_89a5be0b28d1dac9')


def _make_audited_batch(root: Path) -> Path:
    if not _SRC_BATCH.is_dir():
        pytest.skip('historical 14-stock batch not present')
    batch = root / _SRC_BATCH.name
    shutil.copytree(_SRC_BATCH, batch)
    manifest = json.loads((batch / 'manifest.json').read_text())
    manifest['status'] = 'AUDITED'
    (batch / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
    return batch


def test_activate_batch_json_write_failure_keeps_previous_state(tmp_path, monkeypatch):
    # Req 4: a failure while writing the authoritative active_release.json must
    # leave the previously published state fully intact (fail-closed). The snapshot
    # pointer must NOT be switched while the model pointer is missing.
    bundle = Path('/tmp/panji_14_global/two_stage_models_candidate_UNPUBLISHED.pkl')
    # Seed a previous, consistent published state in the target root.
    prev = release_store.write_active_release(
        tmp_path, tmp_path / 'prev_batch', 'P' * 64, None, str(bundle))
    assert json.loads(prev.read_text())['release_id'] == 'P' * 64

    batch = _make_audited_batch(tmp_path)
    # Force the authoritative JSON write to fail (simulated interruption).
    monkeypatch.setattr(release_store, 'atomic_json',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('DISK')))

    with pytest.raises(RuntimeError):
        release_store.activate_batch(tmp_path, batch)
    # Previous state untouched; current symlink never switched to the new batch.
    assert json.loads((tmp_path / 'active_release.json').read_text())['release_id'] == 'P' * 64
    current = tmp_path / 'current'
    assert not current.is_symlink() or Path(current).resolve().name != batch.name


def test_activate_batch_symlink_failure_still_authoritative(tmp_path, monkeypatch):
    # Req 4: if the legacy `current` symlink refresh fails, the publish is still
    # consistent because active_release.json is the authoritative state.
    batch = _make_audited_batch(tmp_path)
    monkeypatch.setattr(release_store, '_refresh_current_symlink',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('symlink-broken')))

    result = release_store.activate_batch(tmp_path, batch)
    assert result['status'] == 'ACTIVATED_AFTER_OPERATOR_APPROVAL'
    active = release_store.resolve_active_release(tmp_path)
    assert active['release_id'] == json.loads(
        (batch / 'manifest.json').read_text())['release_id']
    assert active['batch'] == str(batch.resolve())


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
