"""A future corporate action cannot alter past PIT prices; never double-QFQ."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import model
from app.services.adj_factor import apply_adj_factor_with_as_of


def factor_fixture():
    n=75
    dates=pd.bdate_range('2026-05-01', periods=n)
    raw=pd.DataFrame({
        'open':np.linspace(100,110,n),
        'high':np.linspace(102,112,n),
        'low':np.linspace(98,108,n),
        'close':np.linspace(101,111,n),
        'volume':np.full(n,1000.0),
    },index=dates)
    factors=np.ones(n)
    factors[45:]=2.0
    fm=pd.DataFrame({'trade_date':dates,'adj_factor':factors})
    last=apply_adj_factor_with_as_of(raw,fm,dates[-1].date())
    p=pd.DataFrame(last[['open','high','low','close','volume']].to_numpy(),
                   columns=['open','high','low','close','volume'])
    p.insert(0,'datetime',dates)
    p['adj_factor']=factors
    for c in ('open','high','low','close','volume'):
        p['raw_'+c]=raw[c].to_numpy()
    return p,raw,fm


def test_pit_is_one_single_adjustment_from_raw_not_qfq_of_qfq():
    source,raw,factors=factor_fixture()
    for cutoff in (25, 46, 65):
        cutoff_source=source.iloc[:cutoff+1].copy().reset_index(drop=True)
        actual=model._pit_frame(cutoff_source, str(source.datetime.iloc[cutoff].date()))
        # Single use of Panji's authoritative pure function from RAW.
        expected=apply_adj_factor_with_as_of(
            raw.iloc[:cutoff+1], factors.iloc[:cutoff+1],
            source.datetime.iloc[cutoff].date())
        np.testing.assert_allclose(actual[['open','high','low','close','volume']],
                                   expected[['open','high','low','close','volume']],
                                   rtol=0,atol=1e-12)


def test_missing_raw_evidence_is_rejected_not_doubly_adjusted():
    source,_,_=factor_fixture()
    without=source.drop(columns=[f'raw_{c}' for c in ('open','high','low','close','volume')])
    with pytest.raises(model.AssetError,match='PIT_RAW_OHLCV_REQUIRED_NO_DOUBLE_ADJUST'):
        model._pit_frame(without, str(source.datetime.iloc[25].date()))


def test_future_factor_action_does_not_rewrite_prior_pit_input():
    source,_,_=factor_fixture()
    a=model._pit_frame(source.iloc[:41].reset_index(drop=True),
                       str(source.datetime.iloc[40].date()))
    b=model._pit_frame(source.iloc[:65].reset_index(drop=True),
                       str(source.datetime.iloc[40].date()))
    np.testing.assert_allclose(a[['open','high','low','close','volume']],
                               b[['open','high','low','close','volume']].iloc[:41],
                               rtol=0,atol=1e-12)


def test_canonical_adapter_reads_raw_and_qfq_from_same_mdas_owner(monkeypatch):
    """No local formula and no silent QFQ->RAW mislabel in the canonical bridge."""
    import asyncio
    from datetime import date
    from types import SimpleNamespace
    from app.strategy_integration import canonical_strategy_bars as bridge
    from app.services import market_data_aggregation_service as mds

    dates=pd.bdate_range('2026-07-01', periods=9)
    raw=pd.DataFrame({'open':np.full(9,100.),'high':np.full(9,102.),
                      'low':np.full(9,98.),'close':np.full(9,101.),
                      'volume':np.full(9,1000.)},index=dates)
    adjusted=raw.copy()
    adjusted.loc[:dates[3],['open','high','low','close']]*=0.5
    calls=[]
    async def fake_batch(_self, _session, ids, **kwargs):
        calls.append(kwargs['adj'])
        frame=adjusted if kwargs['adj']=='qfq' else raw
        return {1:SimpleNamespace(bars=frame, degraded=False,
                                  adj_factor_hash='pinned-factor-hash',
                                  source_bar_hash='pinned-bar-hash',
                                  market_data_contract_version='v1',
                                  data_source='DB_ONLY')}
    async def fake_factors(_session,_id,_asof):
        return pd.DataFrame({'trade_date':dates,'adj_factor':[1.]*4+[2.]*5})
    monkeypatch.setattr(mds.MarketDataAggregationService,'get_bars_batch',fake_batch)
    monkeypatch.setattr(bridge,'load_factor_series_for_pit',fake_factors)
    stocks, report=asyncio.run(bridge.load_official_qfq_stock_batch(
        None,{1:'SH:600000'},dates[-1].date(),min_bars=3))
    assert calls==['qfq','none']
    assert report['pit_factor_series_available'] is True
    frame=stocks['SH:600000']
    assert frame.loc[0,'open']==50.0     # MDAS QFQ, displayed directly
    assert frame.loc[0,'raw_open']==100.0  # Independently read MDAS RAW
    assert frame.loc[0,'adj_factor']==1.0
