"""Integration boundary for the frozen portable package; no I/O at import time.

Inference delegates candidate validity to the supplied runtime. Historical runs
use decision-local PIT frames, never one run-date adjustment for every prefix.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

PACKAGE = Path(__file__).parent / 'fourteen'
SCHEMA = 'panji-strategy-candidates-v3'


def verify_package(path, pinned_sha=None):
    path = Path(path).resolve()
    payload = (path / 'manifest.json').read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    if pinned_sha and sha != pinned_sha:
        raise ValueError('FOURTEEN_MANIFEST_SHA_MISMATCH')
    manifest = json.loads(payload)
    expected = {f'{layer}_{kind}_D{stage}' for layer in ('L1', 'L2')
                for kind in ('H', 'L') for stage in (1, 2, 3)} | {'D0_H_D0', 'D0_L_D0'}
    if manifest.get('schema') != 'PANJI_FOURTEEN_PORTABLE_V1' or set(manifest.get('models', {})) != expected:
        raise ValueError('FOURTEEN_TASK_SCHEMA_MISMATCH')
    for name, digest in manifest['files'].items():
        target = path / name
        if target.is_symlink() or path not in target.resolve().parents:
            raise ValueError('FOURTEEN_FILE_OUTSIDE_PACKAGE')
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('FOURTEEN_FILE_SHA_MISMATCH:' + name)
    for info in manifest['models'].values():
        if info['file'] not in manifest['files'] or not 0 <= info['threshold'] <= 1:
            raise ValueError('FOURTEEN_INVALID_MODEL_MANIFEST')
    return manifest, sha


def verify_runtime_contract(manifest):
    """A model delivery may replace weights, never the executable feature/label owner."""
    trusted = json.loads((PACKAGE/'manifest.json').read_text())
    for name in ('runtime.py', 'feature_engine.py', '__init__.py', 'requirements.txt'):
        if manifest['files'].get(name) != trusted['files'][name]:
            raise ValueError('UNAPPROVED_DELIVERY_RUNTIME:' + name)
    if manifest.get('runtime_versions') != trusted['runtime_versions']:
        raise ValueError('UNAPPROVED_DELIVERY_DEPENDENCIES')
    if manifest.get('feature_columns') != trusted['feature_columns']:
        raise ValueError('UNAPPROVED_DELIVERY_FEATURES')
    for task, info in manifest['models'].items():
        if any(info.get(k) != trusted['models'][task].get(k) for k in ('target', 'condition')):
            raise ValueError('UNAPPROVED_DELIVERY_LABEL:' + task)


def load_package(path: Path = PACKAGE, pinned_sha: str | None = None):
    from .fourteen import FourteenModels
    path = Path(path)
    manifest, sha = verify_package(path, pinned_sha)
    verify_runtime_contract(manifest)
    runtime = FourteenModels(path)
    if runtime.manifest['schema'] != 'PANJI_FOURTEEN_PORTABLE_V1':
        raise ValueError('FOURTEEN_SCHEMA_MISMATCH')
    bundle = {'fourteen_runtime': runtime,
              'price_input_contract': runtime.manifest.get('price_input_contract', 'UNVERIFIED'),
              'model_version': runtime.manifest['version']}
    return bundle, {'bundle_sha256': sha, 'models': 14,
                    'model_version': runtime.manifest['version'], 'feature_dim': 131,
                    'price_input_contract': bundle['price_input_contract']}


def is_package(path: Path) -> bool:
    path = Path(path)
    return path.is_dir() and (path / 'manifest.json').is_file() and (path / 'runtime.py').is_file()


def verify_production_package(path, pinned_sha):
    if (not isinstance(pinned_sha, str) or len(pinned_sha) != 64
            or any(c not in '0123456789abcdef' for c in pinned_sha) or not is_package(Path(path))):
        raise ValueError('PRODUCTION_FOURTEEN_PACKAGE_REQUIRED')
    manifest, sha = verify_package(path, pinned_sha)
    verify_runtime_contract(manifest)
    if manifest.get('price_input_contract') != 'QFQ_ASOF_V1':
        raise ValueError('PRODUCTION_PRICE_CONTRACT_REQUIRED')
    return manifest, sha


def build_stock(raw, symbol, bundle, *, release_id, chart_bars, pit_source=None):
    from app.services.strategy_tv_zigzag import overlay

    from . import model
    from .breakout import event_identity, observe_breakout
    runtime = bundle['fourteen_runtime']
    start = max(0, len(raw) - chart_bars)
    bars = [{'date': str(pd.Timestamp(r.datetime).date()), 'open': float(r.open),
             'high': float(r.high), 'low': float(r.low), 'close': float(r.close)}
            for r in raw.iloc[start:].itertuples(index=False)]
    candidates, daily, last_segment = {}, [], None
    segments = {}
    for t in range(max(20, start), len(raw)):
        day = str(pd.Timestamp(raw.datetime.iloc[t]).date())
        prefix = model._pit_frame(pit_source.iloc[:t+1].reset_index(drop=True), day) if pit_source is not None else raw.iloc[:t+1]
        from .inference_cache import decision_inputs
        result, snapshots, causal, segment = decision_inputs(runtime, prefix, day, symbol)
        segments[t] = segment
        last_segment = segment
        scored = {}
        for signal in result['signals']:
            key = (signal['anchor_bar'], signal['kind'], signal['stage'])
            scored.setdefault(key, {})[signal['layer']] = signal
        keys = set(scored) | {(int(j), k, t-int(j)) for j, k, _ in causal if 1 <= t-int(j) <= 3}
        for j, kind, stage_number in sorted(keys):
            if j < start:
                continue
            stage, anchor = f'D{stage_number}', str(pd.Timestamp(prefix.datetime.iloc[j]).date())
            identity = f'{symbol}|{anchor}|{kind}|{runtime.manifest["version"]}|{release_id}|{"D0" if stage == "D0" else "D123"}'
            cid = hashlib.sha256(identity.encode()).hexdigest()[:24]
            candidate = candidates.setdefault(cid, {'candidate_id': cid, 'anchor_date': anchor, 'kind': kind,
                'anchor_price': float(raw.high.iloc[j] if kind == 'H' else raw.low.iloc[j]),
                'visible_at': day, 'observations': []})
            signals = scored.get((j, kind, stage_number), {})
            row = {'observation_id': hashlib.sha256(f'{cid}|{stage}|{day}'.encode()).hexdigest()[:24],
                   'stage': stage, 'decision_date': day, 'data_cutoff': day,
                   'l1_score': None, 'l2_score': None, 'd0_score': None,
                   'l1_threshold': None, 'l2_threshold': None, 'd0_threshold': None,
                   'joint_alert': False, 'first_breakout': False, 'breakout': None,
                   'segment': segment, 'scoring_status': 'SCORED' if signals else 'CANDIDATE_INACTIVE',
                   'reference_price': None, 'reference_confirmed_at': None,
                   'l2_evaluation_domain_verified': False, 'model_version': runtime.manifest['version']}
            for layer, signal in signals.items():
                row[f'{layer.lower()}_score'] = signal['score']
                row[f'{layer.lower()}_threshold'] = signal['threshold']
            if stage != 'D0':
                from .producer import _reference
                reference, confirmed = _reference(prefix, snapshots, j, kind)
                scale = float(raw.close.iloc[t]) / float(prefix.close.iloc[t])
                row['reference_price'] = reference * scale if reference is not None else None
                row['reference_confirmed_at'] = confirmed
                fact = observe_breakout(prefix, snapshots, j, kind, t, segments_by_bar=segments)
                if fact:
                    row['breakout'] = {**fact, 'event_id': event_identity(symbol, anchor, kind, release_id),
                        'reference_price': fact['reference_price'] * scale}
                    row['first_breakout'] = fact['is_first_today']
            row['joint_alert'] = all(row[f'{layer}_score'] is not None and row[f'{layer}_score'] >= row[f'{layer}_threshold'] for layer in ('l1', 'l2'))
            candidate['observations'].append(row)
            if signals or row['breakout']:
                daily.append({'trade_date': day, 'symbol': symbol, 'candidate_id': cid, 'kind': kind, **row})
    return {'bars': bars, 'candidates': list(candidates.values()), 'segment': last_segment,
            'tv_zigzag': overlay(raw)}, daily


def extreme_resolution(raw, final, j, kind):
    """Latest lab resolution: strict replacement can mature a negative early."""
    hit = next((p for p in final if int(p['bar']) == j and p['type'] == kind), None)
    if hit:
        return 1, int(hit['confirmed_at'])
    values = raw.high if kind == 'H' else raw.low
    later = values.iloc[j+1:]
    replaced = later[later > values.iloc[j]] if kind == 'H' else later[later < values.iloc[j]]
    resolutions = ([int(replaced.index[0])] if len(replaced) else []) + [
        int(p['confirmed_at']) for p in final if p['bar'] > j and p['type'] != kind]
    return (0, min(resolutions)) if resolutions else (None, None)


def task_samples(raw, stock, engine, symbol, *, pit_source=None, split_windows=None):
    """Offline labels only. Output belongs to evaluation, never candidate API."""
    from . import lab_parity_contract as labels
    from . import model
    if pit_source is not None:
        raw = model._pit_frame(pit_source, str(pd.Timestamp(raw.datetime.iloc[-1]).date()))
    final, _, _ = engine.replay(raw)
    days = {str(pd.Timestamp(v).date()): i for i, v in enumerate(raw.datetime)}
    result = []
    oracle_cache = {}
    for c in stock['candidates']:
        j = days[c['anchor_date']]
        for o in c['observations']:
            if o.get('scoring_status') == 'CANDIDATE_INACTIVE':
                continue
            t = days[o['decision_date']]
            period = next((name.upper() for name, bounds in (split_windows or {}).items() if bounds[0] <= o['decision_date'] <= bounds[1]), 'UNASSIGNED')
            if period != 'UNASSIGNED':
                bounds = split_windows[period.lower()]
                if period not in oracle_cache:
                    cutoff = max(i for day, i in days.items() if day <= bounds[1])
                    oracle = raw.iloc[:cutoff+1]
                    if pit_source is not None:
                        oracle = model._pit_frame(pit_source.iloc[:cutoff+1], bounds[1])
                    oracle_cache[period] = (oracle, engine.replay(oracle)[0], cutoff)
                oracle, truth, cutoff = oracle_cache[period]
            else:
                oracle, truth, cutoff = raw, final, len(raw)-1
            y, available = extreme_resolution(oracle, truth, j, c['kind'])
            lab = labels.labels_for_stage(oracle, truth, j, c['kind'], t, cutoff) if t > j else None
            for layer in ('D0',) if o['stage'] == 'D0' else ('L1', 'L2'):
                label = lab.l2 if layer == 'L2' else y
                maturity = lab.l2_available_bar if layer == 'L2' else available
                # Already-resolved rows are outside the prediction domain.
                eligible = maturity is None or maturity > t
                result.append({'sample_id': o['observation_id'] + ':' + layer,
                               'symbol': symbol, 'task': f'{layer}_{c["kind"]}_{o["stage"]}',
                               'layer': layer, 'kind': c['kind'], 'stage': o['stage'],
                               'decision_date': o['decision_date'],
                               'trend_context': ({'direction': o['segment']['direction'],
                                   'endpoint_match': o['segment']['lastDate'] == c['anchor_date']
                                       and o['segment'].get('toKind') == c['kind'],
                                   'as_of': o['segment']['asOf'],
                                   'algorithm_sha': o['segment']['algorithmSha']} if o.get('segment') else None),
                               'score': o[f'{layer.lower()}_score'],
                               'threshold': o[f'{layer.lower()}_threshold'],
                               'label': label if eligible else None,
                               'label_available_at': str(pd.Timestamp(oracle.datetime.iloc[maturity]).date()) if maturity is not None else None,
                               'eligible': eligible and (layer != 'L2' or label is not None),
                               'period': period,
                               'evaluation_domain': 'TRUE_ANCHOR_TARGET_ELIGIBLE' if layer == 'L2' else 'ALL_ELIGIBLE_CANDIDATES'})
    return result
