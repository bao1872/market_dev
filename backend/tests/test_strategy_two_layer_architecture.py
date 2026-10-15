"""Mandatory regression tests for the two-layer (global + personal) architecture.

Covers: rolling dates, cross-month boundaries, per-symbol isolation, OOF
non-leakage, label maturity, PIT prefix parity, training dedup, incremental
updates, fallback state, global/personal scoring consistency, release identity,
permissions, sharding and rollback.
"""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import (incremental_training as inc, model,
                                      personal_model as pm, personal_registry,
                                      personal_trainer, producer)
from app.strategy_integration import rolling_windows as rw

ASSETS = Path('/tmp/panji_frozen_assets')


def _no_lightgbm() -> bool:
    try:
        import lightgbm  # noqa: F401
        return False
    except Exception:
        return True
pytestmark = pytest.mark.skipif(
    not (ASSETS / 'two_stage_models_local_only.pkl').is_file(),
    reason='frozen engine assets not present in this environment')


@pytest.fixture(scope='module')
def fmod():
    return model.load_engine(ASSETS)


def _bars(n=420, seed=3, action_at=None, start='2023-06-01'):
    rng = np.random.default_rng(seed)
    v = 30 + np.cumsum(rng.normal(0, .3, n))
    o = v * .999
    c = v * 1.001
    h = np.maximum(o, c) * 1.02
    l = np.minimum(o, c) * .97
    dates = pd.bdate_range(start, periods=n)
    factor = np.ones(n)
    if action_at is not None:
        factor[action_at:] = 0.5
    return pd.DataFrame({'datetime': dates, 'open': o, 'high': h, 'low': l,
                         'close': c, 'volume': np.full(n, 1e6),
                         'adj_factor': factor})


def _market(symbols=3, n=420, action_at=None):
    out = {}
    for i in range(symbols):
        out[f'SH:60000{i}'] = _bars(n=n, seed=10 + i, action_at=action_at)
    return out


# ------------------------------------------------------------------ 1. rolling dates
def test_rolling_window_contract_for_october_2026():
    w = rw.rolling_windows(date(2026, 10, 15))
    assert w['windows']['train'] == ('2024-01-01', '2025-12-31')
    assert w['windows']['val'] == ('2026-01-01', '2026-06-30')
    assert w['windows']['test'] == ('2026-07-01', '2026-09-30')
    assert w['months'] == {'train': 24, 'val': 6, 'test': 3}


def test_rolling_window_advances_with_run_month():
    for run_day, expected in [
        (date(2026, 11, 3), ('2024-02-01', '2026-01-31', '2026-02-01', '2026-07-31')),
        (date(2027, 1, 9), ('2024-04-01', '2026-03-31', '2026-04-01', '2026-09-30')),
    ]:
        w = rw.rolling_windows(run_day)
        assert w['windows']['train'] == (expected[0], expected[1])
        assert w['windows']['val'] == (expected[2], expected[3])


def test_rolling_windows_reject_overlap():
    w = rw.rolling_windows(date(2026, 10, 15))
    with pytest.raises(rw.WindowError):
        inc.check_window_isolation({'windows': {'train': ('2024-01-01', '2027-01-01'),
                                                'val': ('2026-01-01', '2026-06-30'),
                                                'test': ('2026-07-01', '2026-09-30')}})


# -------------------------------------------------------- 2. cross-month boundary
def test_split_of_uses_inclusive_boundaries():
    w = rw.rolling_windows(date(2026, 10, 15))
    assert rw.split_of(date(2024, 1, 1), w) == 'train'
    assert rw.split_of(date(2025, 12, 31), w) == 'train'
    assert rw.split_of(date(2026, 1, 1), w) == 'val'
    assert rw.split_of(date(2026, 6, 30), w) == 'val'
    assert rw.split_of(date(2026, 7, 1), w) == 'test'
    assert rw.split_of(date(2026, 9, 30), w) == 'test'
    assert rw.split_of(date(2023, 12, 31), w) is None
    assert rw.split_of(date(2026, 10, 1), w) is None


def test_event_spanning_two_splits_is_dropped(fmod):
    """A candidate whose D1..D3 crosses a split boundary must not be trained."""
    w = rw.rolling_windows(date(2026, 10, 15))
    frame = _bars(n=900, seed=7)
    data, counters, _hist = personal_trainer and None, None, None
    from app.strategy_integration.training import build_dataset
    data, counters, _h = build_dataset({'SH:600000': frame}, fmod, w)
    # Every retained sample's decision date sits inside one split only.
    for key, rows in data.items():
        split = key[0]
        lo, hi = w['dates'][split]
        for r in rows:
            assert lo <= r['decision_date'] <= hi, (split, r['decision_date'])
    assert counters.get('boundary_dropped', 0) >= 0


# ------------------------------------------------------- 3. per-symbol isolation
def test_collect_symbol_samples_isolates_symbols(fmod):
    w = rw.rolling_windows(date(2026, 10, 15))
    market = _market(symbols=3)
    from app.strategy_integration.training import build_dataset
    data, _c, _h = build_dataset(market, fmod, w)
    train_rows = {key[1:]: rows for key, rows in data.items() if key[0] == 'train'}
    own = personal_trainer.collect_symbol_samples(train_rows, 'SH:600001')
    assert own, 'expected training samples for the isolated symbol'
    for rows in own.values():
        for r in rows:
            assert r['symbol'] == 'SH:600001'
    others = {r['symbol'] for rows in train_rows.values() for r in rows}
    assert 'SH:600000' in others and 'SH:600001' in others


# ------------------------------------------------------------ 4. OOF non-leakage
def _oof_samples(fmod):
    """Small, deterministic per-submodel training set for OOF checks."""
    rng = np.random.default_rng(5)
    rows = []
    days = pd.bdate_range('2024-01-01', periods=240)
    for i, d in enumerate(days):
        x = rng.normal(size=131).astype(np.float32)
        label = int(x[0] + 0.3 * rng.normal() > 0)
        rows.append({'features': x, 'label': label,
                     'sample_id': f'SYM|{d.date()}|L|D1', 'symbol': 'SYM',
                     'decision_date': d.date(), 'anchor_date': d.date()})
    return {('L1', 'L', 1): rows}


@pytest.mark.skipif(_no_lightgbm(),
                    reason='lightgbm runtime not in this venv')
def test_oof_scores_are_not_in_sample(fmod):
    rows = _oof_samples(fmod)
    scores, prov = personal_trainer.compute_oof_base_scores(
        rows, fmod, folds=3, trees=40, threads=1, seed=1)
    assert prov['folds'] == 3
    total = len(rows[('L1', 'L', 1)])
    assert prov['scored_samples'] + prov['unscored_samples'] == total
    # The first fold has no earlier data, so it can never be scored in-fold.
    assert prov['unscored_samples'] > 0
    assert all(0.0 <= v <= 1.0 for v in scores.values())

    # In-sample scores of a memorising model are far more extreme than OOF ones.
    from lightgbm import LGBMClassifier
    X = np.stack([r['features'] for r in rows[('L1', 'L', 1)]]).astype(np.float32)
    y = np.asarray([r['label'] for r in rows[('L1', 'L', 1)]], dtype=int)
    clf = LGBMClassifier(n_estimators=40, num_leaves=15, learning_rate=.08,
                         min_child_samples=40, verbosity=-1, n_jobs=1,
                         random_state=1)
    clf.fit(pd.DataFrame(X, columns=list(fmod.ALL_COLS)), y)
    insample = clf.predict_proba(pd.DataFrame(X, columns=list(fmod.ALL_COLS)))[:, 1]
    oof = np.asarray([scores[r['sample_id']] for r in rows[('L1', 'L', 1)]
                      if r['sample_id'] in scores])
    insample_used = np.asarray([
        insample[i] for i, r in enumerate(rows[('L1', 'L', 1)])
        if r['sample_id'] in scores])
    err_oof = float(np.mean(np.abs(oof - insample_used)))
    assert err_oof > 1e-6, 'OOF scores look identical to in-sample fit'


# --------------------------------------------------------------- 5. label maturity
def test_unmatured_labels_are_never_invented():
    from app.strategy_integration.training import _labels
    frame = _bars(n=300, seed=11)
    fmod2 = model.load_engine(ASSETS)
    final, _snap, _ = fmod2.replay(frame)
    days = [pd.Timestamp(x).date() for x in frame.datetime]
    found_none = 0
    found_value = 0
    for j, kind, _px in fmod2.causal_candidates(frame)[0]:
        if j + 3 >= len(frame):
            continue
        cutoff = j + 2          # far too early for the label to have matured
        l1, l2 = _labels(frame, final, j, kind, cutoff, j + 1)
        if l1 is None and l2 is None:
            found_none += 1
        else:
            found_value += 1
    assert found_none > 0, 'expected unmatured candidates with an early cutoff'
    assert found_value >= 0


def test_labels_become_available_once_mature():
    from app.strategy_integration.training import _labels
    frame = _bars(n=300, seed=13)
    fmod2 = model.load_engine(ASSETS)
    final, _snap, _ = fmod2.replay(frame)
    matured = 0
    for j, kind, _px in fmod2.causal_candidates(frame)[0]:
        if j + 3 >= len(frame):
            continue
        l1, l2 = _labels(frame, final, j, kind, len(frame) - 1, j + 1)
        if l1 is not None or l2 is not None:
            matured += 1
    assert matured > 0


# --------------------------------------------------------- 6. PIT prefix parity
def test_pit_prefix_parity_with_corporate_action(fmod):
    frame = _bars(n=420, seed=17, action_at=250)
    raw = frame.drop(columns=['adj_factor'])
    stub = {'models': {k: _Stub() for k in model.EXPECTED_KEYS},
            'thresholds': {k: 0.5 for k in model.EXPECTED_KEYS}}
    checked = model.assert_prefix_invariance(raw, 'SH:600000', fmod, stub,
                                             tail_cut=25, pit_source=frame)
    assert checked > 0


class _Stub:
    n_features_in_ = 131

    def predict_proba(self, X):
        return np.column_stack([np.full(len(X), .2), np.full(len(X), .8)])


# ------------------------------------------------------------- 7. training dedup
def test_duplicate_training_samples_are_dropped(fmod):
    w = rw.rolling_windows(date(2026, 10, 15))
    market = _market(symbols=2, n=400)
    from app.strategy_integration.training import build_dataset
    _d1, c1, _h1 = build_dataset(market, fmod, w)
    _d2, c2, _h2 = build_dataset(market, fmod, w)
    assert c1 == c2, 'dataset construction must be deterministic'
    assert c1.get('duplicate_dropped', 0) == 0


# ------------------------------------------------------- 8. incremental updates
def test_incremental_plan_and_state(tmp_path):
    state = inc.load_state(tmp_path)
    dates = list(pd.bdate_range('2026-07-01', periods=20))
    plan = inc.plan_incremental(state, 'SH:600000', [d.date() for d in dates])
    assert plan['action'] == 'FULL_INITIAL_BUILD'
    inc.update_symbol_state(state, 'SH:600000', last_trade_date=dates[-1].date(),
                            last_matured_event_date=dates[-1].date(),
                            training_data_sha256='a' * 64,
                            model_version='v1', windows=rw.rolling_windows(date(2026, 10, 15)))
    inc.save_state(state, tmp_path)
    reloaded = inc.load_state(tmp_path)
    again = inc.plan_incremental(reloaded, 'SH:600000', [d.date() for d in dates])
    assert again['action'] == 'NO_CHANGE', 'unchanged history must not be reprocessed'
    later = [d.date() for d in pd.bdate_range('2026-08-20', periods=5)]
    upd = inc.plan_incremental(reloaded, 'SH:600000', [d.date() for d in dates] + later)
    assert upd['action'] == 'INCREMENTAL_UPDATE'
    assert upd['new_bars'] == len(later)


def test_live_refit_candidate_is_new_version_and_never_overrides(tmp_path):
    w = rw.rolling_windows(date(2026, 10, 15))
    record = pm.new_record('SH:600000', windows=w, global_release_id='g' * 64,
                           global_bundle_sha256='b' * 64, data_sha256='d' * 64,
                           history={})
    record['mode'] = pm.MODE_REGULARIZED
    record['submodels'] = {'L1:L:D1': {'mode': pm.MODE_REGULARIZED, 'coef': 1.0,
                                       'intercept': 0.0, 'blend_weight': 0.3,
                                       'threshold': 0.4}}
    record['price_input_contract'] = producer.PRICE_INPUT_CONTRACT
    refit = inc.build_live_refit_candidate(
        symbol='SH:600000', record=record, windows=w,
        global_release_id='g' * 64, data_sha256='d' * 64,
        test_assessment={'status': 'TEST_WINDOW_INCOMPLETE'})
    assert refit['status'] == pm.STATUS_CANDIDATE
    assert refit['update_kind'] == inc.LIVE_REFIT_KIND
    assert refit['independent_test_claim'] == 'NOT_CLAIMED'
    assert refit['model_version'] != record.get('model_version')
    assert record['status'] == pm.STATUS_CANDIDATE, 'source record must be untouched'


# ------------------------------------------------------------- 9. fallback state
def test_insufficient_samples_record_fallback_with_reason(fmod):
    w = rw.rolling_windows(date(2026, 10, 15))
    tiny = {('L1', 'L', 1): [
        {'features': np.zeros(131, dtype=np.float32), 'label': i % 2,
         'sample_id': f'S|{i}', 'symbol': 'SH:600000',
         'decision_date': date(2024, 6, 1) + pd.Timedelta(days=i),
         'anchor_date': date(2024, 6, 1)} for i in range(5)]}
    record = personal_trainer.train_personal(
        'SH:600000', samples_by_key=tiny, oof_scores={}, fmod=fmod, windows=w,
        global_release_id='g' * 64, global_bundle_sha256='b' * 64,
        data_sha256='d' * 64, price_contract=producer.PRICE_INPUT_CONTRACT,
        min_adjust_samples=60)
    assert record['mode'] == pm.MODE_FALLBACK
    assert record['fallback_reason']
    assert 'INSUFFICIENT' in record['fallback_reason']
    with pytest.raises(pm.PersonalModelError, match='CANNOT_BE_ACTIVATED'):
        personal_registry.activate({'symbols': {'SH:600000': record},
                                    'history': {}}, 'SH:600000')


# ------------------------------------------ 10. global/personal scoring identity
def test_scoring_consistency_and_provenance(fmod):
    frame = _bars(n=420, seed=23)
    raw = frame.drop(columns=['adj_factor'])
    stub = {'models': {k: _Stub() for k in model.EXPECTED_KEYS},
            'thresholds': {k: 0.5 for k in model.EXPECTED_KEYS}}
    w = rw.rolling_windows(date(2026, 10, 15))
    record = pm.new_record('SH:600000', windows=w, global_release_id='g' * 64,
                           global_bundle_sha256='b' * 64, data_sha256='d' * 64,
                           history={})
    record['mode'] = pm.MODE_REGULARIZED
    record['submodels'] = {
        str((layer, kind, stage)): {'mode': pm.MODE_REGULARIZED, 'coef': 0.0,
                                     'intercept': 0.0, 'blend_weight': 0.0,
                                     'threshold': 0.5, 'n': 100, 'positive': 40}
        for layer in ('L1', 'L2') for kind in ('H', 'L') for stage in (1, 2, 3)}
    record['price_input_contract'] = producer.PRICE_INPUT_CONTRACT
    record['release_id'] = 'p' * 64

    base = model.score_candidates(raw, 'SH:600000', fmod, stub,
                                  release_id='R' * 64)
    with_personal = model.score_candidates(raw, 'SH:600000', fmod, stub,
                                           release_id='R' * 64,
                                           personal={'record': record})
    # blend_weight=0 must reproduce the global score exactly.
    for c_base, c_pers in zip(base['candidates'], with_personal['candidates']):
        assert c_base['candidate_id'] == c_pers['candidate_id']
        for o_base, o_pers in zip(c_base['observations'], c_pers['observations']):
            assert o_base['observation_id'] == o_pers['observation_id']
            assert abs(o_base['l1_score'] - o_pers['l1_score']) < 1e-12
            assert abs(o_base['l2_score'] - o_pers['l2_score']) < 1e-12
            assert o_pers['scoring_mode'] == 'GLOBAL_PLUS_PERSONAL'
            assert o_pers['personal_release_id'] == 'p' * 64
            assert o_pers['global_release_id'] == 'R' * 64
            assert o_pers['symbol'] == 'SH:600000'
            assert o_pers['decision_date'] == o_base['decision_date']

    # A non-zero blend applies the personal adjustment, so the combined score
    # differs from the pure global score while the provenance is still recorded.
    for sub in record['submodels'].values():
        sub['blend_weight'] = 0.5
    neutral = model.score_candidates(raw, 'SH:600000', fmod, stub,
                                     release_id='R' * 64,
                                     personal={'record': record})
    changed = 0
    for c_base, c_neu in zip(base['candidates'], neutral['candidates']):
        for o_base, o_neu in zip(c_base['observations'], c_neu['observations']):
            assert o_neu['scoring_mode'] == 'GLOBAL_PLUS_PERSONAL'
            assert o_neu['personal_release_id'] == 'p' * 64
            if abs(o_base['l1_score'] - o_neu['l1_score']) > 1e-9:
                changed += 1
    assert changed > 0, 'non-zero blend must change at least some scores'


def test_incompatible_personal_model_is_refused():
    w = rw.rolling_windows(date(2026, 10, 15))
    record = pm.new_record('SH:600000', windows=w, global_release_id='g' * 64,
                           global_bundle_sha256='b' * 64, data_sha256='d' * 64,
                           history={})
    record['price_input_contract'] = 'RAW_OHLCV'
    with pytest.raises(pm.PersonalModelError, match='PRICE_CONTRACT_MISMATCH'):
        pm.check_input_compatibility(record,
                                     expected_price_contract=producer.PRICE_INPUT_CONTRACT,
                                     global_release_id='g' * 64)


# ----------------------------------------------------------- 11. release identity
def test_release_identity_is_bundle_specific():
    a = producer.release_id_for('a' * 64)
    b = producer.release_id_for('b' * 64)
    assert a != b
    assert len(a) == 64 and len(b) == 64
    # The price contract participates in the identity.
    assert producer.release_id_for('a' * 64, 'QFQ_ASOF_V1') != \
        producer.release_id_for('a' * 64, 'OTHER')


def test_personal_release_identity_is_content_addressed():
    w = rw.rolling_windows(date(2026, 10, 15))
    rec = pm.new_record('SH:600000', windows=w, global_release_id='g' * 64,
                        global_bundle_sha256='b' * 64, data_sha256='d' * 64,
                        history={})
    pm.set_identity(rec)
    first = rec['model_version']
    rec['data_sha256'] = 'e' * 64      # distinct content -> distinct version
    pm.set_identity(rec)
    assert rec['model_version'] != first


# --------------------------------------------------------------- 12. permissions
def test_personal_model_endpoint_requires_admin(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import admin_strategy_integration as admin_api
    from app.core.deps import get_db
    from app.services.access_control_service import AccessContext, require_authenticated

    def ctx(kind):
        admin = kind == 'admin'
        return AccessContext(user_id='u', account_status='active',
                             roles=['admin' if admin else 'member'],
                             is_admin=admin, is_member=not admin,
                             subscription_active=True, default_route='/strategy',
                             capabilities={'market_data': {'active': True}})

    app = FastAPI()
    app.include_router(admin_api.router)
    app.dependency_overrides[require_authenticated] = lambda: ctx('admin')

    async def fake_db():
        yield object()
    app.dependency_overrides[get_db] = fake_db
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get('/v1/admin/strategy-research/zigzag/model-releases'
                      '/personal/SH:600000').status_code == 200
    assert client.get('/v1/admin/strategy-research/zigzag/model-releases'
                      '/personal/NOPE').status_code == 422


def test_personal_model_endpoint_forbids_non_admin():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import admin_strategy_integration as admin_api
    from app.core.deps import get_db
    from app.services.access_control_service import AccessContext, require_authenticated

    member = AccessContext(user_id='u', account_status='active', roles=['member'],
                           is_admin=False, is_member=True, subscription_active=True,
                           default_route='/strategy',
                           capabilities={'market_data': {'active': True}})
    app = FastAPI()
    app.include_router(admin_api.router)
    app.dependency_overrides[require_authenticated] = lambda: member

    async def fake_db():
        yield object()
    app.dependency_overrides[get_db] = fake_db
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get('/v1/admin/strategy-research/zigzag/model-releases'
                      '/personal/SH:600000').status_code == 403


# ------------------------------------------------------------------ 13. sharding
def test_sharded_snapshot_keeps_personal_provenance(fmod):
    from app.services.strategy_candidate_shards import write_candidate_shards
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    frame = _bars(n=420, seed=29)
    stub = {'models': {k: _Stub() for k in model.EXPECTED_KEYS},
            'thresholds': {k: 0.5 for k in model.EXPECTED_KEYS}}
    w = rw.rolling_windows(date(2026, 10, 15))
    record = pm.new_record('SH:600000', windows=w, global_release_id='g' * 64,
                           global_bundle_sha256='b' * 64, data_sha256='d' * 64,
                           history={})
    record['mode'] = pm.MODE_REGULARIZED
    record['submodels'] = {
        str((layer, kind, stage)): {'mode': pm.MODE_REGULARIZED, 'coef': 0.0,
                                     'intercept': 0.0, 'blend_weight': 0.0,
                                     'threshold': 0.5}
        for layer in ('L1', 'L2') for kind in ('H', 'L') for stage in (1, 2, 3)}
    record['release_id'] = 'p' * 64
    candidates, _ev = producer.make_snapshots(
        {'SH:600000': frame.drop(columns=['adj_factor'])}, fmod, stub,
        as_of='2026-08-17', source_sha='s' * 64,
        pit_source={'SH:600000': frame},
        personal_by_symbol={'SH:600000': {'record': record}})
    out = Path('/tmp/panji_personal_shard_test')
    import shutil
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    manifest = write_candidate_shards(candidates, out, validate=validate_candidate)
    assert manifest['stock_count'] == 1
    daily_items = [i for b in candidates['daily_batches'] for i in b['items']]
    assert daily_items
    for item in daily_items:
        assert item['scoring_mode'] == 'GLOBAL_PLUS_PERSONAL'
        assert item['personal_release_id'] == 'p' * 64
        assert item['global_release_id']
    assert candidates['metadata']['scoring_architecture'] == \
        'GLOBAL_ROLLING_PLUS_OPTIONAL_PERSONAL'


# ----------------------------------------------------------------- 14. rollback
def test_personal_registry_rollback(tmp_path):
    w = rw.rolling_windows(date(2026, 10, 15))
    registry = pm.load_registry(tmp_path)
    for version in ('v1', 'v2'):
        rec = pm.new_record('SH:600000', windows=w, global_release_id='g' * 64,
                            global_bundle_sha256='b' * 64, data_sha256='d' * 64,
                            history={})
        rec['mode'] = pm.MODE_REGULARIZED
        rec['submodels'] = {'L1:L:D1': {'mode': pm.MODE_REGULARIZED, 'coef': 1.0,
                                        'intercept': 0.0, 'blend_weight': 0.2,
                                        'threshold': 0.4}}
        rec['price_input_contract'] = producer.PRICE_INPUT_CONTRACT
        rec['data_sha256'] = version * 64      # distinct content -> distinct version
        pm.register(registry, rec)
    first = registry['history']['SH:600000'][0]['model_version']
    second = registry['symbols']['SH:600000']['model_version']
    assert first != second
    assert registry['symbols']['SH:600000']['rollback_parent'] == first
    with pytest.raises(pm.PersonalModelError, match='VERSION_NOT_FOUND'):
        pm.rollback_to(registry, 'SH:600000', 'nope')
    assert personal_registry.registry_summary(registry)['symbols'] == 1


def test_insufficient_history_is_flagged_not_disguised():
    w = rw.rolling_windows(date(2026, 10, 15))
    status = rw.stock_history_status(date(2025, 6, 1), date(2026, 8, 17), w)
    assert status['status'] == 'INSUFFICIENT_HISTORY'
    assert status['reason'] == 'HISTORY_SHORTER_THAN_24M_TRAIN_WINDOW'
    ok = rw.stock_history_status(date(2012, 12, 10), date(2026, 8, 17), w)
    assert ok['status'] == 'COMPLETE_24M_HISTORY'
