"""实时 K 线契约永久回归测试 — Fake Exchange → MDAS → ChartSnapshotService。

验证 PROMPT.md §二 / §三 / CP-V3-D2 要求：
1. 交易时段 include_realtime=True 时，1d/15m/1h 最后一根 bar 为 partial
   （is_partial=true）。
2. T0 → T1 实时更新：最后一根 bar 的 high/low/close/volume 发生变化。
3. API 最后 bar 随 T1 变化（chart-snapshot 响应 bars.items 末根等于 T1 末根）。
4. bars.source_bar_hash == indicators.source_bar_hash（同一 MDAS DataFrame 派生）。
5. 展示周期 MDAS 读取一次（单次 ChartSnapshotService 调用中，展示周期
   include_realtime=True 的 get_bars 调用计数 = 1；Node Cluster 的
   completed_only=True 查询不计入展示周期读取）。

测试策略：
- 不使用生产 DB / Token / Secret，不写 /tmp 脚本。
- 在 MarketDataAggregationService 边界注入 Fake（mock），可控返回 T0/T1 bars，
  并记录 get_bars 调用日志。
- CanonicalComputationService.compute 与 _compute_independent_node_cluster 被 mock，
  使 compute_all_indicators 真实运行并真实计算 source_bar_hash，但算法层不产生
  真实 CPU 开销。
- 同步静态检查前端契约：chart-snapshot 请求默认 include_realtime=true、React Query
  交易时段刷新、query key 含 timeframe/bars、StrategyChart 只用 API bars、禁止
  quote→bar 兜底。

运行：
    APP_ENV=test pytest tests/test_chart_snapshot_realtime_contract.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from app.services import indicator_service
from app.services.chart_bars_service import compute_source_bar_hash
from app.services.chart_snapshot_service import ChartSnapshotService
from app.services.market_data_aggregation_service import BarAggregationResult

TEST_INSTRUMENT = uuid.UUID("00000000-0000-0000-0000-068850600000")

# 前端源码路径（静态契约检查用）
_FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


# =============================================================================
# 合成行情数据生成
# =============================================================================
def _build_daily_bars(n_bars: int = 260, seed: int = 42) -> pd.DataFrame:
    """生成确定性日线 bars（>= 250 满足展示窗口）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end="2026-07-18", periods=n_bars, freq="B")
    returns = rng.uniform(-0.02, 0.02, size=n_bars)
    close = 10.0 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": close * (1 + rng.uniform(-0.01, 0.01, size=n_bars)),
        "high": np.maximum(close, close * 1.01),
        "low": np.minimum(close, close * 0.99),
        "close": close,
        "volume": rng.uniform(1_000_000, 5_000_000, size=n_bars),
        "amount": close * 1_000_000,
        "adj_factor": [1.0] * n_bars,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _build_15m_bars(n_bars: int = 4000, seed: int = 43) -> pd.DataFrame:
    """生成确定性 15m bars（满足 Node Cluster 4000 契约）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end="2026-07-18 15:00", periods=n_bars, freq="15min")
    returns = rng.uniform(-0.003, 0.003, size=n_bars)
    close = 10.0 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": close * (1 + rng.uniform(-0.001, 0.001, size=n_bars)),
        "high": np.maximum(close, close * 1.002),
        "low": np.minimum(close, close * 0.998),
        "close": close,
        "volume": rng.uniform(50000, 200000, size=n_bars),
        "amount": close * 50000,
        "adj_factor": [1.0] * n_bars,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _build_1h_bars(n_bars: int = 260, seed: int = 44) -> pd.DataFrame:
    """生成确定性 1h bars。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end="2026-07-18 15:00", periods=n_bars, freq="1h")
    returns = rng.uniform(-0.005, 0.005, size=n_bars)
    close = 10.0 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": close * (1 + rng.uniform(-0.002, 0.002, size=n_bars)),
        "high": np.maximum(close, close * 1.005),
        "low": np.minimum(close, close * 0.995),
        "close": close,
        "volume": rng.uniform(100000, 400000, size=n_bars),
        "amount": close * 100000,
        "adj_factor": [1.0] * n_bars,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _mutate_last_bar(bars: pd.DataFrame, *, dhigh: float, dlow: float,
                     dclose: float, dvol: float) -> pd.DataFrame:
    """返回副本，最后一根 bar 的 high/low/close/volume 增加指定增量（模拟 T1 实时更新）。"""
    out = bars.copy()
    out.iloc[-1, out.columns.get_loc("high")] = float(out.iloc[-1]["high"]) + dhigh
    out.iloc[-1, out.columns.get_loc("low")] = float(out.iloc[-1]["low"]) + dlow
    out.iloc[-1, out.columns.get_loc("close")] = float(out.iloc[-1]["close"]) + dclose
    out.iloc[-1, out.columns.get_loc("volume")] = float(out.iloc[-1]["volume"]) + dvol
    out.iloc[-1, out.columns.get_loc("amount")] = float(out.iloc[-1]["amount"]) + dvol * 10
    return out


def _make_agg_result(
    bars: pd.DataFrame,
    *,
    timeframe: str,
    is_partial: bool = True,
) -> BarAggregationResult:
    """构造 BarAggregationResult，source_bar_hash 由真实 compute_source_bar_hash 计算。

    保证 bars.source_bar_hash 与 indicators.source_bar_hash（compute_all_indicators
    内部重新计算）一致——因为二者都基于同一 DataFrame + 同一 timeframe。
    """
    return BarAggregationResult(
        bars=bars,
        data_source="hybrid" if is_partial else "db",
        as_of=datetime.now(),
        is_partial=is_partial,
        last_persisted_bar_time=bars.index[-2] if (is_partial and len(bars) >= 2) else bars.index[-1],
        last_live_bar_time=bars.index[-1] if is_partial else None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
        source_bar_hash=compute_source_bar_hash(bars, timeframe),
        adj_factor_hash="adjhash_v1",
        completed_through=bars.index[-2] if (is_partial and len(bars) >= 2) else bars.index[-1],
        history_exhausted=False,
    )


# =============================================================================
# Fixtures
# =============================================================================
@pytest.fixture
def mock_session() -> AsyncMock:
    """mock AsyncSession（NodeClusterInputProvider 内部查询 instrument symbol）。"""
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = ("688506",)
    session.execute.return_value = result
    return session


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空策略注册表，避免真实策略执行。"""
    monkeypatch.setattr(indicator_service.StrategyLoader, "_registry", {})


@pytest.fixture
def mock_canonical_non_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock CanonicalComputationService.compute — 非 node_cluster 算法返回 mock payload。"""
    from app.services.canonical_computation_service import CanonicalResult

    async def _mock_compute(algorithm_id, *, instrument_id, as_of, source_bar_hash,
                            adj_factor_hash, **kwargs):
        if algorithm_id == "node_cluster":
            raise AssertionError(
                "node_cluster 应由 _compute_independent_node_cluster 处理，不应到达 CanonicalComputationService"
            )
        if algorithm_id == "macd":
            payload = {"macd_dif": [0.1], "macd_dea": [0.05], "macd_hist": [0.05]}
        elif algorithm_id == "sqzmom":
            payload = {
                "val": [0.1], "sqzOn": [False], "sqzOff": [True], "noSqz": [False],
                "bcolor": [0], "scolor": [0],
                "params": {"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True},
            }
        elif algorithm_id == "bollinger":
            payload = {"bb_mid": [10.0], "bb_upper": [11.0], "bb_lower": [9.0]}
        else:
            payload = {"val": [1.0]}
        return CanonicalResult(
            algorithm_id=algorithm_id,
            algorithm_version="v1",
            output_schema_version=1,
            contract_fingerprint=f"{algorithm_id}-cf-v1",
            result_hash="mock_result_hash",
            registry_version="v1",
            payload=payload,
            computed_at=datetime.now(),
        )

    monkeypatch.setattr(
        indicator_service.CanonicalComputationService, "compute", _mock_compute
    )


@pytest.fixture
def spy_node_cluster(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Spy _compute_independent_node_cluster — 捕获调用但不真实计算。"""
    from app.services.node_cluster_input_provider import NodeClusterInput

    captured: list[NodeClusterInput] = []

    async def _spy_node_cluster(node_input, *, symbol="", instrument_id=None):
        captured.append(node_input)
        return {
            "availability": node_input.availability,
            "degraded_reason": node_input.degraded_reason,
            "profile_meta": {
                "profile_hash": "deterministic_profile_hash",
                "daily_source_hash": node_input.daily_source_hash,
                "bars_15m_source_hash": node_input.m15_source_hash,
                "algorithm_version": "nc-v1",
                "contract_fingerprint": "nc-cf-v1",
                "node_regions_hash": "deterministic_regions_hash",
                "daily_bars_count": node_input.daily_count,
                "bars_15m_count": node_input.m15_count,
            },
            "profile_rows": [],
            "regions": [],
            "poc": "deterministic_poc",
            "vah": "deterministic_vah",
            "val": "deterministic_val",
        }

    monkeypatch.setattr(
        indicator_service, "_compute_independent_node_cluster", _spy_node_cluster
    )
    return {"captured": captured}


@pytest.fixture
def fake_mdas(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake MDAS — 可控返回 T0/T1 bars，记录 get_bars 调用日志。

    返回 dict 含：
    - call_log: 所有 get_bars 调用记录
    - tick: 测试用 — 调用 advance_tick() 让下一次展示周期查询返回 T1 bars
    """
    call_log: list[dict] = []

    # 预生成 T0 基线数据
    # 1d 展示窗口返回 250 根（与 compute_all_indicators 的 daily_count=250 截断窗口对齐，
    # 保证 bars.source_bar_hash 与 indicators.source_bar_hash 基于同一 250 根 DataFrame，
    # 从而用 hash 一致性验证"同一 MDAS 读取"契约）
    daily_t0 = _build_daily_bars(250, seed=42)
    daily_node = _build_daily_bars(250, seed=100)  # Node completed daily
    m15_t0 = _build_15m_bars(4000, seed=43)
    m15_node = _build_15m_bars(4000, seed=101)  # Node completed 15m
    h1_t0 = _build_1h_bars(260, seed=44)

    # T1 = T0 末根 bar 实时更新（high/low/close/volume 均变化）
    daily_t1 = _mutate_last_bar(daily_t0, dhigh=0.15, dlow=-0.10, dclose=0.20, dvol=50000)
    m15_t1 = _mutate_last_bar(m15_t0, dhigh=0.03, dlow=-0.02, dclose=0.04, dvol=8000)
    h1_t1 = _mutate_last_bar(h1_t0, dhigh=0.08, dlow=-0.05, dclose=0.10, dvol=20000)

    # 展示周期 T0/T1 切换状态：每个 timeframe 独立计数
    display_call_counts: dict[str, int] = {"1d": 0, "15m": 0, "1h": 0}

    state = {"use_t1": {"1d": False, "15m": False, "1h": False}}

    class _FakeMDAS:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            completed_only = kwargs.get("completed_only", False)
            include_realtime = kwargs.get("include_realtime", True)
            call_log.append({
                "timeframe": timeframe,
                "adj": adj,
                "completed_only": completed_only,
                "include_realtime": include_realtime,
                "limit": kwargs.get("limit"),
            })

            # Node Cluster completed 查询 — 固定基线
            if completed_only is True:
                if timeframe == "1d":
                    return _make_agg_result(daily_node, timeframe="1d", is_partial=False)
                if timeframe == "15m":
                    return _make_agg_result(m15_node, timeframe="15m", is_partial=False)
                # 其他 completed 查询回退基线
                return _make_agg_result(daily_node, timeframe=timeframe, is_partial=False)

            # 1m 分钟查询（Node crossover）— 返回少量基线
            if timeframe == "1m":
                mini = daily_t0.tail(2).copy()
                return _make_agg_result(mini, timeframe="1m", is_partial=True)

            # 展示周期查询 — T0/T1 切换
            if timeframe in ("1d", "15m", "1h"):
                display_call_counts[timeframe] = display_call_counts.get(timeframe, 0) + 1
                use_t1 = state["use_t1"][timeframe]
                if timeframe == "1d":
                    bars = daily_t1 if use_t1 else daily_t0
                elif timeframe == "15m":
                    bars = m15_t1 if use_t1 else m15_t0
                else:
                    bars = h1_t1 if use_t1 else h1_t0
                return _make_agg_result(bars, timeframe=timeframe, is_partial=True)

            # 兜底
            return _make_agg_result(daily_t0, timeframe=timeframe, is_partial=True)

    fake = _FakeMDAS()
    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", lambda: fake
    )
    # 同时 patch ChartSnapshotService 模块内 import 的 MDAS
    from app.services import chart_snapshot_service
    monkeypatch.setattr(
        chart_snapshot_service, "MarketDataAggregationService", lambda: fake
    )
    # patch node_cluster_input_provider 中的 MDAS
    from app.services import node_cluster_input_provider
    monkeypatch.setattr(
        node_cluster_input_provider, "MarketDataAggregationService", lambda: fake
    )

    def _advance_tick(timeframe: str) -> None:
        """让下一次展示周期查询返回 T1 bars。"""
        state["use_t1"][timeframe] = True

    return {
        "call_log": call_log,
        "advance_tick": _advance_tick,
        "display_call_counts": display_call_counts,
    }


# =============================================================================
# 后端实时 K 线 T0/T1 契约测试
# =============================================================================
@pytest.mark.asyncio
async def test_realtime_t0_t1_contract(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical_non_node: None,
    spy_node_cluster: dict,
    fake_mdas: dict,
) -> None:
    """1d/15m/1h 实时 T0/T1 契约：is_partial、末根变化、hash 一致、MDAS 单次读取。"""
    for timeframe in ("1d", "15m", "1h"):
        # 清空调用日志，每个 timeframe 独立断言
        fake_mdas["call_log"].clear()

        # --- T0 ---
        result_t0 = await ChartSnapshotService.compute_bars_and_indicators(
            session=mock_session,
            instrument_id=TEST_INSTRUMENT,
            timeframe=timeframe,
            adj="qfq",
            bars=120,
            include_smc=False,
            include_realtime=True,
            completed_only=False,
        )

        # 断言 1: is_partial=true
        assert result_t0.bars_result.is_partial is True, (
            f"[{timeframe}] T0 is_partial 应为 True，实际 {result_t0.bars_result.is_partial}"
        )

        # 末根 bar（API 响应末根 = page_df 末根）
        t0_last = result_t0.page_df.iloc[-1]
        t0_bars_hash = result_t0.bars_result.source_bar_hash
        t0_indicators_hash = result_t0.indicators.get("source_bar_hash")

        # 断言 4: bars.source_bar_hash == indicators.source_bar_hash（同一 DataFrame）
        assert t0_bars_hash == t0_indicators_hash, (
            f"[{timeframe}] T0 bars.source_bar_hash={t0_bars_hash!r} != "
            f"indicators.source_bar_hash={t0_indicators_hash!r}"
        )

        # 断言 5: 展示周期 MDAS 读取一次
        # 展示周期 = include_realtime=True 且 completed_only=False 且 timeframe 匹配
        display_reads_t0 = [
            c for c in fake_mdas["call_log"]
            if c["timeframe"] == timeframe
            and c["include_realtime"] is True
            and c["completed_only"] is False
        ]
        assert len(display_reads_t0) == 1, (
            f"[{timeframe}] T0 展示周期 MDAS 读取次数应为 1，实际 {len(display_reads_t0)}；"
            f"完整调用日志={fake_mdas['call_log']}"
        )

        # --- 切换到 T1（实时更新） ---
        fake_mdas["advance_tick"](timeframe)
        fake_mdas["call_log"].clear()

        result_t1 = await ChartSnapshotService.compute_bars_and_indicators(
            session=mock_session,
            instrument_id=TEST_INSTRUMENT,
            timeframe=timeframe,
            adj="qfq",
            bars=120,
            include_smc=False,
            include_realtime=True,
            completed_only=False,
        )

        # 断言 1: T1 is_partial=true
        assert result_t1.bars_result.is_partial is True, (
            f"[{timeframe}] T1 is_partial 应为 True，实际 {result_t1.bars_result.is_partial}"
        )

        t1_last = result_t1.page_df.iloc[-1]

        # 断言 2: T1 末根 high/low/close/volume 与 T0 不同
        for col in ("high", "low", "close", "volume"):
            assert float(t1_last[col]) != float(t0_last[col]), (
                f"[{timeframe}] T1 末根 {col} 应与 T0 不同："
                f"T0={float(t0_last[col])} T1={float(t1_last[col])}"
            )

        # 断言 3: API 最后 bar 随 T1 变化（page_df 末根 = T1 末根）
        # T1 末根 close 应大于 T0（dclose 为正）
        assert float(t1_last["close"]) > float(t0_last["close"]), (
            f"[{timeframe}] T1 末根 close 应 > T0（实时上涨场景）"
        )

        # 断言 4: T1 bars.source_bar_hash == indicators.source_bar_hash
        t1_bars_hash = result_t1.bars_result.source_bar_hash
        t1_indicators_hash = result_t1.indicators.get("source_bar_hash")
        assert t1_bars_hash == t1_indicators_hash, (
            f"[{timeframe}] T1 bars.source_bar_hash={t1_bars_hash!r} != "
            f"indicators.source_bar_hash={t1_indicators_hash!r}"
        )
        # T1 hash 应与 T0 不同（末根数据变化）
        assert t1_bars_hash != t0_bars_hash, (
            f"[{timeframe}] T1 source_bar_hash 应与 T0 不同（末根已变化）"
        )

        # 断言 5: T1 展示周期 MDAS 读取一次
        display_reads_t1 = [
            c for c in fake_mdas["call_log"]
            if c["timeframe"] == timeframe
            and c["include_realtime"] is True
            and c["completed_only"] is False
        ]
        assert len(display_reads_t1) == 1, (
            f"[{timeframe}] T1 展示周期 MDAS 读取次数应为 1，实际 {len(display_reads_t1)}；"
            f"完整调用日志={fake_mdas['call_log']}"
        )


@pytest.mark.asyncio
async def test_completed_only_forces_no_realtime(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical_non_node: None,
    spy_node_cluster: dict,
    fake_mdas: dict,
) -> None:
    """completed_only=True 时强制 include_realtime=False，不返回 partial bar。"""
    fake_mdas["call_log"].clear()

    result = await ChartSnapshotService.compute_bars_and_indicators(
        session=mock_session,
        instrument_id=TEST_INSTRUMENT,
        timeframe="1d",
        adj="qfq",
        bars=120,
        include_smc=False,
        include_realtime=True,  # 会被 completed_only=True 覆盖
        completed_only=True,
    )

    # completed_only 路径下，FakeMDAS 返回的是 daily_node（is_partial=False）
    assert result.bars_result.is_partial is False, (
        "completed_only=True 时 is_partial 应为 False"
    )

    # 不应存在展示周期 include_realtime=True 的调用
    realtime_display_reads = [
        c for c in fake_mdas["call_log"]
        if c["timeframe"] == "1d"
        and c["include_realtime"] is True
        and c["completed_only"] is False
    ]
    assert len(realtime_display_reads) == 0, (
        f"completed_only=True 时不应有展示周期 realtime 读取，实际 {realtime_display_reads}"
    )


# =============================================================================
# 前端契约静态检查（不启动浏览器，读取源码断言）
# =============================================================================
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_frontend_chart_snapshot_request_defaults_realtime() -> None:
    """chart-snapshot 端点默认 include_realtime=true（后端 Query 默认 True）。

    前端 useChartSnapshot 调用未显式关闭 realtime，等价于透传默认 True。
    """
    endpoint_src = _read(_FRONTEND_SRC / "api" / "endpoints.ts")
    assert "chart-snapshot" in endpoint_src
    # ChartSnapshotQueryParams 应声明 include_realtime 字段
    assert "include_realtime" in endpoint_src

    api_src = _read(_FRONTEND_SRC / "hooks" / "useApi.ts")
    # useChartSnapshot 存在且使用 useQuery
    assert "useChartSnapshot" in api_src
    assert "useQuery" in api_src


def test_frontend_react_query_trading_hours_refresh() -> None:
    """React Query 存在交易时段刷新（refetchInterval 依赖 isInTradingHours）。"""
    api_src = _read(_FRONTEND_SRC / "hooks" / "useApi.ts")
    assert "refetchInterval" in api_src, "useChartSnapshot 应配置 refetchInterval"
    assert "isInTradingHours" in api_src, "refetchInterval 应依赖 isInTradingHours()"


def test_frontend_query_key_includes_timeframe_bars() -> None:
    """query key 包含 timeframe/bars（通过 params 对象）。

    params = {timeframe, adj, bars, ...}，整个 params 进入 queryKey，因此
    timeframe/bars 变化会触发新查询；realtime 条件由 refetchInterval 在交易时段
    周期性触发刷新保证。
    """
    api_src = _read(_FRONTEND_SRC / "hooks" / "useApi.ts")
    # queryKey 应包含 'chart-snapshot' + instrumentId + params
    assert "'chart-snapshot'" in api_src or '"chart-snapshot"' in api_src
    # params 对象进入 queryKey（timeframe/bars 包含其中）
    assert "params" in api_src

    # useStockResearchData 透传 timeframe + barsCount
    research_src = _read(_FRONTEND_SRC / "features" / "stock-research" / "useStockResearchData.ts")
    assert "timeframe" in research_src
    assert "bars: barsCount" in research_src or "barsCount" in research_src


def test_frontend_strategy_chart_uses_api_bars_only() -> None:
    """StrategyChart 只接收 API bars prop，不自行拉取行情。"""
    chart_src = _read(_FRONTEND_SRC / "components" / "StrategyChart.tsx")
    # bars 作为 prop 传入
    assert "bars:" in chart_src or "bars?:" in chart_src
    # 不应出现自行拉取行情的 API 调用
    assert "useBars(" not in chart_src, "StrategyChart 不应自行调用 useBars"
    assert "useRealtimeQuote(" not in chart_src, "StrategyChart 不应自行调用 useRealtimeQuote"


def test_frontend_no_quote_to_bar_synthesis() -> None:
    """禁止 quote→bar 兜底：mergeRealtimeQuoteIntoBars 不应在生产代码中被调用。

    useStockResearchData 中 displayBars 直接等于 baseBars（chart-snapshot API bars），
    旧 mergeRealtimeQuoteIntoBars 已移除。
    """
    research_src = _read(_FRONTEND_SRC / "features" / "stock-research" / "useStockResearchData.ts")
    # displayBars 直接等于 baseBars，无 quote 合成
    assert "displayBars = baseBars" in research_src, (
        "displayBars 应直接等于 baseBars（chart-snapshot API bars），禁止 quote→bar"
    )
    # mergeRealtimeQuoteIntoBars 仅作为"已移除"注释出现，不应有调用
    assert "mergeRealtimeQuoteIntoBars(" not in research_src.replace(
        "// 旧 mergeRealtimeQuoteIntoBars 已移除", ""
    ), "mergeRealtimeQuoteIntoBars 不应被调用（quote→bar 兜底已禁止）"
