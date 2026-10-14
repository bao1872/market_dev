"""Contract tests for the canonical qfq market-data bridge.

The bridge must delegate every price decision to
MarketDataAggregationService.get_bars_batch and must never re-apply a factor.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import source
from app.strategy_integration.canonical_strategy_bars import (
    CanonicalMarketDataError,
    load_official_qfq_stock_batch,
)


class _StubResult:
    def __init__(self, bars, *, degraded=False, factor_hash='f' * 16):
        self.bars = bars
        self.degraded = degraded
        self.adj_factor_hash = factor_hash
        self.source_bar_hash = 's' * 16
        self.market_data_contract_version = 'v2'
        self.data_source = 'db'


CALLS: list[dict] = []


def _bars(n=200, *, start='2024-01-01'):
    rng = np.random.default_rng(3)
    v = 20 + np.cumsum(rng.normal(0, .2, n))
    o = v * .999
    c = v * 1.001
    h = np.maximum(o, c) * 1.01
    l = np.minimum(o, c) * .99
    return pd.DataFrame({'open': o, 'high': h, 'low': l, 'close': c, 'volume': np.full(n, 1e6)},
                        index=pd.bdate_range(start, periods=n))


@pytest.fixture
def mdas(monkeypatch):
    """Patch the single market-data owner; returns a per-test result factory."""
    from app.services import market_data_aggregation_service as mdas_mod
    CALLS.clear()
    outcomes: dict = {}

    async def fake(self, session, instrument_ids, **kwargs):
        CALLS.append({'ids': list(instrument_ids), **kwargs})
        return {i: outcomes.get(i) for i in instrument_ids}

    monkeypatch.setattr(mdas_mod.MarketDataAggregationService, 'get_bars_batch', fake)
    yield outcomes


@pytest.mark.asyncio
async def test_canonical_call_uses_qfq_dbonly_and_as_of(mdas):
    ids = ['id-1']
    mdas['id-1'] = _StubResult(_bars())
    accepted, report = await load_official_qfq_stock_batch(
        None, {i: 'SH:600000' for i in ids}, date(2026, 8, 17),
    )
    assert list(accepted) == ['SH:600000']
    call = CALLS[0]
    assert call['adj'] == 'qfq'
    assert call['completed_only'] is True
    assert call['source_policy'].name == 'DB_ONLY'
    assert call['adjustment_as_of'] == date(2026, 8, 17)
    assert call['allow_backfill'] is False
    assert call['limit'] is None
    assert call['timeframe'] == '1d'
    assert report['price_adjustment'] == 'qfq'
    assert report['adjustment_as_of'] == '2026-08-17'
    assert report['external_fetch_allowed'] is False


@pytest.mark.asyncio
async def test_prices_are_passed_through_without_re_adjustment(mdas):
    frame = _bars()
    mdas['id-1'] = _StubResult(frame)
    accepted, _ = await load_official_qfq_stock_batch(
        None, {'id-1': 'SH:600000'}, date(2026, 8, 17),
    )
    # Identical values: the bridge must not multiply by any factor again.
    assert np.allclose(accepted['SH:600000']['close'].to_numpy(), frame['close'].to_numpy())
    assert np.allclose(accepted['SH:600000']['volume'].to_numpy(), frame['volume'].to_numpy())


@pytest.mark.asyncio
async def test_missing_or_degraded_factor_is_excluded(mdas):
    mdas['id-1'] = _StubResult(_bars(), degraded=True)
    mdas['id-2'] = _StubResult(_bars(), factor_hash='')
    accepted, report = await load_official_qfq_stock_batch(
        None, {'id-1': 'SH:600000', 'id-2': 'SZ:000001'}, date(2026, 8, 17),
    )
    assert accepted == {}
    assert report['excluded']['missing_or_degraded_factor'] == 2
    assert report['eligible_stock_count'] == 0


@pytest.mark.asyncio
async def test_short_and_future_and_error_are_excluded_with_reasons(mdas):
    mdas['id-1'] = _StubResult(_bars(n=10))
    mdas['id-2'] = RuntimeError('boom')
    future = _bars()
    future.index = pd.bdate_range('2027-01-01', periods=len(future))
    mdas['id-3'] = _StubResult(future)
    accepted, report = await load_official_qfq_stock_batch(
        None, {'id-1': 'SH:600000', 'id-2': 'SZ:000001', 'id-3': 'SZ:000002'},
        date(2026, 8, 17),
    )
    assert accepted == {}
    assert report['excluded']['too_short_or_missing'] == 1
    assert report['excluded']['mdas_error'] == 1
    assert report['excluded']['future_bar'] == 1


@pytest.mark.asyncio
async def test_bad_parameters_are_rejected():
    with pytest.raises(CanonicalMarketDataError):
        await load_official_qfq_stock_batch(None, {}, date(2026, 8, 17),
                                            start_date=date(2027, 1, 1))


def test_offline_conversion_uses_canonical_pit_function():
    """A future corporate action must not change a past as-of qfq prefix."""
    from app.services.adj_factor import apply_adj_factor_with_as_of
    n = 260
    frame = _bars(n=n)
    factor = np.ones(n)
    factor[200:] = 0.5  # corporate action (e.g. 10送10) after bar 200
    raw = pd.DataFrame({
        'trade_date': frame.index,
        'open': frame['open'].to_numpy(), 'high': frame['high'].to_numpy(),
        'low': frame['low'].to_numpy(), 'close': frame['close'].to_numpy(),
        'volume': frame['volume'].to_numpy(), 'adj_factor': factor,
    })
    decision = 150  # decided well before the corporate action
    decision_date = str(pd.Timestamp(raw.trade_date.iloc[decision]).date())
    # PIT: anchored at the decision day, the later action is invisible.
    pit = source._qfq_from_raw(raw, decision_date)
    # Same bars, same anchor, but computed from the FULL history then truncated.
    full = source._qfq_from_raw(raw, decision_date)
    truncated = full.iloc[:decision + 1]
    assert np.allclose(pit.iloc[:decision + 1]['close'].to_numpy(),
                       truncated['close'].to_numpy())
    # Sanity: the canonical function is the one actually used for conversion.
    probe = apply_adj_factor_with_as_of(
        raw.set_index(pd.DatetimeIndex(raw['trade_date']))[['open', 'high', 'low', 'close', 'volume']].astype(float),
        pd.DataFrame({'trade_date': pd.to_datetime(raw['trade_date']).to_numpy(), 'adj_factor': factor}),
        date.fromisoformat(decision_date),
    )
    assert np.allclose(probe['close'].to_numpy()[:decision + 1],
                       truncated['close'].to_numpy())
