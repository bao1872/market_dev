"""History read API pure test — no DB/network/provider calls."""
import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from app.api import strategy_candidate_research as route
from app.services.market_data_aggregation_service import MarketDataSourcePolicy

class _Rows:
    def scalar_one_or_none(self):
        return SimpleNamespace(id='fake-guid')

class _DB:
    async def execute(self, stmt):
        return _Rows()

class _Service:
    def __init__(self): self.opts=None
    async def get_bars(self, db, instrument_id, **kwargs):
        self.opts=kwargs
        ix=pd.bdate_range('2024-10-01',periods=500)
        v=[10+i/1000 for i in range(500)]
        frame=pd.DataFrame({'open':v,'high':[x+0.2 for x in v],
                            'low':[x-0.2 for x in v],
                            'close':[x+0.05 for x in v]},index=ix)
        return SimpleNamespace(bars=frame,degraded=False,
            adj_factor_hash='abc',source_bar_hash='def',history_exhausted=False)


def test_500_bars_stay_mdas_db_only_without_old_snapshot_overlay(monkeypatch):
    async def _latest(db): return '2026-10-15'
    service=_Service()
    monkeypatch.setattr(route, '_expected_latest_trade_day',_latest)
    monkeypatch.setattr(route, 'MarketDataAggregationService',lambda:service)
    result=asyncio.run(route.strategy_history_bars('SH:600000',500,None,_DB()))
    assert result['count']==500
    assert result['source_policy']=='MDAS_DB_ONLY'
    assert result['candidate_overlay_supported'] is False
    assert result['bars'][0]['date']=='2024-10-01'
    assert service.opts['source_policy']==MarketDataSourcePolicy.DB_ONLY
    assert service.opts['allow_backfill'] is False
    assert service.opts['include_realtime'] is False
    assert service.opts['completed_only'] is True
    assert service.opts['adj']=='qfq'
    assert service.opts['adjustment_as_of'].isoformat()=='2026-10-15'


def test_invalid_history_symbol_rejected_without_db_calls():
    with pytest.raises(HTTPException) as err:
        asyncio.run(route.strategy_history_bars('600000',500,None,_DB()))
    assert err.value.status_code==422
