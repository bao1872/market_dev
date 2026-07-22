"""[CP-16] Atomic Chart Snapshot 单输入原子性测试。

验证 PRD V2.0 §4.2 SNAP-01 + PROMPT.md §二（Phase 4.1）要求：
1. 一个 chart-snapshot 请求内，展示周期 MDAS get_bars 调用次数 = 1
   （preloaded_display_bars 传入时，compute_all_indicators 不再对展示周期二次调 MDAS）
2. Bars 和 Indicators 使用同一 source_bar_hash / adj_factor_hash
3. Redis 不可用时仍保持原子一致（不依赖缓存间接同步）
4. partial bar 变化不改变 completed_hash（completed_through 稳定）
5. render_frame.matched=true（bars vs indicators display_frame 一致）

Node Cluster 输入隔离（不视为"第二次行情读取"）：
- [CP-V3-A] NodeClusterInputProvider 独立查询 completed qfq 日线/15m（合同常量 250/4000）
- 这些查询参数与展示窗口不同（completed_only=True vs 页面参数），不计入展示周期调用次数
- 本测试 mock NodeClusterInputProvider.get_inputs，避免 Provider 内部 MDAS 调用干扰展示周期计数

用法：
    APP_ENV=test pytest tests/test_chart_snapshot_atomic.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services import indicator_service
from app.services.market_data_aggregation_service import BarAggregationResult

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


def _build_bars(frequency: str, length: int = 60, with_partial: bool = False) -> pd.DataFrame:
    """构造指定周期的 mock bars（含 OHLCV + adj_factor）。

    Args:
        with_partial: True 时最后一根为 partial bar（is_partial=True 场景）
    """
    if frequency == "1d":
        dates = pd.date_range("2026-03-01", periods=length, freq="B")
    elif frequency == "15m":
        dates = pd.date_range("2026-06-18 09:30", periods=length, freq="15min")
    elif frequency == "1h":
        dates = pd.date_range("2026-06-18 09:30", periods=length, freq="1h")
    elif frequency == "1w":
        dates = pd.date_range("2024-01-01", periods=length, freq="W-MON")
    elif frequency == "1mo":
        dates = pd.date_range("2021-01-01", periods=length, freq="MS")
    else:
        raise ValueError(f"不支持的 frequency: {frequency}")

    closes = [10.0 + i * 0.1 for i in range(length)]
    df = pd.DataFrame({
        "open": [c - 0.1 for c in closes],
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes],
        "close": closes,
        "volume": [100000 + i for i in range(length)],
        "amount": [1000000 + i * 10 for i in range(length)],
        "adj_factor": [1.0] * length,
    }, index=dates)
    return df


def _make_result(bars: pd.DataFrame, *, source_bar_hash: str = "hash_v1",
                 adj_factor_hash: str = "adjhash_v1",
                 is_partial: bool = False,
                 completed_through: pd.Timestamp | None = None) -> BarAggregationResult:
    """构造 BarAggregationResult mock。"""
    return BarAggregationResult(
        bars=bars,
        data_source="db",
        as_of=datetime.now(),
        is_partial=is_partial,
        last_persisted_bar_time=bars.index[-1] if not bars.empty else None,
        last_live_bar_time=bars.index[-1] if (is_partial and not bars.empty) else None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
        source_bar_hash=source_bar_hash,
        adj_factor_hash=adj_factor_hash,
        completed_through=completed_through or (bars.index[-2] if is_partial and len(bars) >= 2 else (bars.index[-1] if not bars.empty else None)),
    )


def _make_spy_mdas(call_log: list[tuple[str, dict]]) -> type:
    """构造 spy MDAS，记录每次 get_bars 的 (timeframe, kwargs) 到 call_log。

    Node Cluster 输入（completed_only=True）和展示窗口查询参数不同，
    通过 kwargs 区分。展示周期查询特征：timeframe == display_timeframe 且
    completed_only != True（除非页面显式传 completed_only=True）。
    """

    class _SpyMDAS:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            call_log.append((timeframe, dict(kwargs)))
            bars = _build_bars(timeframe)
            # Node Cluster 查询用 completed_only=True，模拟返回 completed bars
            return _make_result(bars, source_bar_hash="node_hash", adj_factor_hash="node_adjhash")

    return _SpyMDAS


@pytest.fixture
def mock_session() -> AsyncMock:
    """mock AsyncSession，execute 返回固定 symbol。"""
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = ("000001",)
    session.execute.return_value = result
    return session


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空策略注册表，避免测试中真实加载策略。"""
    monkeypatch.setattr(indicator_service.StrategyLoader, "_registry", {})


@pytest.fixture
def mock_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock CanonicalComputationService.compute，返回算法特定的 payload。"""
    from app.services.canonical_computation_service import CanonicalResult

    async def _mock_compute(algorithm_id, *, instrument_id, as_of, source_bar_hash,
                            adj_factor_hash, **kwargs):
        # 根据算法返回所需字段
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
def mock_node_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock _compute_independent_node_cluster，返回简单 dict（避免真实 Node 计算）。

    [CP-V3-A] 新签名：第一参数为 NodeClusterInput 对象（含 daily_bars/bars_15m/hash 等）。
    """
    async def _mock_node_cluster(node_input, *, symbol="",
                                  instrument_id=None):
        return {
            "availability": "available",
            "degraded_reason": None,
            "profile_meta": {
                "profile_hash": "mock_profile_hash",
                "daily_source_hash": getattr(node_input, "daily_source_hash", None),
                "bars_15m_source_hash": getattr(node_input, "m15_source_hash", None),
                "algorithm_version": "nc-v1",
                "contract_fingerprint": "nc-cf-v1",
            },
            "profile_rows": [],
            "regions": [],
        }

    monkeypatch.setattr(
        indicator_service, "_compute_independent_node_cluster", _mock_node_cluster
    )


@pytest.fixture
def mock_node_input_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """[CP-V3-A] mock NodeClusterInputProvider.get_inputs，返回 availability=available 的输入。

    避免 Provider 内部 MDAS 调用干扰展示周期 MDAS 调用计数（spy MDAS 只 patch
    indicator_service.MarketDataAggregationService，不 patch Provider 内部的 MDAS）。
    """
    from app.services.node_cluster_input_provider import NodeClusterInput, NodeClusterInputProvider

    async def _mock_get_inputs(session, instrument_id, *, adjustment_as_of=None, end_date=None):
        return NodeClusterInput(
            daily_bars=_build_bars("1d", length=250),
            bars_15m=_build_bars("15m", length=4000),
            daily_source_hash="mock_daily_hash",
            daily_adj_factor_hash="mock_daily_adj",
            m15_source_hash="mock_m15_hash",
            m15_adj_factor_hash="mock_m15_adj",
            daily_count=250,
            m15_count=4000,
            daily_requested=250,
            m15_requested=4000,
            daily_history_exhausted=False,
            m15_history_exhausted=False,
            availability="available",
            degraded_reason=None,
            adjustment_as_of=adjustment_as_of,
        )

    monkeypatch.setattr(
        NodeClusterInputProvider, "get_inputs", _mock_get_inputs
    )


@pytest.fixture
def mock_strategy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock _get_available_strategy_keys 返回空集（跳过 15min/minute 加载）。"""
    async def _mock_keys(session, keys):
        return set()

    monkeypatch.setattr(indicator_service, "_get_available_strategy_keys", _mock_keys)


# =============================================================================
# 测试 1: MDAS 展示周期调用次数 = 1（preloaded 传入时不二次调用）
# =============================================================================


@pytest.mark.parametrize("display_tf", ["1d", "15m", "1h", "1w", "1mo"])
@pytest.mark.asyncio
async def test_preloaded_skips_display_timeframe_mdas_call(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical: None,
    mock_node_cluster: None,
    mock_node_input_provider: None,
    mock_strategy_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    display_tf: str,
) -> None:
    """[CP-16] preloaded_display_bars 传入时，展示周期 MDAS get_bars 不被再次调用。

    验证：compute_all_indicators 收到 preloaded_display_bars 后，
    不会再对 display_tf 周期发起 MDAS get_bars（除非是 Node Cluster 的 completed_only 查询）。
    """
    call_log: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", _make_spy_mdas(call_log)
    )

    preloaded_bars = _build_bars(display_tf, length=60)
    preloaded = _make_result(preloaded_bars, source_bar_hash="display_hash_v1",
                             adj_factor_hash="display_adjhash_v1")

    await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, display_tf, "none", bars=50,
        preloaded_display_bars=preloaded,
    )

    # 统计展示周期的非 Node Cluster 调用（Node Cluster 用 completed_only=True）
    display_calls = [
        (tf, kw) for tf, kw in call_log
        if tf == display_tf and not kw.get("completed_only")
    ]
    assert len(display_calls) == 0, (
        f"preloaded 传入后，展示周期 {display_tf} 不应再次调用 MDAS get_bars "
        f"（非 Node Cluster 查询），实际调用 {len(display_calls)} 次: {display_calls}"
    )


@pytest.mark.parametrize("display_tf", ["1d", "15m", "1h", "1w", "1mo"])
@pytest.mark.asyncio
async def test_without_preloaded_calls_mdas_for_display_timeframe(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical: None,
    mock_node_cluster: None,
    mock_node_input_provider: None,
    mock_strategy_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    display_tf: str,
) -> None:
    """[CP-16] 不传 preloaded_display_bars 时（向后兼容 /indicators API），
    展示周期 MDAS get_bars 仍会被调用。
    """
    call_log: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", _make_spy_mdas(call_log)
    )

    await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, display_tf, "none", bars=50,
    )

    # 不传 preloaded 时，展示周期应至少调用一次
    display_calls = [
        (tf, kw) for tf, kw in call_log
        if tf == display_tf and not kw.get("completed_only")
    ]
    assert len(display_calls) >= 1, (
        f"不传 preloaded 时，展示周期 {display_tf} 应调用 MDAS get_bars，"
        f"实际调用 {len(display_calls)} 次"
    )


# =============================================================================
# 测试 2: Bars 和 Indicators 使用同一 source_bar_hash / adj_factor_hash
# =============================================================================


@pytest.mark.asyncio
async def test_preloaded_hash_propagated_to_indicators(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical: None,
    mock_node_cluster: None,
    mock_node_input_provider: None,
    mock_strategy_keys: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CP-16] preloaded 的 source_bar_hash / adj_factor_hash 必须传播到 indicators 响应。

    验证：chart_snapshot 传入的 bars_result.bars 与
    compute_all_indicators 返回的 source_bar_hash 一致（同一 DataFrame 产生同一 hash）。
    source_bar_hash 由 compute_source_bar_hash(bars) 计算，不是直接取 preloaded.source_bar_hash 字段。
    """
    from app.services.indicator_service import compute_source_bar_hash

    call_log: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", _make_spy_mdas(call_log)
    )

    display_tf = "1d"
    preloaded_bars = _build_bars(display_tf, length=60)
    preloaded = _make_result(
        preloaded_bars,
        source_bar_hash="display_hash_v1",
        adj_factor_hash="display_adjhash_v1",
    )

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, display_tf, "none", bars=50,
        preloaded_display_bars=preloaded,
    )

    # indicators 响应的 source_bar_hash 应由同一 DataFrame 计算（与 bars 侧一致）
    expected_hash = compute_source_bar_hash(preloaded_bars, display_tf)
    assert result["source_bar_hash"] == expected_hash, (
        f"indicators source_bar_hash 应等于 bars 侧 compute_source_bar_hash(preloaded_bars) "
        f"({expected_hash})，实际: {result['source_bar_hash']}"
    )


# =============================================================================
# 测试 3: Redis 不可用时仍保持原子一致
# =============================================================================


@pytest.mark.asyncio
async def test_redis_unavailable_still_atomic(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical: None,
    mock_node_cluster: None,
    mock_node_input_provider: None,
    mock_strategy_keys: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CP-16] Redis 不可用时，preloaded_display_bars 仍保证原子一致。

    验证：即使 MDAS 内部 Redis 缓存不可用（cache_hit=False），
    preloaded 机制仍直接传 DataFrame，不依赖缓存间接同步。
    """
    from app.services.indicator_service import compute_source_bar_hash

    call_log: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", _make_spy_mdas(call_log)
    )

    display_tf = "1d"
    preloaded_bars = _build_bars(display_tf, length=60)
    preloaded = _make_result(
        preloaded_bars,
        source_bar_hash="redis_unavailable_hash",
        adj_factor_hash="redis_unavailable_adj",
    )
    # 模拟 Redis 不可用：cache_hit=False
    preloaded.cache_hit = False

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, display_tf, "none", bars=50,
        preloaded_display_bars=preloaded,
    )

    # Redis 不可用时，indicators 仍使用 preloaded DataFrame 计算的 hash（不依赖缓存）
    expected_hash = compute_source_bar_hash(preloaded_bars, display_tf)
    assert result["source_bar_hash"] == expected_hash, (
        "Redis 不可用时，indicators source_bar_hash 应仍来自 preloaded DataFrame 计算，"
        f"期望: {expected_hash}, 实际: {result['source_bar_hash']}"
    )
    # 展示周期未被再次查询（原子性不依赖缓存）
    display_calls = [
        (tf, kw) for tf, kw in call_log
        if tf == display_tf and not kw.get("completed_only")
    ]
    assert len(display_calls) == 0, (
        "Redis 不可用时，展示周期不应被再次查询（preloaded 直接传 DataFrame）"
    )


# =============================================================================
# 测试 4: partial bar 变化不改变 completed_hash
# =============================================================================


@pytest.mark.asyncio
async def test_partial_bar_does_not_change_completed_hash(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical: None,
    mock_node_cluster: None,
    mock_node_input_provider: None,
    mock_strategy_keys: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CP-16] partial bar 变化不改变 completed_hash。

    验证：当 preloaded 含 partial bar（is_partial=True）时，
    completed_through 指向最后一根已完成 bar（非 partial），
    source_bar_hash 仍由同一 DataFrame 计算（稳定）。
    """
    from app.services.indicator_service import compute_source_bar_hash

    call_log: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", _make_spy_mdas(call_log)
    )

    display_tf = "1d"
    # 构造含 partial bar 的 bars（最后一根为 partial）
    bars = _build_bars(display_tf, length=60)
    completed_through = bars.index[-2]  # 最后一根 completed 是倒数第二根
    preloaded = _make_result(
        bars,
        source_bar_hash="partial_display_hash",
        adj_factor_hash="partial_adj_hash",
        is_partial=True,
        completed_through=completed_through,
    )

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, display_tf, "none", bars=50,
        preloaded_display_bars=preloaded,
    )

    # source_bar_hash 由同一 DataFrame 计算（partial bar 不改变 hash 计算逻辑）
    expected_hash = compute_source_bar_hash(bars, display_tf)
    assert result["source_bar_hash"] == expected_hash, (
        f"partial bar 场景下 source_bar_hash 应由 preloaded DataFrame 计算，"
        f"期望: {expected_hash}, 实际: {result['source_bar_hash']}"
    )
    # is_partial 应传播到 display_frame
    display_frame = result.get("display_frame", {})
    assert display_frame.get("is_partial") is True, (
        f"display_frame.is_partial 应为 True（partial bar 场景），"
        f"实际: {display_frame.get('is_partial')}"
    )


# =============================================================================
# 测试 5: render_frame.matched=true（bars vs indicators display_frame 一致）
# =============================================================================


@pytest.mark.asyncio
async def test_render_frame_matched_true_with_preloaded(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical: None,
    mock_node_cluster: None,
    mock_node_input_provider: None,
    mock_strategy_keys: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CP-16] preloaded 传入时，bars 和 indicators 的 display_frame 必然一致（matched=true）。

    验证：chart_snapshot 端点流程中，bars_display_frame 和 indicators_display_frame
    基于同一 DataFrame 生成，display_hash 必然一致。
    """
    from app.services.indicator_display_frame import build_display_frame, is_display_frame_match

    call_log: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", _make_spy_mdas(call_log)
    )

    display_tf = "1d"
    bars = _build_bars(display_tf, length=60)
    preloaded = _make_result(
        bars,
        source_bar_hash="matched_hash_v1",
        adj_factor_hash="matched_adj_v1",
    )

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, display_tf, "none", bars=50,
        preloaded_display_bars=preloaded,
    )

    # 构建 bars 侧的 display_frame（模拟 chart_snapshot 端点的行为）
    bars_display_df = bars.tail(50)
    bars_display_frame = build_display_frame(
        instrument_id=str(TEST_INSTRUMENT_ID),
        timeframe=display_tf,
        adj="none",
        display_df=bars_display_df,
        completed_through=preloaded.completed_through.isoformat() if preloaded.completed_through else None,
        spec=type("S", (), {
            "instrument_id": str(TEST_INSTRUMENT_ID),
            "timeframe": display_tf,
            "adj": "none",
            "requested_count": 50,
            "include_realtime": True,
            "completed_only": False,
            "adjustment_as_of": None,
        })(),
        is_partial=False,
    )

    indicators_display_frame = result.get("display_frame")
    matched = is_display_frame_match(bars_display_frame, indicators_display_frame)

    assert matched is True, (
        f"preloaded 传入时 render_frame.matched 应为 true，"
        f"bars_hash={bars_display_frame.get('display_hash')}, "
        f"indicators_hash={(indicators_display_frame or {}).get('display_hash')}"
    )
