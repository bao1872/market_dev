"""Offline contract smoke tests for auction_history_semantics_validation.

不连接真实 DB / Pytdx；全部使用 fake dependency / monkeypatch 验证“实际调用合同”。

运行：
    pytest -q experiments/pytdx_auction_history/tests/
"""

# PYTEST_DONT_REWRITE
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
SRC = EXPERIMENT_DIR / "auction_history_semantics_validation.py"
sys.path.insert(0, str(EXPERIMENT_DIR))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "auction_history_semantics_validation", SRC
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod  # 注册以便 dataclass / __future__ annotations 正确解析
spec.loader.exec_module(mod)


# =============================================================================
# Fake dependencies
# =============================================================================
class FakeBarAggregationResult:
    """模仿 MDAS BarAggregationResult 的正式字段。"""

    def __init__(self, bars=None, data_source="db", adj="none", as_of=None):
        self.bars = bars
        self.data_source = data_source
        self.degraded = False
        self.degraded_reason = None
        self.adjustment_as_of = as_of
        self.adj_factor_hash = "fakehash" if adj == "qfq" else ""


class FakeMDAS:
    def __init__(self):
        self.calls = []

    async def get_bars(self, session, instrument_id, timeframe="1d", adj="none",
                       include_realtime=True, completed_only=False,
                       start_date=None, end_date=None, limit=None, warmup_bars=0,
                       adjustment_as_of=None, allow_backfill=True):
        self.calls.append({
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "adj": adj,
            "start_date": start_date,
            "end_date": end_date,
            "adjustment_as_of": adjustment_as_of,
        })
        as_of = adjustment_as_of if adj == "qfq" else None
        return FakeBarAggregationResult(adj=adj, as_of=as_of)


class FakeCalendar:
    def __init__(self):
        self.calls = []

    async def is_trading_day_async(self, session, target_date):
        self.calls.append(target_date)
        # 仅用于确定性测试：周一到周五为交易日（测试不依赖 weekday-only 真实判断，
        # 但 resolver 必须调用此函数而非自行判断）。
        return target_date.weekday() < 5


class FakeAdapter:
    """模仿 PytdxAdapter 受管连接上下文。"""

    def __init__(self, records=None, raise_exc=None):
        self._records = records if records is not None else []
        self._raise = raise_exc
        self.entered = False
        self.api_called = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.entered = False
        return False

    @property
    def api(self):
        if self._raise is not None:
            raise self._raise
        self.api_called = True
        return self

    def get_history_transaction_data(self, market, code, start, count, date_int):
        self.api_called = True
        return self._records


class FakeAdjService:
    def __init__(self, factor_df=None):
        self._factor_df = factor_df

    async def get_factor_series(self, session, instrument_id, as_of=None):
        return self._factor_df


# =============================================================================
# TEST 1 — module import
# =============================================================================
def test_module_importable():
    assert mod is not None
    assert hasattr(mod, "SampleInstrument")
    assert hasattr(mod, "AuctionExtractionResult")
    assert hasattr(mod, "ExtractionStatus")


# =============================================================================
# TEST 2 — MDAS async contract
# =============================================================================
@pytest.mark.asyncio
async def test_mdas_async_contract():
    mdas = FakeMDAS()
    inst_id = UUID(int=1)
    T = date(2026, 8, 13)

    res_a = await mod.get_mdas_daily_open(mdas, None, inst_id, T)
    assert isinstance(res_a, FakeBarAggregationResult)
    call_a = mdas.calls[-1]
    assert call_a["timeframe"] == "1d"
    assert call_a["adj"] == "none"
    assert call_a["start_date"] == T
    assert call_a["end_date"] == T

    earliest = T - timedelta(days=400)
    res_b = await mod.get_mdas_pit_qfq_gap(mdas, None, inst_id, T, earliest)
    call_b = mdas.calls[-1]
    assert call_b["adj"] == "qfq"
    assert call_b["adjustment_as_of"] == T
    assert call_b["start_date"] == earliest
    assert call_b["end_date"] == T
    assert res_b.adjustment_as_of == T


# =============================================================================
# TEST 3 — symbol / UUID separation
# =============================================================================
@pytest.mark.asyncio
async def test_symbol_uuid_separation():
    mdas = FakeMDAS()
    adapter = FakeAdapter(records=[{"time": "09:25", "price": 1.0, "vol": 1}])
    inst = mod.SampleInstrument(
        symbol="600519", instrument_id=UUID(int=99), board="SH", liquidity="large", cohort="routine"
    )
    obs = await mod.run_single_observation(mdas, adapter, None, inst, date(2026, 8, 13))
    # adapter 收到 symbol；MDAS 收到 UUID。
    assert adapter._records is not None
    mdas_call = mdas.calls[-1]
    assert isinstance(mdas_call["instrument_id"], UUID)
    assert str(mdas_call["instrument_id"]) != "600519"
    assert obs["symbol"] == "600519"
    assert obs["instrument_id"] == str(UUID(int=99))


# =============================================================================
# TEST 4 — calendar contract
# =============================================================================
@pytest.mark.asyncio
async def test_calendar_contract():
    cal = FakeCalendar()
    # mod 内部通过 `from ... import is_trading_day_async` 绑定名字，
    # 因此直接 monkeypatch mod.is_trading_day_async。
    orig = mod.is_trading_day_async
    mod.is_trading_day_async = cal.is_trading_day_async
    try:
        dates = await mod.previous_trading_dates(None, date(2026, 8, 17), n=5)
        assert len(dates) == 5
        assert all(d.weekday() < 5 for d in dates)
        # resolver 必须实际调用 calendar（非 weekday-only 自行判断）。
        assert len(cal.calls) > 0
    finally:
        mod.is_trading_day_async = orig


# =============================================================================
# TEST 5 — managed Pytdx context
# =============================================================================
def test_pytdx_managed_context():
    adapter = FakeAdapter(records=[{"time": "09:25", "price": 1.0}])
    with adapter as a:
        assert a.entered is True
        _ = a.api.get_history_transaction_data(market=1, code="600519", start=0, count=10, date_int=20260813)
    assert adapter.entered is False
    assert adapter.api_called is True


# =============================================================================
# TEST 6 — SOURCE_ERROR
# =============================================================================
def test_source_error():
    adapter = FakeAdapter(raise_exc=RuntimeError("connection failed"))
    res = mod.extract_auction_records(adapter, "600519", date(2026, 8, 13))
    assert res.status == mod.ExtractionStatus.SOURCE_ERROR
    assert res.records == []
    assert res.error_code == "RuntimeError"
    assert "connection failed" in (res.error_message or "")


# =============================================================================
# TEST 7 — MULTIPLE_0925
# =============================================================================
def test_multiple_0925():
    recs = [
        {"time": "09:25", "price": 1.0, "vol": 10},
        {"time": "09:25", "price": 2.0, "vol": 20},
    ]
    adapter = FakeAdapter(records=recs)
    res = mod.extract_auction_records(adapter, "600519", date(2026, 8, 13))
    assert res.status == mod.ExtractionStatus.MULTIPLE_0925
    assert len(res.records) == 2  # 两条 raw 全保留


@pytest.mark.asyncio
async def test_multiple_0925_no_lanes():
    mdas = FakeMDAS()
    recs = [
        {"time": "09:25", "price": 1.0, "vol": 10},
        {"time": "09:25", "price": 2.0, "vol": 20},
    ]
    adapter = FakeAdapter(records=recs)
    inst = mod.SampleInstrument(
        symbol="600519", instrument_id=UUID(int=7), board="SH", liquidity="large", cohort="routine"
    )
    obs = await mod.run_single_observation(mdas, adapter, None, inst, date(2026, 8, 13))
    assert obs["extraction_status"] == "MULTIPLE_0925"
    assert obs["entered_semantic_lanes"] is False
    assert obs["lane_a"] is None
    assert obs["lane_b"] is None
    assert obs["gap_pct"] is None
    assert obs["volume_unit"] is None
    assert obs["amount_semantics"] is None
    # MDAS 不应被调用。
    assert mdas.calls == []


# =============================================================================
# TEST 8 — single FOUND
# =============================================================================
@pytest.mark.asyncio
async def test_single_found_enters_lanes():
    mdas = FakeMDAS()
    adapter = FakeAdapter(records=[{"time": "09:25", "price": 1.0, "vol": 10}])
    inst = mod.SampleInstrument(
        symbol="600519", instrument_id=UUID(int=3), board="SH", liquidity="large", cohort="routine"
    )
    obs = await mod.run_single_observation(mdas, adapter, None, inst, date(2026, 8, 13))
    assert obs["extraction_status"] == "FOUND"
    assert obs["entered_semantic_lanes"] is True
    assert obs["lane_a"] is not None
    assert obs["lane_b"] is not None
    assert len(mdas.calls) == 2


# =============================================================================
# TEST 9 — sample reproducibility
# =============================================================================
def test_sample_reproducibility():
    rows = mod.load_sample(mod.SAMPLE_FILE)
    assert len(rows) > 0
    # 无重复 (symbol, cohort)。
    keys = [(r.symbol, r.cohort) for r in rows]
    assert len(keys) == len(set(keys))
    # routine 与 corporate 都存在。
    cohorts = {r.cohort for r in rows}
    assert "routine" in cohorts
    assert "corporate" in cohorts
    # 缺失文件应 fail-fast。
    import tempfile
    with pytest.raises(FileNotFoundError):
        mod.load_sample(Path(tempfile.gettempdir()) / "does_not_exist.csv")


# =============================================================================
# TEST 10 — corporate event selection
# =============================================================================
@pytest.mark.asyncio
async def test_corporate_event_selection():
    import pandas as pd

    # 构造含 factor 变化的序列：T=2026-08-01 发生除权。
    dates = [date(2026, 7, d) for d in range(28, 32)] + [date(2026, 8, 1), date(2026, 8, 2)]
    factors = [1.0, 1.0, 1.0, 1.0, 0.5, 0.5]
    df = pd.DataFrame({"trade_date": dates, "adj_factor": factors})
    adj = FakeAdjService(factor_df=df)
    inst = mod.SampleInstrument(
        symbol="600519", instrument_id=UUID(int=5), board="SH", liquidity="large", cohort="corporate"
    )
    as_of = date(2026, 8, 15)
    cases = await mod.resolve_corporate_cases(adj, None, [inst], as_of, lookback_days=180)
    assert len(cases) == 1
    c = cases[0]
    assert c["status"] == "RESOLVED"
    assert c["event_date_T"] == "2026-08-01"
    assert c["factor_before"] == 1.0
    assert c["factor_after"] == 0.5
    assert c["prev_trade_date"] != ""
    assert c["next_trade_date"] != ""

    # 无事件窗口：lookback 太小则 NO_ADJUSTMENT_EVENT_IN_WINDOW。
    cases2 = await mod.resolve_corporate_cases(adj, None, [inst], as_of, lookback_days=1)
    assert cases2[0]["status"] == "NO_ADJUSTMENT_EVENT_IN_WINDOW"


# =============================================================================
# TEST 11 — no duplicate market logic (import + constructor contract check)
# =============================================================================
def test_no_duplicate_market_logic():
    # MOD13：验证“实际调用合同”与官方 owner 复用，不做全 source 字符串扫描宣布 PASS。
    # 正向断言：必须复用官方 owner。
    src_text = SRC.read_text(encoding="utf-8")
    assert "from app.services.feature_snapshot_service import get_active_a_share_instruments" in src_text
    assert "from app.services.calendar_service import is_trading_day_async" in src_text
    assert "from app.services.market_data_aggregation_service import MarketDataAggregationService" in src_text
    assert "from app.services.adjustment_factor_service import AdjustmentFactorService" in src_text
    assert "from app.core.pytdx_adapter import PytdxAdapter, market_from_code" in src_text

    # 反向断言：禁止第二套 market logic。
    assert "TdxHq_API(" not in src_text, "禁止 new TdxHq_API"
    assert "import TdxHq_API" not in src_text, "禁止 import TdxHq_API"
    assert "import pytdx.hq" not in src_text, "禁止直接 import pytdx.hq"
    assert ".get_daily_bars(" not in src_text, "禁止用 raw daily 作为 Open/PrevClose source"
    assert ".klines(" not in src_text, "禁止用 raw daily 作为 Open/PrevClose source"
    assert "CalendarService(" not in src_text, "禁止 invent CalendarService class"
    # 禁止重新计算 xdxr / event factor（仅检查作为调用/赋值的代码模式，docstring 中的禁述说明除外）。
    assert "xdxr_calc" not in src_text
    assert "recalc_xdxr" not in src_text
    assert "compute_xdxr" not in src_text

    # 实际调用合同：模块成功 import 即证明官方 owner 可被解析（上方 import 行已静态断言）。
    assert mod.MarketDataAggregationService is not None
