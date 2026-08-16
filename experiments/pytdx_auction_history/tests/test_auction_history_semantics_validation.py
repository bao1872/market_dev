# PYTEST_DONT_REWRITE
"""Offline smoke / measurement-pipeline tests for Round 1C runner.

不连接真实 DB / Pytdx；依赖 fake dependency 注入。
使用 APP_ENV=test + PURE_UNIT_TEST=1 + 本地 venv 运行。
"""
import importlib.util
import os
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

import pandas as pd
import pytest

EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
RUNNER_PATH = EXPERIMENT_DIR / "auction_history_semantics_validation.py"

DUMMY_DB = "postgresql+psycopg://bz:secret@localhost:5432/bz_stock_test"
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", DUMMY_DB)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("PURE_UNIT_TEST", "1")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "auction_hist_semval_1c", str(RUNNER_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ---------------------------------------------------------------------------
# Helpers: fake MDAS BarAggregationResult + fake PytdxAdapter
# ---------------------------------------------------------------------------
class FakeBars:
    """构建一个以 trade_date 为索引的 daily bars DataFrame。"""

    def __init__(self, data):
        # data: list of (date, open, high, low, close, volume, amount)
        idx = [pd.Timestamp(d) for d, *_ in data]
        rows = []
        for _, o, h, l, c, v, a in data:
            rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v, "amount": a})
        self.df = pd.DataFrame(rows, index=pd.Index(idx, name="trade_date"))


class FakeMDASResult:
    def __init__(self, bars, data_source="db", degraded=False, degraded_reason=None,
                 adjustment_as_of=None, adj_factor_hash=""):
        self.bars = bars.df if isinstance(bars, FakeBars) else bars
        self.data_source = data_source
        self.as_of = None
        self.is_partial = False
        self.last_persisted_bar_time = None
        self.last_live_bar_time = None
        self.freshness_seconds = 0.0
        self.degraded = degraded
        self.degraded_reason = degraded_reason
        self.cache_hit = False
        self.warmup_bars_full = None
        self.market_data_contract_version = "v2"
        self.source_bar_hash = "x"
        self.adj_factor_hash = adj_factor_hash
        self.adjustment_as_of = adjustment_as_of
        self.completed_through = None
        self.requested_count = None
        self.actual_count = 0
        self.coverage_start = None
        self.coverage_end = None
        self.history_exhausted = False
        self.backfill_rounds = 0
        self.coverage_reason = ""
        self.latest_daily_quote = None


class FakeMDAS:
    """按 (instrument_id, adj, target_date) 返回预设 bars。"""

    def __init__(self, table):
        # table: dict[(instrument_id, adj, target_date)] -> FakeMDASResult
        self._table = table
        self.call_log = []

    async def get_bars(self, session, instrument_id, **kwargs):
        adj = kwargs.get("adj")
        target_date = kwargs.get("end_date")
        key = (instrument_id, adj, target_date)
        self.call_log.append(key)
        res = self._table.get(key)
        if res is None:
            return FakeMDASResult(FakeBars([]))
        return res


class FakeAdapter:
    """PytdxAdapter 受管连接 fake；.api.get_history_transaction_data 返回预设。"""

    def __init__(self, txn_table):
        self._txn_table = txn_table  # dict[(symbol, date_int)] -> list[dict]
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        self.exited = True

    @property
    def api(self):
        return self

    def get_history_transaction_data(self, market, code, start, count, date_int):
        return self._txn_table.get((code, date_int), []) or []


# ---------------------------------------------------------------------------
# TEST 1 — import + SampleInstrument DTO correctness
# ---------------------------------------------------------------------------
def test_import_and_dto(mod):
    assert hasattr(mod, "SampleInstrument")
    assert hasattr(mod, "NormalizedAuctionTransaction")
    assert hasattr(mod, "AuctionExtractionResult")
    s = mod.SampleInstrument(
        symbol="600519", market="SH", instrument_id=UUID(int=1), board="SH_MAIN",
        coverage_tag="large_cap_reference", cohort="routine",
    )
    assert s.symbol == "600519" and s.market == "SH" and s.cohort == "routine"


# ---------------------------------------------------------------------------
# TEST 2 — MDAS async + BarAggregationResult attribute access
# ---------------------------------------------------------------------------
def test_mdas_async_and_attributes(mod):
    bars = FakeBars([(date(2024, 1, 2), 10.0, 10.5, 9.8, 10.2, 1000, 10000)])
    res = FakeMDASResult(bars, data_source="db", adj_factor_hash="abc123")
    assert res.bars is not None
    # 模拟调用方只读正式字段（不触发内部错误）
    _ = (res.bars, res.data_source, res.degraded, res.degraded_reason,
         res.adjustment_as_of, res.adj_factor_hash, res.market_data_contract_version)
    assert res.data_source == "db"
    assert res.adj_factor_hash == "abc123"


# ---------------------------------------------------------------------------
# TEST 3 — Calendar contract: async fn, no CalendarService class
# ---------------------------------------------------------------------------
def test_calendar_contract(mod, monkeypatch):
    async def fake_cal(session, d):
        return d.weekday() < 5
    monkeypatch.setattr(mod, "is_trading_day_async", fake_cal)

    async def run():
        out = await mod.previous_trading_dates(None, date(2024, 1, 8), n=3)
        return out
    out = asyncio_run(run())
    assert len(out) == 3
    # 2024-01-08 周一（含）+ 向前：01-05(五),01-04(四)
    assert out[0] == date(2024, 1, 8)
    assert out[1] == date(2024, 1, 5)
    assert out[2] == date(2024, 1, 4)


# ---------------------------------------------------------------------------
# TEST 4 — Symbol/UUID separation in resolve
# ---------------------------------------------------------------------------
def test_symbol_uuid_separation(mod, monkeypatch):
    samples = [
        mod.SampleInstrument(symbol="600519", market="SH", instrument_id=UUID(int=0),
                             board="SH_MAIN", coverage_tag="large_cap_reference", cohort="routine"),
    ]
    calls = {}

    async def fake_universe(session):
        return [UUID(int=1)]
    monkeypatch.setattr(mod, "get_active_a_share_instruments", fake_universe)

    # 提供 (SH,600519) -> UUID1
    class FakeRow(dict):
        pass

    async def fake_execute(stmt):
        return [{"market": "SH", "symbol": "600519", "id": UUID(int=1)}]
    fake_session = _FakeSession(fake_execute)

    async def run():
        return await mod.resolve_sample_instruments(fake_session, samples)
    resolved, skipped = asyncio_run(run())
    assert len(resolved) == 1
    assert resolved[0].instrument_id == UUID(int=1)
    assert resolved[0].symbol == "600519"
    # 没有把 instrument_id 直接当 symbol 传给 MDAS
    assert resolved[0].instrument_id != UUID(int=0)


# ---------------------------------------------------------------------------
# TEST 5 — Pytdx managed lifecycle (context manager)
# ---------------------------------------------------------------------------
def test_pytdx_managed_lifecycle(mod):
    adapter = FakeAdapter({("600519", 20240102): []})
    with adapter as a:
        assert a.entered is True
    assert adapter.exited is True
    # market_from_code 来自官方 owner
    assert mod.market_from_code("600519") is not None


# ---------------------------------------------------------------------------
# TEST 6 — SOURCE_ERROR structured
# ---------------------------------------------------------------------------
def test_source_error_structured(mod):
    class BoomAdapter(FakeAdapter):
        @property
        def api(self):
            raise RuntimeError("adapter not connected")
    adapter = BoomAdapter({})
    res = mod.extract_auction_records(adapter, "600519", date(2024, 1, 2))
    assert res.status == mod.ExtractionStatus.SOURCE_ERROR
    assert res.records == []
    assert res.error_code == "RuntimeError"
    assert "adapter not connected" in (res.error_message or "")


# ---------------------------------------------------------------------------
# TEST 7 — MULTIPLE_0925 keeps all raw, stops semantic agg
# ---------------------------------------------------------------------------
def test_multiple_0925_keeps_all_raw(mod):
    txns = [
        {"time": "09:25", "price": 10.0, "vol": 100, "buyorsell": 1},
        {"time": "09:25", "price": 10.1, "vol": 120, "buyorsell": 0},
    ]
    adapter = FakeAdapter({("600519", 20240102): txns})
    res = mod.extract_auction_records(adapter, "600519", date(2024, 1, 2))
    assert res.status == mod.ExtractionStatus.MULTIPLE_0925
    assert len(res.records) == 2
    assert res.records[0].raw_price == 10.0
    assert res.records[1].raw_price == 10.1


# ---------------------------------------------------------------------------
# TEST 8 — single canonical FOUND
# ---------------------------------------------------------------------------
def test_single_found(mod):
    txns = [{"time": "09:25", "price": 10.0, "vol": 100, "buyorsell": 1}]
    adapter = FakeAdapter({("600519", 20240102): txns})
    res = mod.extract_auction_records(adapter, "600519", date(2024, 1, 2))
    assert res.status == mod.ExtractionStatus.FOUND
    assert len(res.records) == 1


# ---------------------------------------------------------------------------
# TEST 9 — tracked sample schema + board coverage (MOD14 / TEST J)
# ---------------------------------------------------------------------------
def test_sample_schema_and_board_coverage(mod):
    samples = mod.load_sample(mod.SAMPLE_FILE)
    boards = {s.board for s in samples if s.cohort == "routine"}
    assert "SH_MAIN" in boards
    assert "SZ_MAIN" in boards
    assert "CHINEXT" in boards
    assert "STAR" in boards
    routine = [s for s in samples if s.cohort == "routine"]
    assert len(routine) >= 20
    # 无伪 liquidity tier：coverage_tag 明确标注非正式分类
    for s in samples:
        assert s.coverage_tag in {
            "large_cap_reference", "ordinary_reference", "lower_activity_candidate", "corporate"
        }


# ---------------------------------------------------------------------------
# TEST A — resolved corporate identity must be non-zero UUID (MOD1)
# ---------------------------------------------------------------------------
def test_resolved_corporate_identity_no_uuid_zero(mod, monkeypatch):
    # corporate sample 必须先 resolve（UUID != 0）才能进 resolver
    corp = [
        mod.SampleInstrument(symbol="600000", market="SH", instrument_id=UUID(int=7),
                             board="SH_MAIN", coverage_tag="corporate", cohort="corporate"),
    ]
    # 若传入 UUID(0) 必须 raise
    bad = [
        mod.SampleInstrument(symbol="600000", market="SH", instrument_id=UUID(int=0),
                             board="SH_MAIN", coverage_tag="corporate", cohort="corporate"),
    ]
    async def fake_adj(session, inst_id, as_of):
        # 返回一个含 factor-change 的 DataFrame
        import pandas as pd
        from datetime import date, timedelta
        d0 = as_of - timedelta(days=30)
        df = pd.DataFrame({
            "trade_date": [d0 - timedelta(days=1), d0, d0 + timedelta(days=1)],
            "adj_factor": [1.0, 2.0, 2.0],
        })
        return df
    monkeypatch.setattr(mod, "AdjustmentFactorService", _FakeAdjService(fake_adj))

    async def run_good():
        return await mod.resolve_corporate_cases(_FakeAdjService(fake_adj), _FakeSessionAsync(), corp, date(2024, 6, 1), 180)
    out = asyncio_run(run_good())
    assert out[0]["status"] == "RESOLVED"
    assert out[0]["instrument_id"] != str(UUID(int=0))

    async def run_bad():
        return await mod.resolve_corporate_cases(_FakeAdjService(fake_adj), _FakeSessionAsync(), bad, date(2024, 6, 1), 180)
    with pytest.raises(ValueError, match="INTERNAL_IDENTITY_ERROR"):
        asyncio_run(run_bad())


# ---------------------------------------------------------------------------
# TEST B — market + symbol identity (MOD2)
# ---------------------------------------------------------------------------
def test_market_symbol_identity(mod, monkeypatch):
    samples = [
        mod.SampleInstrument(symbol="000001", market="SZ", instrument_id=UUID(int=0),
                             board="SZ_MAIN", coverage_tag="large_cap_reference", cohort="routine"),
        mod.SampleInstrument(symbol="600519", market="SH", instrument_id=UUID(int=0),
                             board="SH_MAIN", coverage_tag="large_cap_reference", cohort="routine"),
        # 同 symbol 不同 market 不得冲突
        mod.SampleInstrument(symbol="600519", market="SZ", instrument_id=UUID(int=0),
                             board="SZ_MAIN", coverage_tag="ordinary_reference", cohort="routine"),
    ]
    async def fake_universe(session):
        return [UUID(int=10), UUID(int=11), UUID(int=12)]
    monkeypatch.setattr(mod, "get_active_a_share_instruments", fake_universe)

    async def fake_execute(stmt):
        return [
            {"market": "SZ", "symbol": "000001", "id": UUID(int=10)},
            {"market": "SH", "symbol": "600519", "id": UUID(int=11)},
            {"market": "SZ", "symbol": "600519", "id": UUID(int=12)},
        ]
    sess = _FakeSession(fake_execute)

    async def run():
        return await mod.resolve_sample_instruments(sess, samples)
    resolved, skipped = asyncio_run(run())
    assert len(resolved) == 3
    by_key = {(r.market, r.symbol): r.instrument_id for r in resolved}
    assert by_key[("SZ", "000001")] == UUID(int=10)
    assert by_key[("SH", "600519")] == UUID(int=11)
    assert by_key[("SZ", "600519")] == UUID(int=12)


# ---------------------------------------------------------------------------
# TEST C — exact time normalization (MOD3)
# ---------------------------------------------------------------------------
def test_exact_time_normalization(mod):
    assert mod._normalize_auction_time("09:25") == "09:25"
    assert mod._normalize_auction_time("09:25:00") == "09:25"
    assert mod._normalize_auction_time("09:25:01") is None
    assert mod._normalize_auction_time("09:25:59") is None
    assert mod._normalize_auction_time("09:24:59") is None


# ---------------------------------------------------------------------------
# TEST D — Lane A numeric comparison (MOD5)
# ---------------------------------------------------------------------------
def test_lane_a_comparison(mod):
    bars = FakeBars([(date(2024, 1, 2), 10.00, 10.5, 9.8, 10.00, 1000, 10000)])
    bar_T = mod.get_bar_for_date(bars.df, date(2024, 1, 2))
    r1 = mod.compute_lane_a(10.00, bar_T, "db", False, None)
    assert r1["price_exact_match"] is True
    assert r1["price_diff_abs"] == 0.0
    assert r1["price_diff_rel"] == 0.0

    r2 = mod.compute_lane_a(10.01, bar_T, "db", False, None)
    assert r2["price_exact_match"] is False
    assert abs(r2["price_diff_abs"] - 0.01) < 1e-9
    assert abs(r2["price_diff_rel"] - 0.001) < 1e-9

    # MDAS 无 T bar → LANE_A_MISSING_MDA_OPEN，不填 0
    r3 = mod.compute_lane_a(10.0, None, "db", False, None)
    assert r3["status"] == "LANE_A_MISSING_MDA_OPEN"
    assert r3["mdas_raw_open_T"] is None
    assert r3["price_diff_abs"] is None


# ---------------------------------------------------------------------------
# TEST E — PIT Gap corporate math (MOD6)
# ---------------------------------------------------------------------------
def test_pit_gap_corporate(mod):
    # raw (adj=none): near 20 close, 10.2 open
    raw_bars = FakeBars([
        (date(2024, 1, 1), 19.0, 20.0, 18.0, 20.0, 100, 2000),
        (date(2024, 1, 2), 10.2, 10.5, 10.0, 10.3, 100, 1000),
    ])
    # qfq (adj=qfq, as_of=T): factor halved -> Tm1 close ~10, T open ~10.2
    qfq_bars = FakeBars([
        (date(2024, 1, 1), 9.8, 10.0, 9.5, 10.0, 100, 2000),
        (date(2024, 1, 2), 10.2, 10.5, 10.0, 10.3, 100, 1000),
    ])
    auction = 10.2
    raw_Tm1 = mod.get_prev_bar_before(raw_bars.df, date(2024, 1, 2))
    raw_T = mod.get_bar_for_date(raw_bars.df, date(2024, 1, 2))
    qfq_Tm1 = mod.get_prev_bar_before(qfq_bars.df, date(2024, 1, 2))
    qfq_T = mod.get_bar_for_date(qfq_bars.df, date(2024, 1, 2))
    lb = mod.compute_lane_b(auction, raw_Tm1, raw_T, qfq_Tm1, qfq_T,
                             date(2024, 1, 2), "hash", "db", False, None)
    # naive_raw_gap = 10.2/20 - 1 = -0.49
    assert abs(lb["naive_raw_gap"] - (-0.49)) < 1e-9
    # pit_gap = 10.2/10 - 1 = +0.02
    assert abs(lb["pit_gap"] - 0.02) < 1e-9
    assert abs(lb["raw_close_Tm1"] - 20.0) < 1e-9
    assert abs(lb["qfq_close_Tm1"] - 10.0) < 1e-9


# ---------------------------------------------------------------------------
# TEST F — volume multiplier evidence (MOD8)
# ---------------------------------------------------------------------------
def test_volume_multiplier_evidence(mod):
    ve = mod.compute_volume_evidence(10.0, 100.0, 100000.0)
    assert abs(ve["implied_multiplier"] - 100.0) < 1e-9
    assert ve["reason"] == "COMPUTED_PRICE_VOLUME_AMOUNT"
    # 字段缺失 → None + reason
    ve2 = mod.compute_volume_evidence(10.0, 100.0, None)
    assert ve2["implied_multiplier"] is None
    assert ve2["reason"] == "RAW_AMOUNT_FIELD_ABSENT"


# ---------------------------------------------------------------------------
# TEST G — live flag does not mean PASS (MOD12)
# ---------------------------------------------------------------------------
def test_live_flag_not_pass(mod):
    # 全部 SOURCE_ERROR：live=True 但不得 PASS
    obs = [{
        "extraction_status": "SOURCE_ERROR", "cohort": "routine",
        "lane_a": None, "lane_b": None, "volume_evidence": None, "amount_evidence": None,
    }]
    status = mod.derive_live_status(obs, [])
    assert status["LIVE_RUN_STATUS"] == "COMPLETED"
    assert status["EVIDENCE_COMPLETENESS"] == "INSUFFICIENT"
    assert status["EVIDENCE_COMPLETENESS"] != "PASS"


# ---------------------------------------------------------------------------
# TEST H — raw evidence persistence (MULTIPLE_0925 all raw kept)
# ---------------------------------------------------------------------------
def test_raw_evidence_persistence(mod, tmp_path, monkeypatch):
    txns = [
        {"time": "09:25", "price": 10.0, "vol": 100, "buyorsell": 1},
        {"time": "09:25", "price": 10.1, "vol": 120, "buyorsell": 0},
    ]
    adapter = FakeAdapter({("600519", 20240102): txns})
    mdas = FakeMDAS({})
    inst = mod.SampleInstrument(symbol="600519", market="SH", instrument_id=UUID(int=1),
                               board="SH_MAIN", coverage_tag="large_cap_reference", cohort="routine")
    obs = asyncio_run(mod.run_single_observation(mdas, adapter, None, inst, date(2024, 1, 2)))
    assert obs["extraction_status"] == "MULTIPLE_0925"
    assert obs["raw_record_count"] == 2
    # 不进入任何 lane
    assert obs["lane_a"] is None
    assert obs["volume_evidence"] is None


# ---------------------------------------------------------------------------
# TEST I — corporate evidence persistence (factor_before/after + gaps)
# ---------------------------------------------------------------------------
def test_corporate_evidence_persistence(mod):
    # 真实除权：Tm1 raw close=20, T raw open=10.2; qfq Tm1 close=10, T open=10.2
    raw_bars = FakeBars([
        (date(2024, 1, 1), 19.0, 20.0, 18.0, 20.0, 100, 2000),
        (date(2024, 1, 2), 10.2, 10.5, 10.0, 10.3, 100, 1000),
    ])
    qfq_bars = FakeBars([
        (date(2024, 1, 1), 9.8, 10.0, 9.5, 10.0, 100, 2000),
        (date(2024, 1, 2), 10.2, 10.5, 10.0, 10.3, 100, 1000),
    ])
    mdas = FakeMDAS({
        (UUID(int=1), "none", date(2024, 1, 2)): FakeMDASResult(raw_bars),
        (UUID(int=1), "qfq", date(2024, 1, 2)): FakeMDASResult(qfq_bars),
    })
    adapter = FakeAdapter({("600519", 20240102): [{"time": "09:25", "price": 10.2, "vol": 100, "buyorsell": 1}]})
    inst = mod.SampleInstrument(symbol="600519", market="SH", instrument_id=UUID(int=1),
                               board="SH_MAIN", coverage_tag="corporate", cohort="corporate")
    obs = asyncio_run(mod.run_corporate_observation(
        mdas, adapter, None, inst, date(2024, 1, 2), factor_before=1.0, factor_after=2.0))
    assert obs["extraction_status"] == "FOUND"
    assert obs["corporate"]["factor_before"] == 1.0
    assert obs["corporate"]["factor_after"] == 2.0
    # gap_adjustment_effect = pit_gap - naive_raw_gap = 0.02 - (-0.49) = 0.51
    assert abs(obs["corporate"]["gap_adjustment_effect"] - 0.51) < 1e-9
    assert abs(obs["lane_b"]["naive_raw_gap"] - (-0.49)) < 1e-9
    assert abs(obs["lane_b"]["pit_gap"] - 0.02) < 1e-9


# ---------------------------------------------------------------------------
# TEST K — no placeholders in completed observation (MOD15)
# ---------------------------------------------------------------------------
def test_no_placeholders(mod):
    raw_bars = FakeBars([(date(2024, 1, 2), 10.0, 10.5, 9.8, 10.0, 1000, 10000)])
    mdas = FakeMDAS({
        (UUID(int=1), "none", date(2024, 1, 2)): FakeMDASResult(raw_bars),
        (UUID(int=1), "qfq", date(2024, 1, 2)): FakeMDASResult(raw_bars),
    })
    adapter = FakeAdapter({("600519", 20240102): [{"time": "09:25", "price": 10.0, "vol": 100, "buyorsell": 1}]})
    inst = mod.SampleInstrument(symbol="600519", market="SH", instrument_id=UUID(int=1),
                               board="SH_MAIN", coverage_tag="large_cap_reference", cohort="routine")
    obs = asyncio_run(mod.run_single_observation(mdas, adapter, None, inst, date(2024, 1, 2)))
    assert obs["extraction_status"] == "FOUND"
    # 不允许占位字符串
    flat = mod._flatten_observation(obs)
    for k, v in flat.items():
        assert v != "pending_live_data", f"{k} 出现 pending_live_data 占位"
        assert v != "unknown_source_unit", f"{k} 出现 unknown_source_unit 占位"
    # raw_amount_value 应为 None（historical transaction 无 amount 字段），非 0
    assert obs["raw_amount_value"] is None
    # amount evidence：RAW_FIELD_ABSENT
    assert obs["amount_evidence"]["source_type"] == "RAW_FIELD_ABSENT"


# ---------------------------------------------------------------------------
# TEST L — source contract reuse / no second market logic (MOD16)
# ---------------------------------------------------------------------------
def test_source_contract_reuse(mod):
    src = Path(mod.__file__).read_text(encoding="utf-8")
    # 复用官方 owner
    assert "from app.services.feature_snapshot_service import get_active_a_share_instruments" in src
    assert "from app.services.calendar_service import is_trading_day_async" in src
    assert "from app.services.market_data_aggregation_service import MarketDataAggregationService" in src
    assert "from app.services.adjustment_factor_service import AdjustmentFactorService" in src
    assert "from app.core.pytdx_adapter import PytdxAdapter, market_from_code" in src
    # 禁止第二套 market logic / xdxr 重算
    assert "xdxr_calc" not in src
    assert "recalc_xdxr" not in src
    assert "compute_xdxr" not in src


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------
def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, execute_fn):
        self._execute_fn = execute_fn

    async def execute(self, stmt):
        rows = await self._execute_fn(stmt)
        return _FakeResult(rows)


class _FakeSessionAsync:
    async def execute(self, stmt):
        return _FakeResult([])


class _FakeAdjService:
    def __init__(self, factor_fn):
        self._factor_fn = factor_fn

    async def get_factor_series(self, session, instrument_id, as_of=None):
        return await self._factor_fn(session, instrument_id, as_of)
