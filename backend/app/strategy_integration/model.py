#!/usr/bin/env python3
"""Research-only adapter for supplied frozen Panji D1/D2/D3 two-stage models.

NO true/oracle ZigZag pivot filtering, NO after-close publishing, NO training.
Loads locally supplied trusted pickle only after an exact SHA256 gate. The
result is a diagnostic snapshot; its H-side label definition is conflicted.
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import pickle
import sys

import numpy as np
import pandas as pd

ASSET_DIR = Path(os.environ.get('PANJI_STRATEGY_MODEL_ASSETS', str(Path(__file__).parent / 'fourteen')))
ENGINE_SHA = '36c862bc318b5e53617b7527d5db166de3243addc85d2d1ae6a655574a93fb1a'
BUNDLE_SHA = 'e38263f33add0e84bf73a7002122c9717c4bc71e733957cebef14e1b5073a351'
SCRIPT_SHA = '1cc123274c00209552b7ddc920c804be92e714c07fc7ef088c1c7e14a8b816bd'
EXPECTED_VERSION = 'panji-two-stage-final-d123-1.0'
EXPECTED_KEYS = {(layer, kind, stage) for layer in ('L1', 'L2') for kind in ('H', 'L') for stage in (1,2,3)}
SKLEARN_TRAIN_VERSION = '1.9.1'   # From the actual supplied pickle estimator metadata.


class AssetError(RuntimeError):
    pass


# Diagnostics only: how many feature cells were NaN in the as-of rebuild but
# not in the run frame (pivot-depth differences, handled natively by LightGBM).
pit_new_nan_cells = [0]


def digest(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()



def _approved_bundle_sha(assets: Path) -> str:
    """Trusted operator-managed release manifest, not an HTTP-controlled path."""
    manifest = assets / 'approved_bundle.json'
    if not manifest.exists():
        return BUNDLE_SHA
    meta = json.loads(manifest.read_text())
    digest_value = meta.get('bundle_sha256')
    if meta.get('status') != 'AUDITED' or meta.get('target') != {'L':'HH','H':'LL'} or not isinstance(digest_value,str) or len(digest_value)!=64:
        raise AssetError('UNAPPROVED_MODEL_BUNDLE')
    return digest_value

def validate_source_assets(assets: Path = ASSET_DIR) -> dict:
    from .fourteen_adapter import is_package, load_package
    if is_package(assets):
        _, facts = load_package(assets)
        return facts
    expected = {
        'b_v55_d123_trend_change.py': ENGINE_SHA,
        'panji_two_stage_final.py': SCRIPT_SHA,
        'two_stage_models_local_only.pkl': _approved_bundle_sha(assets),
    }
    facts = {}
    for name, known_sha in expected.items():
        path = assets / name
        if not path.is_file():
            raise AssetError(f'ASSET_MISSING: {path}')
        actual = digest(path)
        if actual != known_sha:
            raise AssetError(f'ASSET_SHA_MISMATCH {name}: expected={known_sha} got={actual}')
        facts[name] = {'sha256': actual, 'bytes': path.stat().st_size}
    return facts


def load_engine(assets: Path = ASSET_DIR):
    from .fourteen_adapter import is_package, load_package
    if is_package(assets):
        return load_package(assets)[0]['fourteen_runtime'].engine
    path = assets / 'b_v55_d123_trend_change.py'
    if digest(path) != ENGINE_SHA:
        raise AssetError('ENGINE_SHA_MISMATCH')
    spec = importlib.util.spec_from_file_location('panji_frozen_b55', path)
    if spec is None or spec.loader is None:
        raise AssetError('ENGINE_IMPORT_FAILED')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if len(mod.ALL_COLS) != 131 or len(set(mod.ALL_COLS)) != 131:
        raise AssetError('FROZEN_FEATURE_SCHEMA_MISMATCH')
    return mod


def load_bundle(assets: Path = ASSET_DIR, allow_runtime_mismatch: bool = False) -> tuple[dict, dict]:
    """Only unpickle a local, explicitly SHA-pinned, owner-provided asset.

    NEVER accept a pickle file supplied in an HTTP request or by another user.
    """
    from .fourteen_adapter import is_package, load_package
    if is_package(assets):
        return load_package(assets)
    import sklearn
    version = str(sklearn.__version__)
    if version != SKLEARN_TRAIN_VERSION and not allow_runtime_mismatch:
        raise AssetError(f'SKLEARN_RUNTIME_MISMATCH: saved={SKLEARN_TRAIN_VERSION} running={version}')
    f = assets / 'two_stage_models_local_only.pkl'
    pinned = _approved_bundle_sha(assets)
    if digest(f) != pinned:
        raise AssetError('BUNDLE_SHA_MISMATCH')
    with open(f, 'rb') as handle:
        b = pickle.load(handle)
    if b.get('schema') != 'PANJI_TWO_STAGE_FINAL_D123_V1' or b.get('version') != EXPECTED_VERSION:
        raise AssetError('BUNDLE_SCHEMA_OR_VERSION_MISMATCH')
    if b.get('feature_sha') != ENGINE_SHA or b.get('feature_dim') != 131:
        raise AssetError('BUNDLE_FEATURE_MISMATCH')
    if set(b.get('models', {})) != EXPECTED_KEYS or set(b.get('thresholds', {})) != EXPECTED_KEYS:
        raise AssetError('BUNDLE_MODEL_COUNT_OR_KEYS_MISMATCH')
    if not b.get('uncalibrated'):
        raise AssetError('EXPECTED_UNCALIBRATED_BUNDLE')
    for key in sorted(EXPECTED_KEYS):
        model = b['models'][key]
        if getattr(model, 'n_features_in_', None) != 131:
            raise AssetError(f'MODEL_FEATURE_WIDTH_MISMATCH: {key}')
        if not (0 <= float(b['thresholds'][key]) <= 1):
            raise AssetError(f'INVALID_THRESHOLD: {key}')
    # A production H/SELL release is BLOCKED until LL vs HL contract is fixed.
    h_conflict = b.get('target', {}).get('H') == 'HL'
    if b.get('target', {}).get('L') != 'HH' or b.get('target',{}).get('H') not in ('HL','LL'):
        raise AssetError('UNEXPECTED_MODEL_TARGET_SEMANTICS')
    if h_conflict and pinned!=BUNDLE_SHA:
        raise AssetError('NEW_RELEASE_MUST_HAVE_H_LL_METADATA')
    return b, {
        'runtime_sklearn': version, 'train_sklearn': SKLEARN_TRAIN_VERSION,
        'runtime_compatible': version == SKLEARN_TRAIN_VERSION,
        'models': 12, 'thresholds': 12, 'feature_dim': 131,
        'release_id': hashlib.sha256(f'{pinned}|{ENGINE_SHA}|L_HH__H_LL__V1|QFQ_ASOF_V1'.encode()).hexdigest(),
        'bundle_sha256': pinned,
        'price_input_contract': 'RAW_OHLCV',
        'target_label_conflict': {'kind':'H', 'trained_positive':'LL', 'bundle_declares':'HL'},
        'model_scores_are_calibrated': False,
        'production_publish_allowed': False,
    }


def load_bundle_file(path, *, allow_runtime_mismatch: bool = False,
                     pinned_sha: str | None = None) -> tuple[dict, dict]:
    """Load the exact weights a release pins.

    Used when an activated release selects candidate weights. The file must be a
    locally trusted, SHA-pinned artifact; never accept an HTTP-supplied pickle.
    """
    path = Path(path)
    from .fourteen_adapter import is_package, load_package
    if is_package(path):
        return load_package(path, pinned_sha)
    if not path.is_file():
        raise AssetError('BUNDLE_FILE_MISSING: ' + str(path))
    # Pin the artifact identity first: tampering is rejected before any
    # interpreter/runtime work and before the pickle is ever opened.
    actual = digest(path)
    if pinned_sha and actual != pinned_sha:
        raise AssetError('BUNDLE_SHA_MISMATCH')
    import sklearn
    version = str(sklearn.__version__)
    if version != SKLEARN_TRAIN_VERSION and not allow_runtime_mismatch:
        raise AssetError(f'SKLEARN_RUNTIME_MISMATCH: saved={SKLEARN_TRAIN_VERSION} running={version}')
    with open(path, 'rb') as handle:
        b = pickle.load(handle)
    if b.get('schema') != 'PANJI_TWO_STAGE_FINAL_D123_V1' or b.get('version') != EXPECTED_VERSION:
        raise AssetError('BUNDLE_SCHEMA_OR_VERSION_MISMATCH')
    if b.get('feature_sha') != ENGINE_SHA or b.get('feature_dim') != 131:
        raise AssetError('BUNDLE_FEATURE_MISMATCH')
    if set(b.get('models', {})) != EXPECTED_KEYS or set(b.get('thresholds', {})) != EXPECTED_KEYS:
        raise AssetError('BUNDLE_MODEL_COUNT_OR_KEYS_MISMATCH')
    if not b.get('uncalibrated'):
        raise AssetError('EXPECTED_UNCALIBRATED_BUNDLE')
    for key in sorted(EXPECTED_KEYS):
        m = b['models'][key]
        if getattr(m, 'n_features_in_', None) != 131:
            raise AssetError(f'MODEL_FEATURE_WIDTH_MISMATCH: {key}')
        if not (0 <= float(b['thresholds'][key]) <= 1):
            raise AssetError(f'INVALID_THRESHOLD: {key}')
    if b.get('target', {}).get('L') != 'HH' or b.get('target', {}).get('H') not in ('HL', 'LL'):
        raise AssetError('UNEXPECTED_MODEL_TARGET_SEMANTICS')
    return b, {
        'runtime_sklearn': version, 'train_sklearn': SKLEARN_TRAIN_VERSION,
        'runtime_compatible': version == SKLEARN_TRAIN_VERSION,
        'models': 12, 'thresholds': 12, 'feature_dim': 131,
        'release_id': hashlib.sha256(
            f'{actual}|{ENGINE_SHA}|L_HH__H_LL__V1|QFQ_ASOF_V1'.encode()).hexdigest(),
        'bundle_sha256': actual,
        'price_input_contract': (b.get('price_input_contract') or 'RAW_OHLCV'),
        'target_label_conflict': {'kind': 'H', 'trained_positive': 'LL', 'bundle_declares': 'HL'},
        'model_scores_are_calibrated': False,
        'production_publish_allowed': False,
    }


def read_ohlcv(p: Path, fmod) -> pd.DataFrame:
    # Local CSV useful for diagnostic; production bars must use market_dev's official source.
    if p.suffix.lower() in ('.parquet', '.pq'):
        raw = fmod.load_raw(p)
    elif p.suffix.lower() == '.csv':
        raw = pd.read_csv(p, usecols=['datetime','open','high','low','close','volume'])
        raw['datetime'] = pd.to_datetime(raw.datetime)
        raw = raw.sort_values('datetime').reset_index(drop=True)
    else:
        raise AssetError('OHLCV_INPUT_MUST_BE_CSV_OR_PARQUET')
    if raw.empty or raw.datetime.isna().any() or raw.datetime.duplicated().any():
        raise AssetError('INVALID_OR_EMPTY_DATES')
    a = raw[['open','high','low','close','volume']].to_numpy(dtype=float)
    if not np.isfinite(a).all() or not (a[:, :4] > 0).all() or not (a[:,4] >= 0).all():
        raise AssetError('INVALID_OHLCV')
    if not (raw.high >= raw[['open','low','close']].max(axis=1)).all():
        raise AssetError('INVALID_HIGH')
    if not (raw.low <= raw[['open','high','close']].min(axis=1)).all():
        raise AssetError('INVALID_LOW')
    return raw



def _pit_frame(pit_source: pd.DataFrame, anchor: str) -> pd.DataFrame:
    """Rebuild the qfq frame as it looked on `anchor` (point-in-time).

    Uses Panji's canonical pure function only; never a second adjustment
    formula. Bars inside the anchor's factor regime come back unadjusted and
    earlier regimes are rescaled by their own factor ratio, so a *later*
    corporate action cannot change this as-of series.
    """
    from app.services.adj_factor import apply_adj_factor_with_as_of
    date_col = 'trade_date' if 'trade_date' in pit_source.columns else 'datetime'
    dates = pd.to_datetime(pit_source[date_col])
    bars = pit_source.set_index(pd.DatetimeIndex(dates))
    # pit_source carries MDAS QFQ bars for charting AND independent RAW
    # OHLCV evidence. Only the latter may be adjusted for a past as-of.
    raw_cols = ['raw_open','raw_high','raw_low','raw_close','raw_volume']
    if not set(raw_cols).issubset(pit_source.columns):
        raise AssetError('PIT_RAW_OHLCV_REQUIRED_NO_DOUBLE_ADJUST')
    bars = bars[raw_cols].rename(columns=dict(zip(
        raw_cols, ['open','high','low','close','volume']))).astype(float)
    factors = pd.DataFrame({
        'trade_date': dates.to_numpy(),
        'adj_factor': pd.to_numeric(pit_source['adj_factor'], errors='coerce').astype(float).to_numpy(),
    })
    out = apply_adj_factor_with_as_of(bars, factors, date.fromisoformat(anchor))
    out = out[['open', 'high', 'low', 'close', 'volume']].astype(float).sort_index()
    frame = pd.DataFrame(out.to_numpy(float),
                         columns=['open', 'high', 'low', 'close', 'volume'])
    frame.insert(0, 'datetime',
                 pd.Series(pd.to_datetime(out.index)).reset_index(drop=True))
    return frame


def _pit_features(raw: pd.DataFrame, pit_source: pd.DataFrame, fmod, snap,
                  needed: list[int]) -> tuple[Any, Any]:
    """Feature rows for `needed` bars, each computed on its own as-of frame.

    Bars sharing a factor regime share one frame, so the cost is bounded by the
    number of corporate actions in the lookback rather than by bar count.
    """
    import pandas as _pd
    factors = _pd.to_numeric(pit_source['adj_factor'], errors='coerce').to_numpy(float)
    if len(factors) != len(raw):
        raise AssetError('PIT_SOURCE_LENGTH_MISMATCH')
    # The daily table (signal_available etc.) keeps the run-frame indexing that
    # callers rely on; only the feature matrix is rebuilt per as-of regime.
    day, X_run = fmod.features(raw, snap, needed)
    # Contiguous factor regimes: a repeated factor value is still a distinct
    # regime, and each decision day must be anchored inside its OWN regime.
    regime_of_bar = np.empty(len(factors), dtype=np.int64)
    regime = 0
    for i in range(1, len(factors)):
        if factors[i] != factors[i - 1]:
            regime += 1
        regime_of_bar[i] = regime
    by_regime: dict[int, list[int]] = {}
    for t in needed:
        by_regime.setdefault(int(regime_of_bar[int(t)]), []).append(int(t))
    X = np.full((len(needed), len(fmod.ALL_COLS)), np.nan, dtype=np.float32)
    row_of_bar = {int(t): n for n, t in enumerate(needed)}
    asof_day_rows = {}
    for bars in by_regime.values():
        anchor = str(raw.datetime.iloc[max(bars)].date())
        frame = _pit_frame(pit_source, anchor)
        _, snap_regime, _ = fmod.replay(frame)
        asof_day, X_regime = fmod.features(frame, snap_regime, bars)
        for i, bar in enumerate(bars):
            asof_day_rows[int(bar)] = asof_day.iloc[i].to_dict()
        for i, t in enumerate(bars):
            X[row_of_bar[int(t)]] = X_regime[i]
    # An as-of series legitimately has a slightly different pivot depth, so a
    # few extra NaNs (LightGBM handles them natively) are expected. What must
    # never happen is a decision bar that becomes entirely uninformative.
    new_nan = np.isnan(X) & ~np.isnan(X_run)
    dead = np.isnan(X).all(axis=1) & ~np.isnan(X_run).all(axis=1)
    if dead.any():
        bad = [int(t) for i, t in enumerate(needed) if dead[i]]
        raise AssetError('PIT_DECISION_BAR_UNINFORMATIVE ' + str(bad[:5]))
    pit_new_nan_cells[0] += int(new_nan.sum())
    if set(asof_day_rows) != set(map(int, needed)):
        raise AssetError('PIT_DAILY_ROWS_MISSING')
    day = pd.DataFrame([asof_day_rows[int(t)] for t in needed])
    return day, X


def load_personal_artifacts(record: dict) -> dict:
    """Load the standalone personal classifiers pinned by a record.

    The artifact digest is verified before anything is unpickled; a missing or
    drifted artifact is a hard failure, never a silent fallback.
    """
    path = record.get('artifact_path')
    if not path:
        raise AssetError('PERSONAL_ARTIFACT_MISSING')
    target = Path(path)
    if not target.is_file():
        raise AssetError('PERSONAL_ARTIFACT_MISSING ' + str(target))
    actual = digest(target)
    if actual != record.get('personal_bundle_sha256'):
        raise AssetError('PERSONAL_ARTIFACT_SHA_MISMATCH')
    with open(target, 'rb') as handle:
        blob = pickle.load(handle)
    for key, clf in blob.items():
        if getattr(clf, 'n_features_in_', None) != 131:
            raise AssetError('PERSONAL_ARTIFACT_FEATURE_WIDTH_MISMATCH ' + str(key))
    return blob


def personal_adjusted_score(key, base_score: float, record: dict,
                            artifacts: dict | None,
                            notes: list | None = None):
    """Combine the global score with the symbol's personal adjustment.

    Returns (adjusted_score, threshold, mode). mode == 'GLOBAL_ONLY' means the
    personal model does not apply and the global score/threshold stand.
    """
    sub = (record.get('submodels') or {}).get(str(key))
    if not sub:
        return base_score, None, 'GLOBAL_ONLY'
    mode = sub.get('mode')
    if mode == 'REGULARIZED_ADJUSTMENT':
        coef = sub.get('coef')
        intercept = sub.get('intercept')
        if coef is None or intercept is None:
            raise AssetError('PERSONAL_ADJUSTMENT_COEFFICIENTS_MISSING ' + str(key))
        z = float(coef) * float(base_score) + float(intercept)
        adjusted = 1.0 / (1.0 + float(np.exp(-z)))
        blended = float(sub.get('blend_weight', 0.0)) * adjusted + (
            1.0 - float(sub.get('blend_weight', 0.0))) * float(base_score)
        tau = sub.get('threshold')
        if tau is None:
            raise AssetError('PERSONAL_ADJUSTMENT_THRESHOLD_MISSING ' + str(key))
        return float(blended), float(tau), 'GLOBAL_PLUS_PERSONAL'
    if mode == 'STANDALONE_LIGHT':
        if not artifacts:
            raise AssetError('PERSONAL_STANDALONE_ARTIFACT_REQUIRED ' + str(key))
        clf = artifacts.get(f'{key[0]}:{key[1]}:D{key[2]}')
        if clf is None:
            raise AssetError('PERSONAL_STANDALONE_MODEL_MISSING ' + str(key))
        if notes is not None:
            notes.append('STANDALONE_LIGHT')
        tau = sub.get('threshold')
        if tau is None:
            raise AssetError('PERSONAL_STANDALONE_THRESHOLD_MISSING ' + str(key))
        # A standalone score needs the feature row, handled by the caller; here we
        # only report that this submodel must be scored by the personal model.
        return None, float(tau), 'PERSONAL_STANDALONE_REQUIRES_FEATURES'
    # FALLBACK_GLOBAL: keep the global score and threshold, with an explicit note.
    if notes is not None:
        notes.append('FALLBACK_GLOBAL:' + str(sub.get('reason')))
    return base_score, None, 'GLOBAL_ONLY'


def score_candidates(raw: pd.DataFrame, symbol: str, fmod, bundle: dict, as_of: str | None = None, *, min_anchor_bar: int | None = None,
                   pit_source: pd.DataFrame | None = None,
                   release_id: str = BUNDLE_SHA,
                   personal: dict | None = None) -> dict:
    """Causal D1/D2/D3 for *every* left-5/right-1 candidate, not oracle pivot subset.

    Frozen fmod.replay() yields as-of snapshots and features() only accesses
    OHLCV through each decision bar. No LABEL / future oracle columns are read.
    """
    if as_of is not None:
        cutoff = pd.Timestamp(as_of).normalize()
        raw = raw.loc[raw.datetime.dt.normalize() <= cutoff].copy().reset_index(drop=True)
    if pit_source is not None and as_of is not None:
        pit_source = pit_source.loc[
            pd.to_datetime(pit_source.datetime).dt.normalize() <= cutoff
        ].copy().reset_index(drop=True)
    if len(raw) < 8:
        raise AssetError('INSUFFICIENT_BARS')
    final, snap, _ = fmod.replay(raw)
    del final  # NEVER use hindsight labels or final pivots to select inference candidates.
    if pit_source is not None:
        from . import lab_parity_contract as lab_contract
        causal = lab_contract.causal_candidates_pit(
            raw, pit_source, fmod, _pit_frame)
        # The as-of anchor selects identity; chart coordinates use the
        # run-date QFQ frame so price and displayed bars share a scale.
        causal = [(j, k, float(raw.high.iloc[j] if k == 'H' else raw.low.iloc[j]))
                  for j, k, _ in causal]
    else:
        causal, _ = fmod.causal_candidates(raw)
    entries = [(int(j),kind,int(j+stage),stage,float(px))
               for j,kind,px in causal if min_anchor_bar is None or int(j) >= min_anchor_bar
               for stage in (1,2,3) if int(j)+stage < len(raw)]
    if not entries:
        return {'symbol':symbol,'as_of':str(raw.datetime.iloc[-1].date()), 'candidates':[],
                'today_observations':[], 'status':'DIAGNOSTIC_ONLY'}
    needed = sorted({e[2] for e in entries})
    if pit_source is not None and len(pit_source) == len(raw) and 'adj_factor' in pit_source.columns:
        # Point-in-time: each decision day sees its own as-of qfq frame.
        day, X = _pit_features(raw, pit_source, fmod, snap, needed)
    else:
        day, X = fmod.features(raw, snap, needed)
    if len(fmod.ALL_COLS) != 131 or X.shape != (len(needed),131):
        raise AssetError('CAUSAL_FEATURE_SHAPE_MISMATCH')
    row_of_bar = {int(t):n for n,t in enumerate(needed)}
    n = len(entries)
    preds = {}
    for layer in ('L1','L2'):
        scores = np.full(n,np.nan,dtype=float)
        for kind in ('H','L'):
            for stage in (1,2,3):
                matches=[i for i,(_,k,_,s,_) in enumerate(entries) if k==kind and s==stage]
                if not matches:continue
                rows=np.stack([X[row_of_bar[entries[i][2]]] for i in matches])
                named_rows=pd.DataFrame(rows,columns=list(fmod.ALL_COLS))
                vals=bundle['models'][(layer,kind,stage)].predict_proba(named_rows)[:,1]
                if len(vals)!=len(matches) or not np.isfinite(vals).all():
                    raise AssetError(f'NON_FINITE_MODEL_SCORE {layer}:{kind}:D{stage}')
                scores[matches]=vals
        preds[layer]=scores
    # ---- personal adjustment layer (combines with, never replaces, global) ----
    personal_record = None
    personal_artifacts = None
    personal_notes: list[str] = []
    personal_thresholds: dict = {}
    applied_personal: set[tuple[int,str]] = set()
    if personal is not None:
        personal_record = personal.get('record')
        if personal_record is not None:
            if any(v.get('mode') == 'STANDALONE_LIGHT' for v in
                   (personal_record.get('submodels') or {}).values()):
                raise AssetError('PERSONAL_STANDALONE_SCORER_NOT_READY')
            if personal.get('artifacts') is not None:
                personal_artifacts = personal['artifacts']
            elif personal_record.get('artifact_path'):
                personal_artifacts = load_personal_artifacts(personal_record)
            for i,(j,kind,t,stage,px) in enumerate(entries):
                for layer in ('L1','L2'):
                    key=(layer,kind,stage)
                    adjusted, tau, _mode = personal_adjusted_score(
                        key, float(preds[layer][i]), personal_record,
                        personal_artifacts, personal_notes)
                    if adjusted is not None and tau is not None:
                        preds[layer][i]=adjusted
                        personal_thresholds[(i,layer)]=tau
                        if _mode == 'GLOBAL_PLUS_PERSONAL' and float(
                                (personal_record['submodels'][str(key)]).get('blend_weight', 0)) > 0:
                            applied_personal.add((i,layer))
    grouped: dict[str,dict] = {}
    today=[]
    for i,(j,kind,t,stage,px) in enumerate(entries):
        anchor_date=str(raw.datetime.iloc[j].date())
        decision_date=str(raw.datetime.iloc[t].date())
        visible_at=str(raw.datetime.iloc[j+1].date())
        # date-based stable identity, independent of DataFrame index shifting after backfill.
        candidate_id=f'{symbol}|{anchor_date}|{kind}|B55'
        if candidate_id not in grouped:
            grouped[candidate_id]={
                'candidate_id':candidate_id,'research_event_id':f'{symbol}|{j}|{kind}',
                'symbol':symbol,'kind':kind,'anchor_date':anchor_date,
                'visible_at':visible_at,'anchor_price':px,'observations':[]}
        s1,s2=float(preds['L1'][i]),float(preds['L2'][i])
        t1=float(personal_thresholds.get((i,'L1'), bundle['thresholds'][('L1',kind,stage)]))
        t2=float(personal_thresholds.get((i,'L2'), bundle['thresholds'][('L2',kind,stage)]))
        scoreable=bool(day.iloc[row_of_bar[t]].signal_available)
        # The frozen product contract is L→HH / H→LL; retain original weights and SHA.
        joint=bool(s1>=t1 and s2>=t2) if scoreable else False
        scoring_mode='GLOBAL_ONLY'
        personal_release_id=None
        if (i, 'L1') in applied_personal or (i, 'L2') in applied_personal:
            scoring_mode='GLOBAL_PLUS_PERSONAL'
            personal_release_id=personal_record.get('release_id')
        obs={
            'observation_id':hashlib.sha256(f'{candidate_id}|{release_id}|D{stage}|{decision_date}'.encode()).hexdigest()[:24],
            'candidate_id':candidate_id,'model_release_id':release_id,
            'symbol':symbol,
            'global_release_id':release_id,
            'personal_release_id':personal_release_id,
            'scoring_mode':scoring_mode,
            'l1_scoring_mode':('GLOBAL_PLUS_PERSONAL' if (i,'L1') in applied_personal else 'GLOBAL_ONLY'),
            'l2_scoring_mode':('GLOBAL_PLUS_PERSONAL' if (i,'L2') in applied_personal else 'GLOBAL_ONLY'),
            'stage':f'D{stage}','decision_date':decision_date,'data_cutoff':decision_date,
            'l1_score':s1,'l2_score':s2,'l1_threshold':t1,'l2_threshold':t2,
            'scoreable':scoreable,'joint_alert_diagnostic':joint,
            'l2_conditional_on_true_anchor':True,
            'h_target_definition_blocked':False,
            'scores_calibrated':False,
            'research_only':True,
        }
        grouped[candidate_id]['observations'].append(obs)
        if decision_date==str(raw.datetime.iloc[-1].date()):
            today.append(obs)
    return {
        'schema':'PANJI_SP0_DIAGNOSTIC_V1',
        'status':'DIAGNOSTIC_ONLY',
        'eligible_for_daily_publish':False,  # validated/exported separately

        'symbol':symbol,
        'as_of':str(raw.datetime.iloc[-1].date()),
        'feature_sha':ENGINE_SHA,'model_release_id':release_id,
        'global_release_id':release_id,
        'personal_release_id':(personal_record or {}).get('release_id'),
        'scoring_mode':('GLOBAL_PLUS_PERSONAL' if applied_personal
                        else 'GLOBAL_ONLY'),
        'model_score_kind':'UNCALIBRATED',
        'h_positive_label_contract':'H_LL__L_HH__2026_10_14',
        'candidates':list(grouped.values()),
        'today_observations':today,
    }


def assert_prefix_invariance(raw: pd.DataFrame, symbol: str, fmod, bundle: dict, tail_cut: int = 15,
                             pit_source: pd.DataFrame | None = None):
    if len(raw) <= tail_cut+30:
        raise AssetError('PREFIX_TEST_REQUIRES_MORE_BARS')
    early_source = None
    if pit_source is not None and len(pit_source) == len(raw):
        early_source = pit_source.iloc[:-tail_cut].reset_index(drop=True)
    full = score_candidates(raw,symbol,fmod,bundle,pit_source=pit_source)
    early = score_candidates(raw.iloc[:-tail_cut].reset_index(drop=True),symbol,fmod,bundle,
                             pit_source=early_source)
    byid={o['observation_id']:o for c in full['candidates'] for o in c['observations']}
    checked=0
    for c in early['candidates']:
        for old in c['observations']:
            actual=byid.get(old['observation_id'])
            if actual is None:
                raise AssetError(f'PREFIX_MISSING_OBSERVATION {old["observation_id"]}')
            for score in ('l1_score','l2_score'):
                if not np.isclose(actual[score],old[score],atol=1e-12,rtol=0):
                    raise AssetError(f'PREFIX_SCORE_LEAK {score}: {old["observation_id"]}')
            checked+=1
    if checked==0:
        raise AssetError('PREFIX_ZERO_COMMON_OBSERVATIONS')
    return checked


def main():
    parser=argparse.ArgumentParser(description='SP0 read-only two-stage causal diagnostic scorer')
    parser.add_argument('command', choices=['preflight','predict-stock'])
    parser.add_argument('--assets',type=Path,default=ASSET_DIR)
    parser.add_argument('--input',type=Path,help='Trusted source OHLCV CSV or Parquet')
    parser.add_argument('--symbol',default='SH:600000')
    parser.add_argument('--as-of',default=None,help='Inclusive YYYY-MM-DD; future OHLCV truncated')
    parser.add_argument('--out',type=Path,help='Diagnostic JSON output path (never published automatically)')
    parser.add_argument('--prefix-check',action='store_true')
    parser.add_argument('--allow-runtime-mismatch-for-diagnostics',action='store_true')
    args=parser.parse_args()
    assets=validate_source_assets(args.assets)
    b,facts=load_bundle(args.assets,args.allow_runtime_mismatch_for_diagnostics)
    if args.command=='preflight':
        print(json.dumps({'status':'ASSET_VERIFIED_NOT_RELEASE_APPROVED','assets':assets,'bundle':facts},ensure_ascii=False,indent=2))
        return
    if args.input is None or args.out is None:
        parser.error('--input and --out are required for predict-stock')
    fmod=load_engine(args.assets)
    raw=read_ohlcv(args.input,fmod)
    ans=score_candidates(raw,args.symbol,fmod,b,args.as_of)
    if args.prefix_check:
        working=raw if args.as_of is None else raw.loc[raw.datetime<=pd.Timestamp(args.as_of)].reset_index(drop=True)
        ans['prefix_invariance_tested_observations']=assert_prefix_invariance(working,args.symbol,fmod,b)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(ans,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'status':ans['status'],'symbol':args.symbol,'candidates':len(ans['candidates']),
        'latest_stage_rows':len(ans['today_observations']),'out':str(args.out),
        'h_label_blocked':True,'can_publish':False},ensure_ascii=False))


if __name__=='__main__':
    try: main()
    except AssetError as e:
        print('STOP:',e,file=sys.stderr);sys.exit(2)
