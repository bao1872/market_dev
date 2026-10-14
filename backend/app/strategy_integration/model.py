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

ASSET_DIR = Path(os.environ.get('PANJI_STRATEGY_MODEL_ASSETS', '/opt/panji/strategy-assets'))
ENGINE_SHA = '36c862bc318b5e53617b7527d5db166de3243addc85d2d1ae6a655574a93fb1a'
BUNDLE_SHA = 'e38263f33add0e84bf73a7002122c9717c4bc71e733957cebef14e1b5073a351'
SCRIPT_SHA = '1cc123274c00209552b7ddc920c804be92e714c07fc7ef088c1c7e14a8b816bd'
EXPECTED_VERSION = 'panji-two-stage-final-d123-1.0'
EXPECTED_KEYS = {(layer, kind, stage) for layer in ('L1', 'L2') for kind in ('H', 'L') for stage in (1,2,3)}
SKLEARN_TRAIN_VERSION = '1.9.1'   # From the actual supplied pickle estimator metadata.


class AssetError(RuntimeError):
    pass


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
        'target_label_conflict': {'kind':'H', 'trained_positive':'LL', 'bundle_declares':'HL'},
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


def score_candidates(raw: pd.DataFrame, symbol: str, fmod, bundle: dict, as_of: str | None = None, *, min_anchor_bar: int | None = None) -> dict:
    """Causal D1/D2/D3 for *every* left-5/right-1 candidate, not oracle pivot subset.

    Frozen fmod.replay() yields as-of snapshots and features() only accesses
    OHLCV through each decision bar. No LABEL / future oracle columns are read.
    """
    if as_of is not None:
        cutoff = pd.Timestamp(as_of).normalize()
        raw = raw.loc[raw.datetime.dt.normalize() <= cutoff].copy().reset_index(drop=True)
    if len(raw) < 8:
        raise AssetError('INSUFFICIENT_BARS')
    final, snap, _ = fmod.replay(raw)
    del final  # NEVER use hindsight labels or final pivots to select inference candidates.
    causal, _ = fmod.causal_candidates(raw)
    entries = [(int(j),kind,int(j+stage),stage,float(px))
               for j,kind,px in causal if min_anchor_bar is None or int(j) >= min_anchor_bar
               for stage in (1,2,3) if int(j)+stage < len(raw)]
    if not entries:
        return {'symbol':symbol,'as_of':str(raw.datetime.iloc[-1].date()), 'candidates':[],
                'today_observations':[], 'status':'DIAGNOSTIC_ONLY'}
    needed = sorted({e[2] for e in entries})
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
        t1=float(bundle['thresholds'][('L1',kind,stage)])
        t2=float(bundle['thresholds'][('L2',kind,stage)])
        scoreable=bool(day.iloc[row_of_bar[t]].signal_available)
        # The frozen product contract is L→HH / H→LL; retain original weights and SHA.
        joint=bool(s1>=t1 and s2>=t2) if scoreable else False
        obs={
            'observation_id':hashlib.sha256(f'{candidate_id}|{BUNDLE_SHA}|D{stage}|{decision_date}'.encode()).hexdigest()[:24],
            'candidate_id':candidate_id,'model_release_id':BUNDLE_SHA,
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
        'feature_sha':ENGINE_SHA,'model_release_id':BUNDLE_SHA,
        'model_score_kind':'UNCALIBRATED',
        'h_positive_label_contract':'H_LL__L_HH__2026_10_14',
        'candidates':list(grouped.values()),
        'today_observations':today,
    }


def assert_prefix_invariance(raw: pd.DataFrame, symbol: str, fmod, bundle: dict, tail_cut: int = 15):
    if len(raw) <= tail_cut+30:
        raise AssetError('PREFIX_TEST_REQUIRES_MORE_BARS')
    full = score_candidates(raw,symbol,fmod,bundle)
    early = score_candidates(raw.iloc[:-tail_cut].reset_index(drop=True),symbol,fmod,bundle)
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
