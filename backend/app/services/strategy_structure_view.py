"""Read-only frozen B55 structure-view adapter.

One algorithm owner: exact SHA-pinned frozen panji_lab engine (not reimplemented).
No LightGBM bundle, no predictions, no oracle labels, no DB writes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

ENGINE_SHA = "36c862bc318b5e53617b7527d5db166de3243addc85d2d1ae6a655574a93fb1a"
FROZEN_ASSETS = Path(__file__).resolve().parents[1] / 'strategy_integration' / 'frozen_reference'


class StructureUnavailable(RuntimeError):
    pass


def _frozen_engine():
    # Reuse the production-pinned loader; no new ZigZag formula or dynamic imports.
    from app.strategy_integration.model import AssetError, load_engine
    try:
        return load_engine(FROZEN_ASSETS)
    except (AssetError, OSError, ValueError) as exc:
        raise StructureUnavailable('FROZEN_B55_ENGINE_NOT_VERIFIED') from exc


def structure_from_verified_bars(payload: dict[str, Any]) -> dict[str, Any]:
    """Produce as-of-query structure from the exact MDAS read-path response.

    CAUTION: The full series uses the query's QFQ factor anchor. It is NOT an
    independently attested point-in-time feature frame for historical decisions;
    do not interpret pivot anchor dates as dates of advance knowledge.
    """
    if payload.get('source_policy') != 'MDAS_DB_ONLY' or payload.get('price_adjustment') != 'qfq':
        raise StructureUnavailable('NOT_CANONICAL_MDAS_DAILY_QFQ')
    if not payload.get('source_bar_hash') or not payload.get('adj_factor_hash'):
        raise StructureUnavailable('QFQ_PROVENANCE_MISSING')
    bars = payload.get('bars')
    if not isinstance(bars, list) or len(bars) < 11 or len(bars) != payload.get('count'):
        raise StructureUnavailable('STRUCTURE_WINDOW_TOO_SHORT_OR_INCONSISTENT')
    dates = [str(b['date']) for b in bars]
    if len(dates) != len(set(dates)) or dates != sorted(dates) or dates[-1] > payload['as_of']:
        raise StructureUnavailable('NON_MONOTONIC_STRUCTURE_WINDOW')
    raw = pd.DataFrame({
        'datetime': pd.to_datetime(dates),
        'open': [float(b['open']) for b in bars],
        'high': [float(b['high']) for b in bars],
        'low': [float(b['low']) for b in bars],
        'close': [float(b['close']) for b in bars],
        'volume': [0.0] * len(bars),
    })
    frozen = _frozen_engine()
    final, snapshots, audit = frozen.replay(raw)
    causal, ambiguous = frozen.causal_candidates(raw)
    # This is intentionally NOT ML score output. No B55 oracle truth data is
    # imported from labels; final pivots are a separate DISPLAY-ONLY layer.
    confirmed = []
    for p in final:
        i, cert = int(p['bar']), int(p['confirmed_at'])
        if not (0 <= i <= cert < len(bars)):
            raise StructureUnavailable('FROZEN_ENGINE_FUTURE_CERTIFICATION')
        confirmed.append({
            'anchor_date': dates[i], 'kind': p['type'], 'price': float(p['price']),
            'seen_at': dates[int(p['seen_at'])], 'confirmed_at': dates[cert],
        })
    early = [
        {'anchor_date': dates[j], 'visible_at': dates[j + 1], 'kind': kind,
         'price': float(price), 'model_scored': False}
        for j, kind, price in causal
    ]
    if len(snapshots) != len(bars):
        raise StructureUnavailable('FROZEN_ENGINE_SNAPSHOT_LENGTH_MISMATCH')
    return {
        'algorithm': 'frozen-panji-lab-B55-replay',
        'algorithm_sha256': ENGINE_SHA,
        'as_of': payload['as_of'],
        'source_bar_hash': payload['source_bar_hash'],
        'adj_factor_hash': payload['adj_factor_hash'],
        'coverage_start': dates[0], 'coverage_end': dates[-1],
        'confirmed_pivots': confirmed,
        'early_candidates': early,
        'latest_mutable_count': int(audit['latest_mutable']),
        'ambiguous_anchor_count': int(ambiguous),
        'confirm_lineage': 'FROZEN_REPLAY_AS_OF_QUERY_QFQ',
        'decision_pit_verified': False,
        'hindsight_label_used_for_model_scoring': False,
        'raw_ohlcv_volume_used_for_features': False,
        'notes': '折线仅显示查询截止日已经确认的结构；锚点日期不等于确认日期。当前QFQ视图不声称逐历史决策日PIT复权；早期候选无L1/L2预测。',
    }
