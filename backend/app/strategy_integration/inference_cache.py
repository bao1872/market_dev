"""Content-addressed prefix cache; future rows never contribute to the key.

Only decision-local engine outputs are cached. Run-date chart scaling and
evaluation labels are derived again by their existing owners.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd
from app.services.strategy_tv_zigzag import current_segment, identity as tv_identity

from .release_store import atomic_json


def decision_inputs(runtime, prefix, day, symbol):
    def compute():
        result = runtime.score(prefix, day)
        _, snapshots, _ = runtime.engine.replay(prefix)
        causal, _ = runtime.engine.causal_candidates(prefix)
        t = len(prefix)-1
        def confirmed_tail(i):
            visible = [p for p in snapshots[i] if p.get('confirmed_at') is not None and p['confirmed_at'] <= i]
            # Consumers need the latest confirmed pivot and the previous
            # opposite reference, never the complete historical pivot array.
            keep = {id(p) for kind in ('H', 'L') for p in [p for p in visible if p['type'] == kind][-2:]}
            return [p for p in visible if id(p) in keep]
        return {'result': result,
            'tv_segment': current_segment(prefix),
            'snapshots': {str(i): confirmed_tail(i) for i in range(max(0, t-3), t+1)},
            'causal': [(int(j), k, float(p)) for j, k, p in causal if 1 <= t-int(j) <= 3]}

    root = os.getenv('PANJI_STRATEGY_CACHE_ROOT')
    if not root:
        value = compute()
    else:
        base = Path(root)
        if not base.is_absolute():
            raise ValueError('INFERENCE_CACHE_ROOT_NOT_ABSOLUTE')
        contract = hashlib.sha256(json.dumps(runtime.manifest, sort_keys=True).encode()).hexdigest()
        source = hashlib.sha256(pd.util.hash_pandas_object(
            prefix[['datetime','open','high','low','close','volume']], index=False).to_numpy().tobytes()).hexdigest()
        key = hashlib.sha256(f'{contract}|{source}|{day}|{tv_identity()}|prefix-v4'.encode()).hexdigest()
        folder = base/hashlib.sha256(symbol.encode()).hexdigest()[:24]
        path = folder/(key+'.json')
        if path.is_file():
            if path.stat().st_size > 1024*1024:
                raise ValueError('INFERENCE_CACHE_ENTRY_TOO_LARGE')
            record = json.loads(path.read_text())
            value = record['value']
            encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
            if record.get('key') != key or hashlib.sha256(encoded).hexdigest() != record.get('sha256'):
                raise ValueError('INFERENCE_CACHE_INTEGRITY_FAILED')
        else:
            value = compute()
            # Normalize NumPy scalar types without altering frozen calculations.
            value = json.loads(json.dumps(value, default=lambda v: v.item()))
            encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
            atomic_json(path, {'key': key, 'sha256': hashlib.sha256(encoded).hexdigest(), 'value': value})
    return value['result'], {int(i): v for i, v in value['snapshots'].items()}, value['causal'], value['tv_segment']
