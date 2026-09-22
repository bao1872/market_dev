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
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    MarketDataAggregationService,
    MarketDataSourcePolicy,
    resolve_display_source_policy,
)
from app.services.node_cluster_input_provider import (
    NodeClusterInputProvider,
    NodeClusterSourceMode,
    _resolve_node_15m_source_policy,
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


def test_resolve_display_source_policy_is_single_owner() -> None:
    assert resolve_display_source_policy("15m") is MarketDataSourcePolicy.PROVIDER_DIRECT
    assert resolve_display_source_policy("1h") is MarketDataSourcePolicy.PROVIDER_DIRECT
    for tf in ("1d", "1w", "1mo", "1m"):
        assert resolve_display_source_policy(tf) is MarketDataSourcePolicy.HYBRID


def test_node_source_mode_maps_15m_policy_explicitly() -> None:
    assert (
        _resolve_node_15m_source_policy(NodeClusterSourceMode.LIVE_DIRECT)
        is MarketDataSourcePolicy.PROVIDER_DIRECT
    )
    assert (
        _resolve_node_15m_source_policy(NodeClusterSourceMode.HISTORICAL_DB)
        is MarketDataSourcePolicy.DB_ONLY
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
    """HISTORICAL_DB：Node 的 15m 输入必须声明 db_only（PIT 禁止触网）。"""
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
    assert by_tf["1d"]["source_policy"] is MarketDataSourcePolicy.HYBRID


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
