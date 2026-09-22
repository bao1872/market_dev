"""PANJI-INTRADAY-DIRECT-SOURCE 纯单元合同测试。

冻结的语义边界（source ownership）：

    实时分钟行情 = Provider（provider_direct）
    历史分钟行情 = DB（db_only）
    Node Cluster = 1d × 250 + 15m × 4000，但 15m 的来源由**显式** source_mode 决定
    旧的盘后持久化 chip 快照 = retired（不再被生产读链消费）

覆盖：

- B/C 15m / 1h 图表取数只走 provider，绝不读 DB 分钟线
- D   provider 失败必须明确失败，**禁止静默 fallback 到 stale DB**
- E   Live Node → 15m 走 provider_direct
- F   Historical / PIT Node → 15m 走 db_only 且不触网
- H   cache key 必须隔离 source policy（否则 provider_direct 会命中 hybrid 缓存）
- 复权 owner 唯一：provider_direct 与 hybrid 共用同一份 qfq 实现（不允许二次复权）

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_intraday_direct_source_policy.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.constants.indicator_contract import NODE_CLUSTER_LOW_BARS
from app.services import market_data_aggregation_service as mdas
from app.services.indicator_service import (
    IndicatorMarketDataMode,
    _indicator_node_source_mode,
    _indicator_source_policy,
)
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    MarketDataAggregationService,
    MarketDataSourcePolicy,
    is_historical_market_data_request,
    resolve_market_data_source_policy,
    resolve_request_source_policy,
)
from app.services.node_cluster_input_provider import (
    NodeClusterInputProvider,
    NodeClusterSourceMode,
    _resolve_node_source_policies,
)

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


async def _async_return(value: Any) -> Any:
    return value


def _last_closed_15m() -> pd.Timestamp:
    """当前时间向下取整到 15m 边界（naive，与 bars index 约定一致）。"""
    now = pd.Timestamp.now().tz_localize(None)
    return now.floor("15min") - pd.Timedelta(minutes=15)


def _mock_session() -> AsyncMock:
    return AsyncMock()


def _build_15m_bars(count: int, end: str | None = None) -> pd.DataFrame:
    """构造 mock provider 原生 15m bars（adj_factor=1.0，未复权）。

    默认**截止到当前最近一根已闭合 15m bar**：MDAS 会剔除未完成/未来 bar，
    若 bars 跨到未来会被裁掉，导致条数断言失真。
    """
    end_ts = pd.Timestamp(end) if end is not None else _last_closed_15m()
    times = pd.date_range(end=end_ts, periods=count, freq="15min")
    closes = [10.0 + i * 0.01 for i in range(count)]
    df = pd.DataFrame({
        "open": [c - 0.01 for c in closes],
        "high": [c + 0.01 for c in closes],
        "low": [c - 0.02 for c in closes],
        "close": closes,
        "volume": [1000.0 + i for i in range(count)],
        "amount": [10000.0 + i * 10 for i in range(count)],
        "adj_factor": [1.0] * count,
    }, index=times)
    df.index.name = "trade_time"
    return df


def _build_60m_bars(count: int, end: str | None = None) -> pd.DataFrame:
    end_ts = pd.Timestamp(end) if end is not None else _last_closed_15m()
    times = pd.date_range(end=end_ts, periods=count, freq="60min")
    closes = [20.0 + i * 0.02 for i in range(count)]
    df = pd.DataFrame({
        "open": [c - 0.02 for c in closes],
        "high": [c + 0.03 for c in closes],
        "low": [c - 0.03 for c in closes],
        "close": closes,
        "volume": [2000.0 + i for i in range(count)],
        "amount": [20000.0 + i * 10 for i in range(count)],
        "adj_factor": [1.0] * count,
    }, index=times)
    df.index.name = "trade_time"
    return df


def _build_daily_bars(count: int, end: str = "2026-06-30") -> pd.DataFrame:
    """构造 DB 日线（index 为 naive DatetimeIndex，与真实 repository 返回一致）。"""
    times = pd.date_range(end=pd.Timestamp(end), periods=count, freq="B")
    closes = [30.0 + i * 0.05 for i in range(count)]
    df = pd.DataFrame({
        "open": [c - 0.1 for c in closes],
        "high": [c + 0.15 for c in closes],
        "low": [c - 0.15 for c in closes],
        "close": closes,
        "volume": [50000.0 + i for i in range(count)],
        "amount": [500000.0 + i * 10 for i in range(count)],
        "adj_factor": [1.0] * count,
    }, index=times)
    df.index.name = "trade_date"
    return df


class _FakeAdjService:
    """最小复权服务替身：不触网、不做二次变换（source 分流用例只关心取数来源）。"""

    async def get_factor_series(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame({
            "trade_date": pd.to_datetime(["2026-01-01"]),
            "adj_factor": [1.0],
        })

    def apply_qfq(
        self, bars: pd.DataFrame, factors: pd.DataFrame, **kwargs: Any,
    ) -> pd.DataFrame:
        return bars


class _FakeResult:
    """最小 SQLAlchemy Result 替身。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> Any:
        class _Scalars:
            def all(self) -> list[Any]:
                return []

        return _Scalars()

    def scalar_one_or_none(self) -> None:
        return None

    def scalar(self) -> None:
        return None

    def mappings(self) -> Any:
        class _Mappings:
            def all(self) -> list[Any]:
                return []

        return _Mappings()


class _InstrumentOnlySession:
    """最小 DB 替身：只回答 ``Instrument.symbol``，策略注册表查询一律返回空。

    本模块的请求级用例要让**真实 compute_all_indicators** 跑起来（而不是 mock 掉），
    因此需要一个能把「symbol 查询」与「策略版本查询」区分开的最小 session：
    前者必须命中（否则 compute_all_indicators 第一步就 raise），后者返回空 ⇒
    不加载任何策略、不引入额外数据读取。
    """

    def __init__(self, symbol: str = "600519") -> None:
        self.symbol = symbol
        self.statements: list[str] = []

    async def execute(self, stmt: Any) -> _FakeResult:
        sql = " ".join(str(stmt).split())
        self.statements.append(sql)
        if "instruments.symbol" in sql:
            return _FakeResult([(self.symbol,)])
        return _FakeResult([])


# 外部行情 provider 的全部入口（历史 / PIT 路径必须零调用）。
_EXTERNAL_MARKET_DATA_FNS: tuple[str, ...] = (
    "fetch_daily_bars",
    "fetch_today_daily_bars",
    "fetch_15min_bars",
    "fetch_60min_bars",
    "fetch_minute_bars",
)


def _record_external_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把 5 个外部行情入口全部换成**调用记录器**，返回记录列表。

    刻意不用「一调用即抛异常」：MDAS 的 daily 回补与实时尾部两段都在
    ``try/except Exception`` 内（降级语义），异常会被吞掉，反而让用例误判为通过。
    只有调用记录才是非异常、不可被吞的判据。
    """

    calls: list[str] = []

    def _make(name: str) -> Any:
        async def _fake(*args: Any, **kwargs: Any) -> pd.DataFrame:
            calls.append(name)
            return pd.DataFrame()

        return _fake

    for fn_name in _EXTERNAL_MARKET_DATA_FNS:
        monkeypatch.setattr(mdas, fn_name, _make(fn_name))
    return calls


def _install_historical_db_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    """安装「历史 DB 里有数据、但**日线缺尾**」的 repository 替身。

    刻意让 DB 日线只覆盖到 2026-06-10，而预期最后完成日（stub）是 2026-06-30 ——
    于是 ``need_tail=True``：任何把日线留在 ``HYBRID``（allow_backfill=True）的实现
    都会去 ``fetch_daily_bars`` 触网。这正是本轮要关闭的失败模式，
    因此这个缺口让「零外部调用」断言真正具备判别力（否则 DB 覆盖完整时断言会空转）。
    """

    async def _db_daily(*args: Any, **kwargs: Any) -> pd.DataFrame:
        # 人为缺尾：DB 只到 06-10，预期完成日 06-30 ⇒ need_tail=True
        return _build_daily_bars(400, end="2026-06-10")

    async def _db_15m(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return _build_15m_bars(300, end="2026-06-30 14:45")

    async def _db_60m(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return _build_60m_bars(300, end="2026-06-30 14:00")

    monkeypatch.setattr(mdas, "_query_daily_bars", _db_daily)
    monkeypatch.setattr(mdas, "_query_15min_bars", _db_15m)
    monkeypatch.setattr(mdas, "_query_60min_bars", _db_60m)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))
    monkeypatch.setattr(
        mdas, "_call_expected_last_completed_daily_bar",
        lambda *a, **kw: _async_return(date(2026, 6, 30)),
    )
    monkeypatch.setattr(mdas, "AdjustmentFactorService", _FakeAdjService)
    monkeypatch.setattr(
        NodeClusterInputProvider, "_compute_exhaustion_proofs",
        AsyncMock(return_value={"1d": (False, None), "15m": (False, None)}),
    )


@pytest.fixture(autouse=True)
def _ban_db_intraday_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何 DB 分钟线读取在 provider_direct 用例中都视为失败。

    默认把所有 DB 分钟线 reader 与 listing_date 查询替换为「一调用就炸」，
    仅允许显式需要它们的用例（DB_ONLY）自行覆盖回正常实现。
    """

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "provider_direct 路径不得读取 DB 分钟线 / listing_date"
        )

    monkeypatch.setattr(mdas, "_query_15min_bars", _boom)
    monkeypatch.setattr(mdas, "_query_60min_bars", _boom)
    monkeypatch.setattr(mdas, "_query_minute_bars", _boom)
    monkeypatch.setattr(mdas, "_get_listing_date", _boom)
    monkeypatch.setattr(mdas, "list_bars_by_timeframe", _boom, raising=False)


@pytest.fixture(autouse=True)
def _no_realtime_1d_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定非交易日 + 非交易时段，避免 1d partial daily 合成分支干扰。"""
    monkeypatch.setattr(
        mdas, "is_trading_day_async", lambda *a, **kw: _async_return(False),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)


# ============================================================
# 枚举与唯一判定点
# ============================================================


def test_source_policy_enum_values_are_stable() -> None:
    assert MarketDataSourcePolicy.HYBRID.value == "hybrid"
    assert MarketDataSourcePolicy.PROVIDER_DIRECT.value == "provider_direct"
    assert MarketDataSourcePolicy.DB_ONLY.value == "db_only"


def test_source_policy_resolver_is_single_owner() -> None:
    """``resolve_market_data_source_policy`` 是唯一判定点，且 **historical 优先于 timeframe**。"""
    assert resolve_market_data_source_policy(
        "15m", historical=False,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT
    assert resolve_market_data_source_policy(
        "1h", historical=False,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT
    # live 的非原生日内周期 → HYBRID（行为与既有完全一致）
    for tf in ("1d", "1w", "1mo", "1m"):
        assert resolve_market_data_source_policy(
            tf, historical=False,
        ) is MarketDataSourcePolicy.HYBRID, tf

    # historical = zero-network：**任意周期**（含 1d / 1w / 1mo / 1m）都必须 DB_ONLY。
    # 若先按 timeframe 分流再判 historical，historical 的 1d/1w/1mo 会落回 HYBRID，
    # 而 HYBRID 的 need_tail 回补与 partial daily 合并仍会触网 —— 名义上「已 DB_ONLY」
    # 实际却把当前行情拉进历史结果。
    for tf in ("1d", "15m", "1h", "1w", "1mo", "1m"):
        assert resolve_market_data_source_policy(
            tf, historical=True,
        ) is MarketDataSourcePolicy.DB_ONLY, tf


def test_request_source_policy_maps_historical_requests_to_db_only() -> None:
    """request-level resolver：as-of / 回溯窗口 → db_only，live → provider_direct。"""
    today = date(2026, 9, 22)

    # live：无 as-of、无 end_date（或 end_date == today）
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=None, end_date=None, today=today,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=None, end_date=today, today=today,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT
    # 未来 end_date 也不算历史
    assert resolve_request_source_policy(
        "1h", adjustment_as_of=None, end_date=date(2026, 9, 25), today=today,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT

    # historical：显式 as-of
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=date(2026, 7, 1), today=today,
    ) is MarketDataSourcePolicy.DB_ONLY
    # historical：回溯窗口（end_date 早于今天）
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=None, end_date=date(2026, 6, 1), today=today,
    ) is MarketDataSourcePolicy.DB_ONLY
    # 非分钟周期在**历史**请求下同样 DB_ONLY（PIT zero-network 无 timeframe 例外）
    assert resolve_request_source_policy(
        "1d", adjustment_as_of=date(2026, 7, 1), today=today,
    ) is MarketDataSourcePolicy.DB_ONLY
    # …但非分钟周期的 live 请求仍是 HYBRID（不得把 PIT 收紧误扩到 live）
    assert resolve_request_source_policy(
        "1d", adjustment_as_of=None, end_date=None, today=today,
    ) is MarketDataSourcePolicy.HYBRID


def test_completed_only_is_not_treated_as_historical() -> None:
    """``completed_only=True`` **不得**被当成 historical。

    实时 Node 输入同样传 completed_only=True，但它仍是 live，必须走 provider_direct
    的已完成 bars。resolver 的入参中根本没有 completed_only —— 本用例锁死这一点。
    """
    import inspect

    for fn in (resolve_market_data_source_policy, resolve_request_source_policy):
        params = set(inspect.signature(fn).parameters)
        assert "completed_only" not in params, (
            f"{fn.__name__} 不得接收 completed_only（它不是历史判据）"
        )
    # live 15m + completed_only=True 仍走 provider_direct（由 chart-snapshot 侧保证）
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=None, end_date=None, today=date(2026, 9, 22),
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT


def test_node_source_mode_maps_both_policies_explicitly() -> None:
    """Node source_mode → (daily, 15m) 成对策略，**显式**参数、不推断。"""
    assert _resolve_node_source_policies(NodeClusterSourceMode.LIVE_DIRECT) == (
        MarketDataSourcePolicy.HYBRID,
        MarketDataSourcePolicy.PROVIDER_DIRECT,
    )
    # HISTORICAL_DB 必须是 daily + 15m 都 DB_ONLY：PIT 绝对禁止访问网络
    assert _resolve_node_source_policies(NodeClusterSourceMode.HISTORICAL_DB) == (
        MarketDataSourcePolicy.DB_ONLY,
        MarketDataSourcePolicy.DB_ONLY,
    )
    # source_mode 必须是显式参数且默认 LIVE_DIRECT（禁止用 adjustment_as_of 猜模式）
    import inspect

    sig = inspect.signature(NodeClusterInputProvider.get_inputs)
    assert sig.parameters["source_mode"].default is NodeClusterSourceMode.LIVE_DIRECT


# ============================================================
# B / C — 15m / 1h 只走 provider
# ============================================================


async def test_b_15m_provider_direct_never_queries_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15m + provider_direct：provider 调用 1 次，DB 分钟线 0 次。"""
    service = MarketDataAggregationService()
    provider_bars = _build_15m_bars(4000)
    provider_calls: list[dict[str, Any]] = []

    async def _fake_fetch_15m(session: Any, instrument_id: Any, *, count: int) -> pd.DataFrame:
        provider_calls.append({"count": count})
        return provider_bars.copy()

    monkeypatch.setattr(mdas, "fetch_15min_bars", _fake_fetch_15m)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", limit=4000,
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
    )

    assert isinstance(result, BarAggregationResult)
    assert len(provider_calls) == 1, "15m 应恰好调用 provider 1 次"
    assert provider_calls[0]["count"] == 4000
    assert result.data_source == "provider_direct"
    assert len(result.bars) == 4000
    assert result.is_partial is False
    assert result.last_persisted_bar_time is None, (
        "provider_direct 不读 DB，不应有 persisted bar 时间"
    )


async def test_c_1h_provider_direct_never_queries_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1h + provider_direct：走原生 60m provider，DB 分钟线 0 次。"""
    service = MarketDataAggregationService()
    provider_bars = _build_60m_bars(1200)
    provider_calls: list[dict[str, Any]] = []

    async def _fake_fetch_60m(session: Any, instrument_id: Any, *, count: int) -> pd.DataFrame:
        provider_calls.append({"count": count})
        return provider_bars.copy()

    monkeypatch.setattr(mdas, "fetch_60min_bars", _fake_fetch_60m)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1h", adj="none", limit=1200,
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
    )

    assert len(provider_calls) == 1
    assert provider_calls[0]["count"] == 1200
    assert result.data_source == "provider_direct"
    assert len(result.bars) == 1200


async def test_provider_direct_uses_contract_defaults_when_limit_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式给 limit 时，15m 默认 4000（Node 完整质量门槛）。"""
    service = MarketDataAggregationService()
    seen: list[int] = []

    async def _fake_fetch_15m(session: Any, instrument_id: Any, *, count: int) -> pd.DataFrame:
        seen.append(count)
        return _build_15m_bars(10)

    monkeypatch.setattr(mdas, "fetch_15min_bars", _fake_fetch_15m)

    await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none",
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
    )

    assert seen == [NODE_CLUSTER_LOW_BARS]
    assert NODE_CLUSTER_LOW_BARS == 4000


async def test_provider_direct_rejects_non_native_timeframe() -> None:
    """provider_direct 只支持 provider 原生日内周期（1d 必须早失败）。"""
    service = MarketDataAggregationService()
    with pytest.raises(ValueError, match="provider_direct 只支持"):
        await service.get_bars(
            _mock_session(), TEST_INSTRUMENT_ID,
            timeframe="1d", adj="none",
            source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
        )


# ============================================================
# D — provider 失败必须明确失败（禁止静默 fallback 到 stale DB）
# ============================================================


async def test_d_provider_failure_does_not_fall_back_to_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 抛错时必须向上传播；DB 里即使有 4000 根旧 15m 也不得被用来兜底。"""
    service = MarketDataAggregationService()
    stale_db_bars = _build_15m_bars(4000)
    db_query_calls: list[int] = []

    async def _db_fallback(*args: Any, **kwargs: Any) -> pd.DataFrame:
        db_query_calls.append(1)
        return stale_db_bars.copy()

    async def _failing_provider(*args: Any, **kwargs: Any) -> pd.DataFrame:
        from app.core.pytdx_adapter import PytdxSourceError

        raise PytdxSourceError(operation="get_15min_bars", message="provider unreachable")

    # 允许 DB reader 被调用（用于证明"它没有被调用"）
    monkeypatch.setattr(mdas, "_query_15min_bars", _db_fallback)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))
    monkeypatch.setattr(mdas, "fetch_15min_bars", _failing_provider)

    from app.core.pytdx_adapter import PytdxSourceError

    with pytest.raises(PytdxSourceError):
        await service.get_bars(
            _mock_session(), TEST_INSTRUMENT_ID,
            timeframe="15m", adj="none", limit=4000,
            source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
        )

    assert db_query_calls == [], (
        "provider 失败后绝不允许读 DB 旧分钟线兜底（会掩盖故障并返回越来越旧的数据）"
    )


async def test_provider_empty_returns_empty_not_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 返回空（新股）→ 空结果 + 非降级兜底，不得拿 DB 补。"""
    service = MarketDataAggregationService()

    async def _empty_provider(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    monkeypatch.setattr(mdas, "fetch_15min_bars", _empty_provider)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", limit=4000,
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
    )
    assert result.data_source == "provider_direct"
    assert len(result.bars) == 0


async def test_provider_insufficient_history_is_marked_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 只给 800 根（新股）→ actual < requested 且 history_exhausted。"""
    service = MarketDataAggregationService()

    async def _short_provider(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return _build_15m_bars(800)

    monkeypatch.setattr(mdas, "fetch_15min_bars", _short_provider)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", limit=4000,
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
    )
    assert result.data_source == "provider_direct"
    assert len(result.bars) == 800
    assert result.history_exhausted is True


# ============================================================
# 复权 owner 唯一（改变 source 不改变 adjustment owner）
# ============================================================


async def test_provider_direct_applies_qfq_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider_direct 的未复权原生 bar 必须被 MDAS 统一 qfq pipeline 复权**恰好一次**。"""
    service = MarketDataAggregationService()
    factor_df = pd.DataFrame(
        {"trade_date": pd.to_datetime(["2026-09-01"]), "adj_factor": [0.5]},
    )
    apply_calls: list[int] = []

    class _FakeAdjService:
        async def get_factor_series(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
            return factor_df.copy()

        def apply_qfq(
            self, bars: pd.DataFrame, factors: pd.DataFrame, *,
            as_of: date | None = None, intraday: bool = False,
        ) -> pd.DataFrame:
            apply_calls.append(1)
            out = bars.copy()
            for col in ("open", "high", "low", "close"):
                out[col] = out[col] * 0.5
            return out

    monkeypatch.setattr(mdas, "AdjustmentFactorService", _FakeAdjService)

    async def _fake_fetch_15m(session: Any, instrument_id: Any, *, count: int) -> pd.DataFrame:
        return _build_15m_bars(10)

    monkeypatch.setattr(mdas, "fetch_15min_bars", _fake_fetch_15m)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="qfq", limit=10,
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
    )

    assert apply_calls == [1], (
        f"qfq 必须恰好施加一次（禁止 provider_direct 自复权 + MDAS 二次复权），"
        f"实际 {len(apply_calls)} 次"
    )
    # 10.0 → 5.0（若二次复权会变成 2.5）
    assert result.bars["close"].iloc[0] == pytest.approx(5.0)
    assert result.data_source == "provider_direct"


def test_qfq_has_single_owner_helper() -> None:
    """两条分支必须共用 _apply_intraday_qfq，而不是各自内联 qfq。"""
    import inspect

    src = inspect.getsource(MarketDataAggregationService.get_bars)
    assert src.count("_apply_intraday_qfq(") == 2, (
        "provider_direct 与 hybrid 两条分支应各调用一次同一 helper"
    )
    # 日内 qfq 不得内联（get_bars 内剩余的 apply_qfq 调用全部是 intraday=False 的日线路径）
    assert "intraday=True" not in src, (
        "get_bars 内不得内联日内 qfq；日内复权必须经 _apply_intraday_qfq 单一 owner"
    )


# ============================================================
# DB_ONLY — 历史 / PIT 绝不触网
# ============================================================


async def test_db_only_never_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """db_only：只读 DB，provider 0 次。"""
    service = MarketDataAggregationService()
    db_bars = _build_15m_bars(300)
    provider_calls: list[int] = []

    async def _db_read(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return db_bars.copy()

    async def _provider_should_not_run(*args: Any, **kwargs: Any) -> pd.DataFrame:
        provider_calls.append(1)
        return _build_15m_bars(10)

    monkeypatch.setattr(mdas, "_query_15min_bars", _db_read)
    monkeypatch.setattr(mdas, "fetch_15min_bars", _provider_should_not_run)
    # DB_ONLY 路径允许 listing_date（历史下界）查询
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", limit=300,
        source_policy=MarketDataSourcePolicy.DB_ONLY,
    )

    assert provider_calls == [], "db_only 路径禁止访问外部 provider"
    assert result.data_source == "db"


async def test_db_only_bounds_repository_window_at_end_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """db_only + end_date：下推到 repository 的查询窗口上界不得晚于 end_date（PIT 防未来）。

    真实生产路径中「不返回未来 bar」由 repository 的 SQL 窗口保证；本用例锁定
    MDAS 确实把调用方的 end_date 下推（而不是自己扩大窗口）。
    """
    service = MarketDataAggregationService()
    windows: list[tuple[Any, Any]] = []

    async def _db_read(
        session: Any, instrument_id: Any, start: Any, end: Any,
    ) -> pd.DataFrame:
        windows.append((start, end))
        return _build_15m_bars(200, end=end)

    monkeypatch.setattr(mdas, "_query_15min_bars", _db_read)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", limit=200,
        end_date=date(2026, 1, 6),
        source_policy=MarketDataSourcePolicy.DB_ONLY,
    )

    assert windows, "db_only 必须查询 DB repository"
    _, query_end = windows[0]
    assert pd.Timestamp(query_end).date() <= date(2026, 1, 6), (
        f"repository 查询上界不得晚于请求 end_date，实际 {query_end!r}"
    )
    assert not result.bars.empty
    assert result.bars.index.max().date() <= date(2026, 1, 6)


async def test_provider_direct_honours_end_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider_direct + end：provider 返回的未来 bar 必须被裁掉（MDAS 自己保证）。"""
    service = MarketDataAggregationService()

    # 2000 根 ≈ 20 个交易日，跨越 2026-09-10，确保裁剪后仍有数据
    async def _provider_returns_future(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return _build_15m_bars(2000, end="2026-09-20 14:45")

    monkeypatch.setattr(mdas, "fetch_15min_bars", _provider_returns_future)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", limit=2000,
        end_date=date(2026, 9, 10),
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
    )

    assert not result.bars.empty
    assert result.bars.index.max().date() <= date(2026, 9, 10), (
        "provider_direct 不得返回 end 之后的 bar（PIT 防未来）"
    )


# ============================================================
# H — cache key 必须隔离 source policy
# ============================================================


def test_h_cache_key_isolates_source_policy() -> None:
    base_kwargs: dict[str, Any] = {
        "include_realtime": True, "completed_only": False, "start_date": None,
        "end_date": None, "limit": 4000, "warmup_bars": 0, "adjustment_as_of": None,
    }
    hybrid = mdas._cache_key(
        TEST_INSTRUMENT_ID, "15m", "none",
        source_policy=MarketDataSourcePolicy.HYBRID, **base_kwargs,
    )
    direct = mdas._cache_key(
        TEST_INSTRUMENT_ID, "15m", "none",
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT, **base_kwargs,
    )
    db_only = mdas._cache_key(
        TEST_INSTRUMENT_ID, "15m", "none",
        source_policy=MarketDataSourcePolicy.DB_ONLY, **base_kwargs,
    )

    assert len({hybrid, direct, db_only}) == 3, (
        "不同 source_policy 必须落到不同缓存条目，否则 provider_direct 会命中 hybrid（=DB）缓存"
    )
    assert hybrid != direct and direct != db_only and hybrid != db_only

    # 同一 policy 必须稳定（不引入随机性）
    assert direct == mdas._cache_key(
        TEST_INSTRUMENT_ID, "15m", "none",
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT, **base_kwargs,
    )
    assert "provider_direct" in direct


def test_contract_version_bumped_for_source_policy() -> None:
    """契约版本已 bump（旧缓存自动失效，无需全局 flush）。"""
    assert mdas._MARKET_DATA_CONTRACT_VERSION == "v6"


# ============================================================
# E / F — Node Cluster 的 15m 来源由显式 source_mode 决定
# ============================================================


def _node_input_result(count: int, timeframe: str) -> BarAggregationResult:
    bars = _build_15m_bars(count) if timeframe == "15m" else _build_60m_bars(count)
    return BarAggregationResult(
        bars=bars,
        data_source="provider_direct" if timeframe == "15m" else "hybrid",
        as_of=datetime(2026, 9, 1, 15, 0),
        is_partial=False,
        last_persisted_bar_time=None,
        last_live_bar_time=None,
        freshness_seconds=1.0,
        degraded=False,
        degraded_reason=None,
        history_exhausted=False,
        source_bar_hash=f"hash-{timeframe}",
    )


async def test_e_live_node_uses_provider_direct_for_15m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE_DIRECT：Node 的 15m 输入必须声明 provider_direct，daily 仍 HYBRID。"""
    calls: list[dict[str, Any]] = []

    async def _fake_get_bars(
        self: Any, session: Any, instrument_id: Any, **kwargs: Any,
    ) -> BarAggregationResult:
        calls.append(kwargs)
        return _node_input_result(kwargs.get("limit") or 0, kwargs["timeframe"])

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _fake_get_bars)
    monkeypatch.setattr(
        NodeClusterInputProvider, "_compute_exhaustion_proofs",
        AsyncMock(return_value={"1d": (False, None), "15m": (False, None)}),
    )

    await NodeClusterInputProvider.get_inputs(
        _mock_session(), TEST_INSTRUMENT_ID,
        source_mode=NodeClusterSourceMode.LIVE_DIRECT,
    )

    by_tf = {c["timeframe"]: c for c in calls}
    assert set(by_tf) == {"1d", "15m"}
    assert by_tf["15m"]["source_policy"] is MarketDataSourcePolicy.PROVIDER_DIRECT
    assert by_tf["15m"]["limit"] == 4000
    assert by_tf["1d"]["source_policy"] is MarketDataSourcePolicy.HYBRID
    assert by_tf["1d"]["limit"] == 250


async def test_f_historical_node_uses_db_only_and_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HISTORICAL_DB：Node 的 daily **与** 15m 输入都必须声明 db_only（PIT 禁止触网）。"""
    calls: list[dict[str, Any]] = []

    async def _fake_get_bars(
        self: Any, session: Any, instrument_id: Any, **kwargs: Any,
    ) -> BarAggregationResult:
        calls.append(kwargs)
        return _node_input_result(kwargs.get("limit") or 0, kwargs["timeframe"])

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _fake_get_bars)
    monkeypatch.setattr(
        NodeClusterInputProvider, "_compute_exhaustion_proofs",
        AsyncMock(return_value={"1d": (False, None), "15m": (False, None)}),
    )

    await NodeClusterInputProvider.get_inputs(
        _mock_session(), TEST_INSTRUMENT_ID,
        adjustment_as_of=date(2026, 7, 1),
        end_date=date(2026, 7, 1),
        source_mode=NodeClusterSourceMode.HISTORICAL_DB,
    )

    by_tf = {c["timeframe"]: c for c in calls}
    assert by_tf["15m"]["source_policy"] is MarketDataSourcePolicy.DB_ONLY
    assert by_tf["15m"]["limit"] == 4000
    # [P1-3] daily 也必须是 DB_ONLY：HYBRID 仍可在 need_tail 时访问 provider，
    # 历史 / PIT 路径必须零网络访问。
    assert by_tf["1d"]["source_policy"] is MarketDataSourcePolicy.DB_ONLY
    assert by_tf["1d"]["limit"] == 250


async def test_node_source_mode_default_is_live_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传 source_mode 默认 LIVE_DIRECT；且**禁止**用 adjustment_as_of 推断模式。"""
    calls: list[dict[str, Any]] = []

    async def _fake_get_bars(
        self: Any, session: Any, instrument_id: Any, **kwargs: Any,
    ) -> BarAggregationResult:
        calls.append(kwargs)
        return _node_input_result(kwargs.get("limit") or 0, kwargs["timeframe"])

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _fake_get_bars)
    monkeypatch.setattr(
        NodeClusterInputProvider, "_compute_exhaustion_proofs",
        AsyncMock(return_value={"1d": (False, None), "15m": (False, None)}),
    )

    # 显式传 adjustment_as_of=today（Capture / Monitor / 当前交易日计算的真实形态）
    await NodeClusterInputProvider.get_inputs(
        _mock_session(), TEST_INSTRUMENT_ID,
        adjustment_as_of=date(2026, 9, 1),
    )

    by_tf = {c["timeframe"]: c for c in calls}
    assert by_tf["15m"]["source_policy"] is MarketDataSourcePolicy.PROVIDER_DIRECT, (
        "显式传 adjustment_as_of 仍是 live，不得被当成 historical_db"
    )


# ============================================================
# 请求语义边界（真实调用点）：/bars 与 /chart-snapshot 共用同一判定点
# ============================================================
# 上面 B..H 锁的是 MDAS 内部枚举行为；本节锁的是**真实入口**是否把「请求语义」
# 正确地翻译成 source policy —— 历史 15m/1h 曾经因为 resolver 只看 timeframe
# 而被强制成 provider_direct（历史请求打到线上 provider）。


def _access_ctx() -> Any:
    """构造最小可用的 AccessContext（本组用例不校验权限，只校验取数分流）。"""
    from app.services.access_control_service import AccessContext

    return AccessContext(
        user_id=str(uuid.uuid4()),
        account_status="active",
        roles=["member"],
        is_admin=False,
        is_member=True,
        subscription_active=True,
        default_route="/market",
    )


async def test_bars_historical_15m_uses_db_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/bars` 历史 15m（end_date < today）→ DB_ONLY：provider = 0 / DB = 1。

    真实入口 + 真实 MDAS，只把两端的 IO 换掉：
    - ``fetch_15min_bars``（provider 抓取）记录调用 → 必须 0；
    - ``_query_15min_bars``（DB 分钟线）记录调用 → 必须恰好 1（无 limit ⇒ 单轮）。
    """
    from fastapi import Response

    from app.api import bars as bars_api

    provider_calls: list[Any] = []
    db_calls: list[Any] = []

    async def _provider_should_not_run(*args: Any, **kwargs: Any) -> pd.DataFrame:
        provider_calls.append(1)
        return _build_15m_bars(4000)

    async def _db_read(*args: Any, **kwargs: Any) -> pd.DataFrame:
        db_calls.append(1)
        return _build_15m_bars(300)

    monkeypatch.setattr(mdas, "fetch_15min_bars", _provider_should_not_run)
    monkeypatch.setattr(mdas, "_query_15min_bars", _db_read)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))

    response = Response()
    result = await bars_api.get_bars(
        TEST_INSTRUMENT_ID,
        timeframe="15m",
        adj="none",
        start_date=None,
        end_date=date(2026, 6, 1),  # < today ⇒ historical（PIT）
        page=1,
        page_size=100,
        include_realtime=True,
        completed_only=False,
        adjustment_as_of=None,
        ctx=_access_ctx(),
        session=_mock_session(),
        response=response,
    )

    assert provider_calls == [], (
        "历史 15m 请求（end_date < today）绝不允许访问 provider"
    )
    assert len(db_calls) == 1, (
        f"历史 15m 请求必须且只读 DB 一次，实际 {len(db_calls)} 次"
    )
    assert result.data_source == "db"
    assert response.headers["X-Data-Source"] == "db"


async def test_bars_current_15m_uses_provider_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/bars` 实时 15m（无 end_date / 无 as-of）→ PROVIDER_DIRECT：provider = 1 / DB = 0。

    DB 分钟线 reader 由本文件的 autouse fixture 全量替换为「一调用即失败」，
    因此本用例只要成功返回，即等价于「DB 分钟线 0 次」。
    """
    from fastapi import Response

    from app.api import bars as bars_api

    provider_calls: list[Any] = []

    async def _fake_fetch_15m(
        session: Any, instrument_id: Any, *, count: int,
    ) -> pd.DataFrame:
        provider_calls.append(count)
        return _build_15m_bars(count)

    monkeypatch.setattr(mdas, "fetch_15min_bars", _fake_fetch_15m)

    response = Response()
    result = await bars_api.get_bars(
        TEST_INSTRUMENT_ID,
        timeframe="15m",
        adj="none",
        start_date=None,
        end_date=None,
        page=1,
        page_size=100,
        include_realtime=True,
        completed_only=False,
        adjustment_as_of=None,
        ctx=_access_ctx(),
        session=_mock_session(),
        response=response,
    )

    assert len(provider_calls) == 1, "实时 15m 必须恰好走 provider 1 次"
    assert provider_calls[0] == NODE_CLUSTER_LOW_BARS
    assert result.data_source == "provider_direct"
    assert response.headers["X-Data-Source"] == "provider_direct"


async def test_chart_snapshot_historical_1h_uses_db_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/chart-snapshot` 历史 1h（显式 as-of）→ DB_ONLY：provider = 0。

    与 /bars 共用 MDAS `resolve_request_source_policy`（唯一判定点），
    本用例在 chart-snapshot 入口上再验证一次，防止两条入口再次分叉。
    """
    from app.services import chart_snapshot_service as css

    provider_calls: list[Any] = []
    db_calls: list[Any] = []

    async def _provider_should_not_run(*args: Any, **kwargs: Any) -> pd.DataFrame:
        provider_calls.append(1)
        return _build_60m_bars(1200)

    async def _db_read(*args: Any, **kwargs: Any) -> pd.DataFrame:
        db_calls.append(1)
        return _build_60m_bars(300)

    monkeypatch.setattr(mdas, "fetch_60min_bars", _provider_should_not_run)
    monkeypatch.setattr(mdas, "_query_60min_bars", _db_read)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))
    # 指标计算与本次 source 分流无关，隔离掉以免引入额外 DB/Node 读。
    indicators_mock = AsyncMock(
        return_value={"layers": [], "data": {}, "timeframe": "1h"},
    )
    monkeypatch.setattr(css, "compute_all_indicators", indicators_mock)

    result = await css.ChartSnapshotService.compute_bars_and_indicators(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1h", adj="none", bars=100,
        include_realtime=False, completed_only=True,
        adjustment_as_of=date(2026, 7, 1),
    )

    assert provider_calls == [], (
        "历史 1h 快照请求（显式 adjustment_as_of）绝不允许访问 provider"
    )
    assert len(db_calls) == 1, (
        f"历史 1h 快照请求必须且只读 DB 一次，实际 {len(db_calls)} 次"
    )
    # [P1-chart-snapshot-historical] 只有展示半边 DB_ONLY 是不够的：
    # 指标读取模式必须由**同一**请求语义派生并显式传下去。
    assert indicators_mock.await_args.kwargs["market_data_mode"] is (
        IndicatorMarketDataMode.HISTORICAL_DB
    ), "历史快照必须把 HISTORICAL_DB 传给 compute_all_indicators"
    assert result.bars_result.data_source == "db"
    assert result.is_empty is False


async def test_historical_node_has_zero_external_market_data_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HISTORICAL_DB Node 全链零外部行情调用（daily + 15m 都是 DB_ONLY）。

    与 ``test_f_*`` 的区别：本用例不替换 ``get_bars``，而是让**真实 MDAS** 跑，
    把 5 个外部 provider 入口全部替换为**记录器** —— 只要历史 Node 路径里任何一处
    （daily need_tail / 15m fresh tail / realtime 1m）真的触网，调用记录就会非空。

    注意：不能用「一调用即抛异常」来断言 —— MDAS 的 daily 回补 / 实时尾部两段
    都在 ``try/except Exception`` 内（降级语义），异常会被吞掉，反而让用例误判为通过。
    必须用调用记录做**非异常**判据。
    """
    external_calls = _record_external_calls(monkeypatch)

    async def _empty_daily(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "amount"])

    async def _db_15m(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return _build_15m_bars(300)

    monkeypatch.setattr(mdas, "_query_daily_bars", _empty_daily)
    monkeypatch.setattr(mdas, "_query_15min_bars", _db_15m)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))
    monkeypatch.setattr(
        mdas, "_call_expected_last_completed_daily_bar",
        lambda *a, **kw: _async_return(date(2026, 7, 1)),
    )
    monkeypatch.setattr(mdas, "AdjustmentFactorService", _FakeAdjService)
    monkeypatch.setattr(
        NodeClusterInputProvider, "_compute_exhaustion_proofs",
        AsyncMock(return_value={"1d": (False, None), "15m": (False, None)}),
    )

    await NodeClusterInputProvider.get_inputs(
        _mock_session(), TEST_INSTRUMENT_ID,
        adjustment_as_of=date(2026, 7, 1),
        end_date=date(2026, 7, 1),
        source_mode=NodeClusterSourceMode.HISTORICAL_DB,
    )

    assert external_calls == [], (
        f"HISTORICAL_DB 路径绝对禁止访问外部行情源，实际触网: {external_calls}"
    )


# ============================================================
# 审计要求的端到端 PIT 用例：整条历史请求链零网络
# ============================================================
# 上一版只证明了「chart-snapshot 的 bars 半边不联网」——因为用例把
# ``compute_all_indicators`` 整个 mock 掉了，真正会漏的 Node / indicator 内部取数
# 根本没被执行。本节三个用例刻意**不 mock** 出问题的那一段。


async def test_historical_chart_snapshot_has_zero_external_market_data_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史 chart-snapshot **整条链** PIT 零网络（含 indicators 内部的日线/分钟/Node 输入）。

    请求语义：``timeframe="1h" + adjustment_as_of=2026-07-01``。
    真实调用 ``ChartSnapshotService.compute_bars_and_indicators``，
    **不 mock ``compute_all_indicators``**：让指标内部的
    日线 HYBRID→DB_ONLY、Node LIVE_DIRECT→HISTORICAL_DB、
    15m/1h 展示与 SMC 预热全部真实跑一遍。

    判据用「5 个外部行情入口的调用记录」而不是「一调用即抛异常」——
    MDAS 的 daily 回补 / 实时尾部都在 ``try/except`` 内，异常会被吞掉。
    """
    from app.services import chart_snapshot_service as css

    external_calls = _record_external_calls(monkeypatch)
    _install_historical_db_readers(monkeypatch)

    result = await css.ChartSnapshotService.compute_bars_and_indicators(
        _InstrumentOnlySession(), TEST_INSTRUMENT_ID,
        timeframe="1h", adj="qfq", bars=100,
        include_realtime=False, completed_only=True,
        adjustment_as_of=date(2026, 7, 1),
    )

    assert result.is_empty is False, (
        "用例前提是历史 DB 有数据；否则下面的断言是空转"
    )
    assert external_calls == [], (
        "历史 chart-snapshot 必须整条链 PIT 零外部行情调用"
        f"（display bars + 指标日线/分钟/SMC + Node 输入），实际触网: {external_calls}"
    )


async def test_historical_indicators_use_historical_db_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/indicators?adjustment_as_of=…` → 历史模式：daily DB_ONLY / Node HISTORICAL_DB / 15m 零触网。

    直接驱动 ``compute_all_indicators``（/indicators 的真实计算入口），
    在 **不传 preloaded_display_bars** 的情况下验证「内部自取数据」这一半：
    - 日线 ``source_policy`` 必须是 ``DB_ONLY``（不能停在 HYBRID）；
    - Node 输入必须是 ``HISTORICAL_DB``（否则 Node 15m 会走 provider）；
    - 外部 provider 调用 0 次。
    """
    from app.services import indicator_service as ind

    external_calls = _record_external_calls(monkeypatch)
    _install_historical_db_readers(monkeypatch)

    seen: list[dict[str, Any]] = []
    node_modes: list[Any] = []
    real_get_inputs = NodeClusterInputProvider.get_inputs
    real_get_bars = MarketDataAggregationService.get_bars

    async def _spy_get_inputs(
        cls: Any, session: Any, instrument_id: Any, **kwargs: Any,
    ) -> Any:
        node_modes.append(kwargs.get("source_mode"))
        return await real_get_inputs(session, instrument_id, **kwargs)

    async def _spy_get_bars(
        self: Any, session: Any, instrument_id: Any, **kwargs: Any,
    ) -> Any:
        seen.append(kwargs)
        return await real_get_bars(self, session, instrument_id, **kwargs)

    monkeypatch.setattr(
        NodeClusterInputProvider, "get_inputs", classmethod(_spy_get_inputs),
    )
    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _spy_get_bars)

    await ind.compute_all_indicators(
        _InstrumentOnlySession(), TEST_INSTRUMENT_ID,
        timeframe="1h", adj="qfq", bars=100,
        include_realtime=False, completed_only=True,
        adjustment_as_of=date(2026, 7, 1),
        market_data_mode=IndicatorMarketDataMode.HISTORICAL_DB,
    )

    daily_policies = [
        c["source_policy"] for c in seen if c["timeframe"] == "1d"
    ]
    assert daily_policies, "指标内部必须自取日线（未传 preloaded 时）"
    assert all(p is MarketDataSourcePolicy.DB_ONLY for p in daily_policies), (
        f"历史模式下日线不得停在 HYBRID（DB 缺尾会触网），实际: {daily_policies}"
    )
    # 指标内部直接取用的分钟周期（非 preloaded 分支）也必须 DB_ONLY
    intraday = [c for c in seen if c["timeframe"] in ("15m", "1h")]
    assert intraday, "本用例要求指标内部至少取一次分钟周期"
    assert all(
        c["source_policy"] is MarketDataSourcePolicy.DB_ONLY for c in intraday
    ), [(c["timeframe"], c["source_policy"]) for c in intraday]

    assert node_modes and all(
        m is NodeClusterSourceMode.HISTORICAL_DB for m in node_modes
    ), f"历史模式下 Node 输入必须是 HISTORICAL_DB，实际: {node_modes}"
    assert external_calls == [], (
        f"历史 indicators 请求禁止触网，实际: {external_calls}"
    )


async def test_indicators_api_declares_request_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/indicators` 独立 API 必须把请求语义翻译成 market_data_mode 传下去。

    只接 chart-snapshot 是不够的：``/indicators?adjustment_as_of=…`` 曾经直接
    ``compute_all_indicators(...)``，于是留下第二条 PIT 泄漏入口。
    本用例在 **API 边界**上锁住这个参数（compute 本身由上一个用例真实验证）。
    """
    from fastapi import Response

    from app.api import indicators as indicators_api

    calls: list[dict[str, Any]] = []

    async def _fake_compute(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"layers": [], "data": {}, "timeframe": kwargs["timeframe"]}

    monkeypatch.setattr(indicators_api, "compute_all_indicators", _fake_compute)
    monkeypatch.setattr(
        indicators_api, "_get_last_bar_time",
        AsyncMock(return_value=datetime(2026, 7, 1, 15, 0)),
    )
    monkeypatch.setattr(
        indicators_api.indicator_cache, "get", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        indicators_api.indicator_cache, "set", AsyncMock(return_value=None),
    )

    async def _call(*, adjustment_as_of: date | None) -> None:
        await indicators_api.get_indicators(
            TEST_INSTRUMENT_ID,
            timeframe="1h", adj="none", bars=100,
            force_refresh=True, capture=False, include_smc=False,
            include_realtime=adjustment_as_of is None,
            completed_only=adjustment_as_of is not None,
            adjustment_as_of=adjustment_as_of,
            ctx=_access_ctx(), db=_mock_session(), response=Response(),
        )

    # 历史请求（显式 as-of）
    await _call(adjustment_as_of=date(2026, 7, 1))
    assert calls[-1]["market_data_mode"] is (
        IndicatorMarketDataMode.HISTORICAL_DB
    ), "历史 /indicators 请求必须以 HISTORICAL_DB 驱动内部取数"

    # 实时请求（无 as-of）必须保持 LIVE，不能把当前页面一起改成 DB_ONLY
    await _call(adjustment_as_of=None)
    assert calls[-1]["market_data_mode"] is IndicatorMarketDataMode.LIVE


async def test_live_chart_snapshot_keeps_live_indicator_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前个股页（无 as-of）必须保持 live：15m 展示 provider_direct + Node LIVE_DIRECT。

    防止为修 PIT 而把当前页面一起「修坏」成 DB_ONLY（会让实时页读到旧 DB 分钟线）。
    """
    from app.services import chart_snapshot_service as css

    # 安全网：任何未预期的外部行情入口都只被记录、不做真实网络访问。
    _record_external_calls(monkeypatch)

    node_modes: list[Any] = []
    display_policies: list[Any] = []
    provider_calls: list[int] = []
    real_get_inputs = NodeClusterInputProvider.get_inputs
    real_get_bars = MarketDataAggregationService.get_bars

    async def _spy_get_inputs(
        cls: Any, session: Any, instrument_id: Any, **kwargs: Any,
    ) -> Any:
        node_modes.append(kwargs.get("source_mode"))
        return await real_get_inputs(session, instrument_id, **kwargs)

    async def _spy_get_bars(
        self: Any, session: Any, instrument_id: Any, **kwargs: Any,
    ) -> Any:
        display_policies.append((kwargs.get("timeframe"), kwargs.get("source_policy")))
        return await real_get_bars(self, session, instrument_id, **kwargs)

    monkeypatch.setattr(
        NodeClusterInputProvider, "get_inputs", classmethod(_spy_get_inputs),
    )
    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _spy_get_bars)

    # live：15m 展示 + Node 15m 都必须真的走 provider（provider_direct）
    async def _provider_15m(
        session: Any, instrument_id: Any, *, count: int,
    ) -> pd.DataFrame:
        provider_calls.append(count)
        return _build_15m_bars(count)

    monkeypatch.setattr(mdas, "fetch_15min_bars", _provider_15m)

    # 日线：DB 已完整覆盖到今日 ⇒ need_tail=False，live 日线不必触网
    async def _db_daily(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return _build_daily_bars(400, end=pd.Timestamp.today().normalize())

    monkeypatch.setattr(mdas, "_query_daily_bars", _db_daily)
    monkeypatch.setattr(
        mdas, "_call_expected_last_completed_daily_bar",
        lambda *a, **kw: _async_return(date.today()),
    )
    monkeypatch.setattr(mdas, "AdjustmentFactorService", _FakeAdjService)
    monkeypatch.setattr(
        NodeClusterInputProvider, "_compute_exhaustion_proofs",
        AsyncMock(return_value={"1d": (False, None), "15m": (False, None)}),
    )

    await css.ChartSnapshotService.compute_bars_and_indicators(
        _InstrumentOnlySession(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", bars=100,
        include_realtime=True, completed_only=False,
        adjustment_as_of=None,
    )

    m15 = [p for tf, p in display_policies if tf == "15m"]
    assert m15 and all(p is MarketDataSourcePolicy.PROVIDER_DIRECT for p in m15), (
        f"live 15m 展示必须 provider_direct，实际: {m15}"
    )
    assert node_modes and all(
        m is NodeClusterSourceMode.LIVE_DIRECT for m in node_modes
    ), f"live Node 输入必须 LIVE_DIRECT，实际: {node_modes}"
    assert provider_calls, (
        "用例前提：live 15m 应当真的走 provider；若没走到说明分流被改坏"
    )


def test_request_semantics_helper_is_single_owner() -> None:
    """`is_historical_market_data_request` 是请求语义唯一 owner，且被 resolver 复用。"""
    import inspect

    assert is_historical_market_data_request(adjustment_as_of=None) is False
    assert is_historical_market_data_request(
        adjustment_as_of=date(2026, 7, 1),
    ) is True
    # end_date 回溯窗口
    assert is_historical_market_data_request(
        adjustment_as_of=None, end_date=date(2026, 6, 1), today=date(2026, 9, 22),
    ) is True
    assert is_historical_market_data_request(
        adjustment_as_of=None, end_date=date(2026, 9, 22), today=date(2026, 9, 22),
    ) is False
    # completed_only 不是本函数的判据
    assert "completed_only" not in inspect.signature(
        is_historical_market_data_request
    ).parameters

    # 指标侧的策略/模式派生必须来自同一套 MDAS 规则（不得复制第二份）
    assert _indicator_source_policy("15m", IndicatorMarketDataMode.LIVE) \
        is MarketDataSourcePolicy.PROVIDER_DIRECT
    assert _indicator_source_policy("1d", IndicatorMarketDataMode.LIVE) \
        is MarketDataSourcePolicy.HYBRID
    for tf in ("1d", "15m", "1h", "1w", "1mo", "1m"):
        assert _indicator_source_policy(
            tf, IndicatorMarketDataMode.HISTORICAL_DB,
        ) is MarketDataSourcePolicy.DB_ONLY, tf
    assert _indicator_node_source_mode(IndicatorMarketDataMode.HISTORICAL_DB) \
        is NodeClusterSourceMode.HISTORICAL_DB
    assert _indicator_node_source_mode(IndicatorMarketDataMode.LIVE) \
        is NodeClusterSourceMode.LIVE_DIRECT
    # compute_all_indicators 的默认模式必须是 LIVE（不改变既有调用方行为）
    from app.services.indicator_service import compute_all_indicators

    sig = inspect.signature(compute_all_indicators)
    assert sig.parameters["market_data_mode"].default \
        is IndicatorMarketDataMode.LIVE


# ============================================================
# P1-db-only-zero-network：DB_ONLY 必须自己冻结时间坐标
# ============================================================
# 上一版的「历史零网络」用例全部手工传 ``include_realtime=False`` /
# ``completed_only=True``，等于替实现关掉了
#   1. 1d / 1w / 1mo 的今日 partial daily 合并（fetch_today_daily_bars）
#   2. 复权因子的 as-of 语义（fetch_as_of = None if include_realtime else as_of）
# 于是 DB_ONLY 名义上 zero-network、实际仍会触网。本节三个用例刻意**保留 API 默认参数**
# （include_realtime=True / completed_only=False）并**强制交易时段**，让失败模式可被判据捕获。


def _force_trading_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「交易日 + 早盘」打开，让 partial daily 分支真的会执行。

    本模块有一个 autouse fixture 把交易时段固定为 False；若不反制，
    partial daily 分支会被外部条件关掉，关于它的断言会全部空转。
    """
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: True)
    monkeypatch.setattr(
        mdas, "is_trading_day_async", lambda *a, **kw: _async_return(True),
    )
    monkeypatch.setattr(
        mdas, "compute_market_session",
        lambda now, is_trading_day: mdas.MARKET_SESSION_MORNING,
    )


def test_resolver_all_historical_timeframes_are_db_only() -> None:
    """PIT 合同：historical ⇒ **任意**周期 DB_ONLY（两条历史判据都要成立）。"""
    for tf in ("1d", "15m", "1h", "1w", "1mo", "1m"):
        assert resolve_market_data_source_policy(
            tf, historical=True,
        ) is MarketDataSourcePolicy.DB_ONLY, tf
        # 判据一：显式 as-of
        assert resolve_request_source_policy(
            tf, adjustment_as_of=date(2026, 7, 1), today=date(2026, 9, 22),
        ) is MarketDataSourcePolicy.DB_ONLY, tf
        # 判据二：回溯窗口（end_date < today）
        assert resolve_request_source_policy(
            tf, adjustment_as_of=None, end_date=date(2026, 6, 1),
            today=date(2026, 9, 22),
        ) is MarketDataSourcePolicy.DB_ONLY, tf
    # live 侧合同不变（防止把 PIT 收紧误扩到实时链路）
    assert resolve_market_data_source_policy(
        "1d", historical=False,
    ) is MarketDataSourcePolicy.HYBRID
    assert resolve_market_data_source_policy(
        "15m", historical=False,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT


async def test_db_only_overrides_api_realtime_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB_ONLY 必须覆写调用方传入的 ``include_realtime=True``（1d / 1w / 1mo 同）。

    ``include_realtime`` 不只是「要不要实时 bar」：它同时决定
    ``if timeframe == "1d" and include_realtime:`` 与
    ``if timeframe in ("1w","1mo") and include_realtime and _is_trading_hours(now):``
    两条 partial daily 合并分支。API 默认值就是 True，所以 DB_ONLY 不覆写它
    就等于历史请求仍会 ``fetch_today_daily_bars``。
    """
    external_calls = _record_external_calls(monkeypatch)
    _install_historical_db_readers(monkeypatch)
    _force_trading_session(monkeypatch)

    for idx, tf in enumerate(("1d", "1w", "1mo")):
        result = await MarketDataAggregationService().get_bars(
            _mock_session(), TEST_INSTRUMENT_ID,
            timeframe=tf, adj="qfq", limit=100 + idx,
            include_realtime=True,       # ← API 默认值，刻意保留
            completed_only=False,        # ← API 默认值，刻意保留
            adjustment_as_of=date(2026, 7, 1),
            source_policy=MarketDataSourcePolicy.DB_ONLY,
        )
        assert result.data_source == "db", (
            f"{tf}: DB_ONLY 只允许返回 db 数据，不得 hybrid/degraded，"
            f"实际 {result.data_source}"
        )

    assert external_calls == [], (
        "DB_ONLY 必须覆写 include_realtime=True：1d/1w/1mo 的 partial daily "
        f"合并也绝不能触网，实际触网: {external_calls}"
    )

    # 反向对照（防空转）：同样参数走 HYBRID（allow_backfill=True）时必须真的触网，
    # 否则上面的 [] 只是因为记录器没生效。
    await MarketDataAggregationService().get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1d", adj="qfq", limit=999,
        include_realtime=True, completed_only=False,
        adjustment_as_of=date(2026, 7, 1),
        source_policy=MarketDataSourcePolicy.HYBRID,
    )
    assert "fetch_daily_bars" in external_calls, (
        "对照前提：HYBRID 在 DB 缺尾时本应回补 provider；"
        "没有触发说明本用例的零网络断言是空转"
    )


async def test_db_only_uses_historical_as_of_for_adjustment_factors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB_ONLY 下复权因子必须按请求的 as-of 取，不得退化为「取最新」。

    ``fetch_as_of = None if include_realtime else adjustment_as_of``：
    API 默认 ``include_realtime=True``，因此 DB_ONLY 若不覆写它，
    一根 bar 都不联网也已经破坏 PIT —— 因子序列本身来自「今天」，
    历史结果的 qfq 基准随当前行情漂移。
    """
    _record_external_calls(monkeypatch)
    _install_historical_db_readers(monkeypatch)

    seen_as_of: list[Any] = []

    class _SpyAdjService(_FakeAdjService):
        async def get_factor_series(
            self, *args: Any, **kwargs: Any,
        ) -> pd.DataFrame:
            seen_as_of.append(kwargs.get("as_of"))
            return await super().get_factor_series(*args, **kwargs)

    monkeypatch.setattr(mdas, "AdjustmentFactorService", _SpyAdjService)

    await MarketDataAggregationService().get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1d", adj="qfq", limit=321,
        include_realtime=True,       # ← API 默认值，刻意保留
        completed_only=False,        # ← API 默认值，刻意保留
        adjustment_as_of=date(2026, 7, 1),
        source_policy=MarketDataSourcePolicy.DB_ONLY,
    )

    assert seen_as_of, "用例前提：adj=qfq 必须取一次复权因子"
    assert seen_as_of == [date(2026, 7, 1)], (
        "DB_ONLY 必须用请求的 as-of 取复权因子；取到 None 说明 include_realtime "
        f"未被覆写、因子退化成「最新」= PIT 泄漏，实际: {seen_as_of}"
    )


async def test_historical_chart_snapshot_default_realtime_is_still_db_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史 1d chart-snapshot 在 **API 默认参数**下也整条链零外部行情调用。

    这是上一版真正的盲区：历史用例手工塞了 ``include_realtime=False``，
    等于替实现关掉了 partial daily 与因子 as-of 两条泄漏路径。
    本用例保持 API 默认值 + 强制交易时段，真实跑到
    ``compute_all_indicators``（**不 mock** 指标层）。
    """
    from app.services import chart_snapshot_service as css

    external_calls = _record_external_calls(monkeypatch)
    _install_historical_db_readers(monkeypatch)
    _force_trading_session(monkeypatch)

    result = await css.ChartSnapshotService.compute_bars_and_indicators(
        _InstrumentOnlySession(), TEST_INSTRUMENT_ID,
        timeframe="1d", adj="qfq", bars=100,
        include_realtime=True,       # ← API 默认值，刻意保留
        completed_only=False,        # ← API 默认值，刻意保留
        adjustment_as_of=date(2026, 7, 1),
    )

    assert result.is_empty is False, (
        "用例前提是历史 DB 有数据；否则下面的断言是空转"
    )
    assert external_calls == [], (
        "历史 1d chart-snapshot 在 API 默认参数下必须整条链零外部行情调用"
        f"（展示 bars + 指标日线/分钟/SMC + Node 输入），实际触网: {external_calls}"
    )


# ============================================================
# P1-batch-contract：get_bars_batch 必须消费同一个 source owner 合同
# ============================================================
# 上一版只把 DB_ONLY 归一放进**单股** get_bars，batch 入口（get_bars_batch）完全不解析
# ``source_policy``，于是形成 contract 分叉：
#     单股 historical → zero-network
#     批量 historical → 仍可 fetch_daily_bars()
# 生产 PIT 调用方（feature_snapshot_service）批读 1d/15m 时恰好没有传
# ``allow_backfill=False`` ⇒ batch 默认 True ⇒ DB 缺尾即触网。


def _install_batch_db_readers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    daily_end: str = "2026-06-10",
    factor_as_of_sink: list[Any] | None = None,
) -> None:
    """安装 batch 路径的 repository 替身：DB 批量日线**缺尾** + 因子按 as-of 记录。

    batch 的 bars / 因子来自 ``get_daily_bars_batch`` / ``get_adj_factor_series_batch``
    （不是单股的 ``_query_daily_bars``）。刻意让 DB 日线只到 ``daily_end`` 而预期最后
    完成日是 2026-06-30 ⇒ ``need_tail=True``：凡是 ``allow_backfill`` 仍为 True 的实现
    都会 ``fetch_daily_bars`` 触网，断言因此具备判别力（否则会空转）。
    """

    async def _db_batch_daily(
        session: Any, instrument_ids: list[uuid.UUID], start: Any, end: Any,
    ) -> dict[uuid.UUID, pd.DataFrame]:
        return {iid: _build_daily_bars(400, end=daily_end) for iid in instrument_ids}

    async def _db_batch_factor(
        session: Any, instrument_ids: list[uuid.UUID], as_of: Any = None,
    ) -> dict[uuid.UUID, pd.DataFrame]:
        if factor_as_of_sink is not None:
            factor_as_of_sink.append(as_of)
        return {
            iid: pd.DataFrame({
                "trade_date": pd.to_datetime(["2026-01-01"]),
                "adj_factor": [1.0],
            })
            for iid in instrument_ids
        }

    monkeypatch.setattr(mdas, "get_daily_bars_batch", _db_batch_daily)
    monkeypatch.setattr(mdas, "get_adj_factor_series_batch", _db_batch_factor)


async def test_get_bars_batch_db_only_disables_external_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """batch + DB_ONLY 必须覆写 ``allow_backfill=True`` / ``include_realtime=True``。

    ``allow_backfill`` 决定 ``_build_daily_aggregation`` 的
    ``if need_tail and allow_backfill: fetch_daily_bars(...)``；
    ``include_realtime`` 决定 1w/1mo 的 ``fetch_today_daily_bars`` partial daily 合并。
    batch 默认分别是 True / API 默认 True，所以批读不消费 source_policy
    就等于 1d 和 1w 历史请求都还能联网。
    """
    external_calls = _record_external_calls(monkeypatch)
    _install_historical_db_readers(monkeypatch)
    _install_batch_db_readers(monkeypatch)
    _force_trading_session(monkeypatch)

    for idx, tf in enumerate(("1d", "1w")):
        result = await MarketDataAggregationService().get_bars_batch(
            _mock_session(), [TEST_INSTRUMENT_ID],
            timeframe=tf, adj="qfq", limit=100 + idx,
            allow_backfill=True,        # ← 故意给错：batch 默认就是 True
            include_realtime=True,      # ← API 默认值，刻意保留
            completed_only=False,       # ← API 默认值，刻意保留
            adjustment_as_of=date(2026, 7, 1),
            source_policy=MarketDataSourcePolicy.DB_ONLY,
        )
        single = result[TEST_INSTRUMENT_ID]
        assert isinstance(single, BarAggregationResult), single
        assert single.data_source == "db", (
            f"{tf}: batch DB_ONLY 只允许返回 db 数据，不得 hybrid/degraded，"
            f"实际 {single.data_source}"
        )

    assert external_calls == [], (
        "batch DB_ONLY 必须覆写 allow_backfill/include_realtime："
        "1d 的日线回补与 1w 的 partial daily 合并都绝不能触网，"
        f"实际触网: {external_calls}"
    )

    # 反向对照（防空转）：同参数走 HYBRID（batch 默认 allow_backfill=True）时两条路径
    # 都必须真的触网，否则上面的 [] 只是因为记录器/前置条件没生效。
    for idx, tf in enumerate(("1d", "1w")):
        await MarketDataAggregationService().get_bars_batch(
            _mock_session(), [TEST_INSTRUMENT_ID],
            timeframe=tf, adj="qfq", limit=900 + idx,
            include_realtime=True, completed_only=False,
            adjustment_as_of=date(2026, 7, 1),
            source_policy=MarketDataSourcePolicy.HYBRID,
        )
    assert "fetch_daily_bars" in external_calls, (
        "对照前提：HYBRID batch 在 DB 缺尾时本应回补 provider；"
        "没有触发说明本用例的零网络断言是空转"
    )
    assert "fetch_today_daily_bars" in external_calls, (
        "对照前提：HYBRID batch 在交易时段本应合并今日 partial daily"
    )


async def test_get_bars_batch_db_only_uses_historical_as_of_for_adjustment_factors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """batch DB_ONLY 下复权因子必须按请求 as-of 取，不得退化为「取最新」。

    batch 与单股同源：``fetch_as_of = None if include_realtime else adjustment_as_of``。
    API 默认 ``include_realtime=True`` ⇒ DB_ONLY 若不覆写它，批量历史行情的 qfq 基准
    仍随当前行情漂移（一根 bar 都不联网也照样破坏 PIT）。
    """
    _record_external_calls(monkeypatch)
    _install_historical_db_readers(monkeypatch)
    seen_as_of: list[Any] = []
    _install_batch_db_readers(monkeypatch, factor_as_of_sink=seen_as_of)

    await MarketDataAggregationService().get_bars_batch(
        _mock_session(), [TEST_INSTRUMENT_ID],
        timeframe="1d", adj="qfq", limit=321,
        include_realtime=True,       # ← API 默认值，刻意保留
        completed_only=False,        # ← API 默认值，刻意保留
        adjustment_as_of=date(2026, 7, 1),
        source_policy=MarketDataSourcePolicy.DB_ONLY,
    )

    assert seen_as_of == [date(2026, 7, 1)], (
        "batch DB_ONLY 必须用请求的 as-of 取复权因子；取到 None 说明 include_realtime "
        f"未被覆写、因子退化成「最新」= PIT 泄漏，实际: {seen_as_of}"
    )

    # 反向对照：HYBRID（include_realtime=True）必须退化为 as_of=None（取最新），
    # 证明这条断言真的能区分两种策略，而不是记录器根本没接上。
    await MarketDataAggregationService().get_bars_batch(
        _mock_session(), [TEST_INSTRUMENT_ID],
        timeframe="1d", adj="qfq", limit=322,
        include_realtime=True, completed_only=False,
        adjustment_as_of=date(2026, 7, 1),
        source_policy=MarketDataSourcePolicy.HYBRID,
    )
    assert seen_as_of[-1] is None, (
        f"对照前提：HYBRID 应取最新因子（as_of=None），实际 {seen_as_of}"
    )


class _RecordingSession:
    """最小 session 替身：symbol 映射查询返回空列表，其余不做任何事。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt: Any, params: Any = None) -> list[Any]:
        self.statements.append(" ".join(str(stmt).split()))
        return []

    async def flush(self) -> None:
        return None


async def test_feature_snapshot_pit_batches_declare_db_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """feature_snapshot PIT 批读必须**显式**声明 DB_ONLY（1d primary + 15m secondary）。

    这里锁的是**调用参数**而不是行为：batch 内部不再从 ``adjustment_as_of`` 反推 source，
    PIT 调用方必须自己声明。记录到 ``source_policy`` 为 None 即视为回归。
    """
    from app.services import feature_snapshot_service as fss

    recorded: list[dict[str, Any]] = []

    async def _recording_batch(
        self: Any, session: Any, ids: Any, *, _diag_sink: Any = None, **kwargs: Any,
    ) -> dict[uuid.UUID, Any]:
        recorded.append({
            "timeframe": kwargs.get("timeframe"),
            "source_policy": kwargs.get("source_policy"),
            "allow_backfill": kwargs.get("allow_backfill"),
        })
        # 记录后立刻让每股可见地失败并短路（不进入真实计算链），保持记录纯净。
        return {iid: RuntimeError("probe-stop-after-record") for iid in ids}

    monkeypatch.setattr(
        MarketDataAggregationService, "get_bars_batch", _recording_batch,
    )

    await fss.compute_for_trade_date(
        _RecordingSession(), date(2026, 9, 22), [TEST_INSTRUMENT_ID],
        batch_size=20, failure_threshold=1.0, enforce_compute_once=False,
    )

    by_tf = {r["timeframe"]: r for r in recorded}
    assert set(by_tf) == {"1d", "15m"}, (
        f"PIT 链必须同时批读 1d 与 15m，实际 {sorted(by_tf)}"
    )
    for tf in ("1d", "15m"):
        assert by_tf[tf]["source_policy"] is MarketDataSourcePolicy.DB_ONLY, (
            f"feature_snapshot PIT 批读 {tf} 必须显式声明 DB_ONLY，"
            f"实际 {by_tf[tf]['source_policy']!r}"
            "（None 表示调用方没声明，batch 会退回 HYBRID 默认值）"
        )


def test_all_feature_snapshot_batch_calls_declare_db_only_source_policy() -> None:
    """结构不变式：feature_snapshot_service 中**每一个** batch 调用都必须显式 DB_ONLY。

    该文件的 PIT 批读共三处（review-core run items 1d、feature snapshot 1d/15m）。
    只修其中一处就会让 batch contract 再次分叉，所以用结构断言把全部调用点
    （含未来新增）一起钉住 —— source identity 必须显式声明，不允许靠
    ``allow_backfill`` / ``adjustment_as_of`` 反推。
    """
    import ast
    from pathlib import Path

    from app.services import feature_snapshot_service as fss

    path = Path(fss.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    call_sites: list[Any] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "get_bars_batch":
                call_sites.append(node)

    assert len(call_sites) >= 3, (
        f"预期至少 3 处 PIT 批读调用（review-core 1d + feature snapshot 1d/15m），"
        f"实际 {len(call_sites)}"
    )

    for call in call_sites:
        kw = {k.arg: k.value for k in call.keywords}
        policy = kw.get("source_policy")
        assert policy is not None, (
            f"{path.name}:{call.lineno} get_bars_batch 缺少 source_policy："
            "batch 必须显式声明 source identity，禁止旁路"
        )
        assert isinstance(policy, ast.Attribute) and policy.attr == "DB_ONLY", (
            f"{path.name}:{call.lineno} get_bars_batch 的 source_policy 必须是 DB_ONLY"
        )


async def test_15m_batch_fallback_inherits_db_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15m batch 退回逐股 ``get_bars`` 时必须把 source_policy 一起带过去。

    否则 15m 历史请求虽然因为 include_realtime=False 暂时「行为碰巧等价」，
    但声明仍是 HYBRID —— 一旦 HYBRID 以后增加功能就会复发。
    """
    captured: list[dict[str, Any]] = []
    sentinel = RuntimeError("per-symbol-fallback-sentinel")

    async def _spy_get_bars(
        self: Any, session: Any, instrument_id: Any, *args: Any, **kwargs: Any,
    ) -> Any:
        captured.append(kwargs)
        return sentinel

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _spy_get_bars)

    diag: dict[str, Any] = {}
    result = await MarketDataAggregationService().get_bars_batch(
        _mock_session(), [TEST_INSTRUMENT_ID],
        timeframe="15m", adj="qfq", limit=400,
        include_realtime=False, completed_only=True,
        end_date=date(2026, 9, 22), adjustment_as_of=date(2026, 9, 22),
        source_policy=MarketDataSourcePolicy.DB_ONLY,
        _diag_sink=diag,
    )

    assert diag.get("read_mode") == "per_symbol_fallback", (
        "用例前提：15m 必须走逐股 fallback 分支，否则下面的断言是空转"
    )
    assert captured, "15m fallback 必须逐股调用 get_bars"
    assert all(
        kw.get("source_policy") is MarketDataSourcePolicy.DB_ONLY for kw in captured
    ), (
        "batch fallback 必须继承同一个 source_policy；否则 15m 历史请求会落回 HYBRID，"
        f"实际 {captured}"
    )
    assert result[TEST_INSTRUMENT_ID] is sentinel
