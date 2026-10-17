"""B2 PIT input gate: authoritative daily factors never look ahead.

Pure unit fixtures; neither production database nor credentials are used.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import canonical_strategy_bars as cs
from app.strategy_integration import db_export as de


def _factors(day_list, values):
    return pd.DataFrame({'trade_date': day_list, 'adj_factor': values})


def test_valid_asof_factor_alignment_changes_only_on_effective_day():
    bars = pd.DatetimeIndex(['2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07'])
    f = _factors(['2026-01-01', '2026-01-06'], [1.1, 1.25])
    actual = cs._align_factor_series(bars, f)
    np.testing.assert_array_equal(actual, np.array([1.1, 1.1, 1.25, 1.25]))


def test_missing_initial_factor_cannot_be_backfilled_from_future():
    bars = pd.DatetimeIndex(['2026-01-02', '2026-01-05', '2026-01-06'])
    f = _factors(['2026-01-05'], [1.25])
    with pytest.raises(cs.CanonicalMarketDataError, match='PIT_FACTOR_INITIAL_COVERAGE_MISSING'):
        cs._align_factor_series(bars, f)


@pytest.mark.parametrize('factors', [
    _factors(['2026-01-01', '2026-01-01'], [1.0, 1.1]),
    _factors(['2026-01-01'], [0.0]),
    _factors(['2026-01-01'], [-1.0]),
    _factors(['2026-01-01'], [float('nan')]),
    _factors(['not-a-date'], [1.0]),
    _factors([], []),
])
def test_invalid_factor_series_is_rejected(factors):
    with pytest.raises(cs.CanonicalMarketDataError, match='INVALID_PIT_FACTOR_SERIES'):
        cs._align_factor_series(pd.DatetimeIndex(['2026-01-05']), factors)


def test_nonmonotonic_daily_bars_are_rejected():
    bars = pd.DatetimeIndex(['2026-01-05', '2026-01-02'])
    with pytest.raises(cs.CanonicalMarketDataError, match='INVALID_PIT_BAR_DATES'):
        cs._align_factor_series(bars, _factors(['2026-01-01'], [1.0]))


def _sample_price_frame():
    idx = pd.DatetimeIndex(['2026-01-02', '2026-01-05', '2026-01-06'])
    return pd.DataFrame({
        'open': [10., 10., 10.], 'high': [11., 11., 11.],
        'low': [9., 9., 9.], 'close': [10., 10., 10.],
        'volume': [100., 100., 100.],
    }, index=idx)


def test_missing_factor_coverage_excludes_instrument_without_synthesis(monkeypatch):
    from app.services import market_data_aggregation_service as mdas
    frame = _sample_price_frame()
    result = SimpleNamespace(
        bars=frame, degraded=False, adj_factor_hash='official-factor-hash',
        source_bar_hash='official-bars-hash',
        market_data_contract_version='v1', data_source='DB',
    )

    class FakeService:
        async def get_bars_batch(self, *args, **kwargs):
            return {101: result}

    async def incomplete_factor(*args, **kwargs):
        return _factors(['2026-01-05'], [1.2])

    monkeypatch.setattr(mdas, 'MarketDataAggregationService', FakeService)
    monkeypatch.setattr(cs, 'load_factor_series_for_pit', incomplete_factor)
    accepted, report = asyncio.run(cs.load_official_qfq_stock_batch(
        object(), {101: 'SH:600000'}, date(2026, 1, 6),
        min_bars=1, with_factor_series=True,
    ))
    assert accepted == {}
    assert report['excluded']['missing_authoritative_factor_series'] == 1
    assert not report['pit_factor_series_available']


def test_db_session_is_open_during_all_canonical_reads(monkeypatch):
    # INVALID_FIXTURE after fail-closed source gate: model an explicitly read-only session.
    monkeypatch.setenv('PANJI_STRATEGY_READ_ONLY_DATABASE', '1')
    monkeypatch.setattr('app.strategy_integration.readonly_runtime._installed', True)
    class FakeContext:
        def __init__(self):
            self.closed = False
            self.calls = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, typ, val, tb):
            self.closed = True
        async def execute(self, *_args, **_kwargs):
            assert not self.closed
            self.calls += 1
            if str(_args[0]) == 'SHOW transaction_read_only':
                return SimpleNamespace(scalar_one=lambda:'on')
            return SimpleNamespace(all=lambda: [SimpleNamespace(
                id=101, market='SH', symbol='600000')])

    ctx = FakeContext()
    observed = []
    async def fake_reader(db, symbols, as_of, **kwargs):
        observed.append((not db.closed, symbols, as_of))
        assert not db.closed
        return {'SH:600000': object()}, {'read_only': True}

    monkeypatch.setattr(de, 'AsyncSessionLocal', lambda: ctx)
    monkeypatch.setattr(de, 'load_official_qfq_stock_batch', fake_reader)
    accepted, report = asyncio.run(de.read_official_qfq_stock_batch(date(2026, 1, 6)))
    assert ctx.closed
    assert ctx.calls == 2
    assert observed == [(True, {101: 'SH:600000'}, date(2026, 1, 6))]
    assert list(accepted) == ['SH:600000']
    assert report['read_only'] is True
