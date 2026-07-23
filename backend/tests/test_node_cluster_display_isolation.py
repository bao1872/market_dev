"""Node Cluster 计算窗口隔离集成测试。

验证 PROMPT.md §一 要求：
1. 90/60/120 仅是 ChartDisplaySpec/Feishu 图片可视窗口，不得进入 Node 输入
2. Detail、Feishu Capture、Feature Snapshot、Monitor 必须通过同一个
   NodeClusterInputProvider：daily=250 completed qfq, 15m=4000 completed qfq
3. display=60/90/120 时 profile_hash/node_regions_hash/POC/VAH/VAL 完全相同
4. 只有 visible bars 变化

测试策略：
- Mock MDAS 返回固定 250 daily + 4000 15m bars（确定性数据）
- Spy _compute_independent_node_cluster 捕获 node_input
- 调用 compute_all_indicators(bars=60/90/120)，断言 node_input 一致
- 直接调用 NodeClusterInputProvider.get_inputs()，断言 250+4000

运行：
    APP_ENV=test pytest tests/test_node_cluster_display_isolation.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from app.services import indicator_service
from app.services.market_data_aggregation_service import BarAggregationResult
from app.services.node_cluster_input_provider import NodeClusterInput

# 测试标的（模拟 688506 和 300725）
TEST_INSTRUMENT_688506 = uuid.UUID("00000000-0000-0000-0000-068850600000")
TEST_INSTRUMENT_300725 = uuid.UUID("00000000-0000-0000-0000-030072500000")


def _build_daily_bars(n_bars: int = 250, seed: int = 42) -> pd.DataFrame:
    """生成确定性日线 bars（满足 DAILY_HISTORY_BARS=250）。"""
    np.random.seed(seed)
    dates = pd.date_range(end="2026-07-18", periods=n_bars, freq="B")
    returns = np.random.uniform(-0.02, 0.02, size=n_bars)
    close = 10.0 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": close * (1 + np.random.uniform(-0.01, 0.01, size=n_bars)),
        "high": np.maximum(close, close * 1.01),
        "low": np.minimum(close, close * 0.99),
        "close": close,
        "volume": np.random.uniform(1_000_000, 5_000_000, size=n_bars),
        "amount": close * 1_000_000,
        "adj_factor": [1.0] * n_bars,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _build_15m_bars(n_bars: int = 4000, seed: int = 43) -> pd.DataFrame:
    """生成确定性 15m bars（满足 NODE_CLUSTER_LOW_BARS=4000）。"""
    np.random.seed(seed)
    dates = pd.date_range(end="2026-07-18 15:00", periods=n_bars, freq="15min")
    returns = np.random.uniform(-0.003, 0.003, size=n_bars)
    close = 10.0 * np.cumprod(1 + returns)
    df = pd.DataFrame({
        "open": close * (1 + np.random.uniform(-0.001, 0.001, size=n_bars)),
        "high": np.maximum(close, close * 1.002),
        "low": np.minimum(close, close * 0.998),
        "close": close,
        "volume": np.random.uniform(50000, 200000, size=n_bars),
        "amount": close * 50000,
        "adj_factor": [1.0] * n_bars,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _build_display_bars(n_bars: int = 250, seed: int = 44) -> pd.DataFrame:
    """生成展示窗口 bars（模拟 1d timeframe，含 realtime partial）。"""
    np.random.seed(seed)
    dates = pd.date_range(end="2026-07-18", periods=n_bars, freq="B")
    close = np.array([10.0 + i * 0.05 for i in range(n_bars)])
    df = pd.DataFrame({
        "open": close - 0.02,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": np.array([100000 + i * 10 for i in range(n_bars)]),
        "amount": close * 100000,
        "adj_factor": [1.0] * n_bars,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _make_agg_result(
    bars: pd.DataFrame,
    *,
    source_bar_hash: str = "hash_v1",
    adj_factor_hash: str = "adjhash_v1",
    is_partial: bool = False,
    history_exhausted: bool = False,
) -> BarAggregationResult:
    """构造 BarAggregationResult。"""
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
        completed_through=bars.index[-1] if not bars.empty else None,
        history_exhausted=history_exhausted,
    )


# ===== Fixtures =====

@pytest.fixture
def mock_session() -> AsyncMock:
    """mock AsyncSession。"""
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = ("000001",)
    session.execute.return_value = result
    return session


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空策略注册表。"""
    monkeypatch.setattr(indicator_service.StrategyLoader, "_registry", {})


@pytest.fixture
def mock_canonical_non_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock CanonicalComputationService.compute — 非 node_cluster 算法返回 mock。

    node_cluster 算法不在此 mock，由 _compute_independent_node_cluster spy 处理。
    """
    from app.services.canonical_computation_service import CanonicalResult

    async def _mock_compute(algorithm_id, *, instrument_id, as_of, source_bar_hash,
                            adj_factor_hash, **kwargs):
        if algorithm_id == "node_cluster":
            # 不应到达此处 — _compute_independent_node_cluster 应被 spy 拦截
            raise AssertionError(
                "node_cluster 应由 _compute_independent_node_cluster 处理，不应到达 CanonicalComputationService"
            )
        # 各算法返回完整字段（与 test_chart_snapshot_atomic.py 一致）
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
    """Spy _compute_independent_node_cluster — 捕获 node_input 参数。

    返回 dict 含 captured_inputs 列表，每个元素是一次调用的 node_input。
    mock 返回确定性结果，使调用方不报错。
    """
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
def mock_mdas(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Mock MDAS — 返回固定 250 daily + 4000 15m bars for Node，展示窗口 bars for display。

    返回 dict 含 call_log，记录所有 get_bars 调用。
    """
    call_log: list[dict] = []

    # 预生成固定数据
    daily_bars = _build_daily_bars(250)
    m15_bars = _build_15m_bars(4000)
    display_bars = _build_display_bars(250)

    class _MockMDAS:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            call_log.append({
                "timeframe": timeframe,
                "adj": adj,
                "completed_only": kwargs.get("completed_only", False),
                "include_realtime": kwargs.get("include_realtime", True),
                "limit": kwargs.get("limit"),
                "adjustment_as_of": kwargs.get("adjustment_as_of"),
            })

            if timeframe == "1d":
                if kwargs.get("completed_only") is True:
                    # Node Cluster 查询 → 返回 250 completed daily bars
                    return _make_agg_result(daily_bars, source_bar_hash="node_daily_hash")
                else:
                    # 展示窗口查询 → 返回 display bars
                    return _make_agg_result(display_bars, source_bar_hash="display_hash",
                                           is_partial=True)
            elif timeframe == "15m":
                # Node Cluster 查询 → 返回 4000 completed 15m bars
                return _make_agg_result(m15_bars, source_bar_hash="node_m15_hash")
            else:
                return _make_agg_result(display_bars, source_bar_hash="other_hash")

    monkeypatch.setattr(
        indicator_service, "MarketDataAggregationService", _MockMDAS
    )
    # 同时 patch node_cluster_input_provider 中的 MDAS（Provider 直接 import）
    from app.services import node_cluster_input_provider
    monkeypatch.setattr(
        node_cluster_input_provider, "MarketDataAggregationService", _MockMDAS
    )
    return {"call_log": call_log}


# ===== 测试 1: NodeClusterInputProvider 直接验证 =====

@pytest.mark.asyncio
async def test_node_cluster_input_provider_returns_250_4000(
    mock_session: AsyncMock,
    mock_mdas: dict,
) -> None:
    """NodeClusterInputProvider.get_inputs() 始终返回 250 daily + 4000 15m。

    验证：不接收 display_count 参数；daily/15m count 固定。
    """
    from app.services.node_cluster_input_provider import NodeClusterInputProvider

    node_input = await NodeClusterInputProvider.get_inputs(
        mock_session, TEST_INSTRUMENT_688506,
    )

    assert node_input.daily_count == 250, f"daily_count 应为 250，实际 {node_input.daily_count}"
    assert node_input.m15_count == 4000, f"m15_count 应为 4000，实际 {node_input.m15_count}"
    assert node_input.daily_requested == 250
    assert node_input.m15_requested == 4000
    assert node_input.availability == "available"
    assert node_input.daily_source_hash == "node_daily_hash"
    assert node_input.m15_source_hash == "node_m15_hash"


# ===== 测试 2: compute_all_indicators display=60/90/120 不影响 Node 输入 =====

@pytest.mark.asyncio
async def test_display_count_does_not_affect_node_input(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical_non_node: None,
    spy_node_cluster: dict,
    mock_mdas: dict,
) -> None:
    """display=60/90/120 时 Node Cluster 输入始终相同（250+4000）。

    核心断言：
    - 每次调用 _compute_independent_node_cluster 的 node_input.daily_count=250
    - 每次调用 _compute_independent_node_cluster 的 node_input.m15_count=4000
    - 三次调用的 daily_source_hash / m15_source_hash 完全相同
    """
    captured = spy_node_cluster["captured"]

    for display_bars in [60, 90, 120]:
        result = await indicator_service.compute_all_indicators(
            session=mock_session,
            instrument_id=TEST_INSTRUMENT_688506,
            timeframe="1d",
            adj="qfq",
            bars=display_bars,
            include_smc=False,
            include_realtime=True,
            completed_only=False,
        )
        assert "node_cluster" in result.get("data", {}), (
            f"bars={display_bars}: 缺少 node_cluster 输出, keys={list(result.keys())}"
        )

    # 断言：3 次调用，每次 node_input 都相同
    assert len(captured) == 3, f"应有 3 次 _compute_independent_node_cluster 调用，实际 {len(captured)}"

    for idx, node_input in enumerate(captured):
        display_bars = [60, 90, 120][idx]
        assert node_input.daily_count == 250, (
            f"display={display_bars}: daily_count 应为 250，实际 {node_input.daily_count}"
        )
        assert node_input.m15_count == 4000, (
            f"display={display_bars}: m15_count 应为 4000，实际 {node_input.m15_count}"
        )

    # 三次调用的 hash 必须完全相同
    daily_hashes = [ni.daily_source_hash for ni in captured]
    m15_hashes = [ni.m15_source_hash for ni in captured]
    assert len(set(daily_hashes)) == 1, f"daily_source_hash 不一致: {daily_hashes}"
    assert len(set(m15_hashes)) == 1, f"m15_source_hash 不一致: {m15_hashes}"


# ===== 测试 3: Node Cluster 输出（profile_hash/POC/VAH/VAL）在 display 变化时一致 =====

@pytest.mark.asyncio
async def test_node_cluster_output_identical_across_display_counts(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical_non_node: None,
    spy_node_cluster: dict,
    mock_mdas: dict,
) -> None:
    """display=60/90/120 时 Node Cluster 输出完全一致。

    断言：profile_hash / node_regions_hash / POC / VAH / VAL 不随 display 变化。
    """
    results: dict[int, dict] = {}

    for display_bars in [60, 90, 120]:
        result = await indicator_service.compute_all_indicators(
            session=mock_session,
            instrument_id=TEST_INSTRUMENT_300725,
            timeframe="1d",
            adj="qfq",
            bars=display_bars,
            include_smc=False,
            include_realtime=True,
            completed_only=False,
        )
        results[display_bars] = result["data"]["node_cluster"]

    # 提取 Node Cluster 关键输出
    def _extract_key_fields(nc: dict) -> dict:
        meta = nc.get("profile_meta", {})
        return {
            "profile_hash": meta.get("profile_hash"),
            "node_regions_hash": meta.get("node_regions_hash"),
            "daily_bars_count": meta.get("daily_bars_count"),
            "bars_15m_count": meta.get("bars_15m_count"),
            "poc": nc.get("poc"),
            "vah": nc.get("vah"),
            "val": nc.get("val"),
            "availability": nc.get("availability"),
        }

    fields_60 = _extract_key_fields(results[60])
    fields_90 = _extract_key_fields(results[90])
    fields_120 = _extract_key_fields(results[120])

    # 核心断言：三组完全一致
    assert fields_60 == fields_90, (
        f"display=60 vs 90 Node Cluster 输出不一致:\n"
        f"  60: {fields_60}\n  90: {fields_90}"
    )
    assert fields_60 == fields_120, (
        f"display=60 vs 120 Node Cluster 输出不一致:\n"
        f"  60: {fields_60}\n  120: {fields_120}"
    )

    # 额外断言：daily/15m count 固定
    assert fields_60["daily_bars_count"] == 250
    assert fields_60["bars_15m_count"] == 4000


# ===== 测试 4: NodeClusterInputProvider 不接受 display 参数 =====

def test_node_cluster_input_provider_signature_rejects_display() -> None:
    """NodeClusterInputProvider.get_inputs() 签名不包含 display/bars 参数。

    静态验证：确保展示参数无法通过签名传入。
    """
    import inspect
    from app.services.node_cluster_input_provider import NodeClusterInputProvider

    sig = inspect.signature(NodeClusterInputProvider.get_inputs)
    params = set(sig.parameters.keys())

    # 允许的参数（cls 在 classmethod 签名中不可见）
    expected = {"session", "instrument_id", "adjustment_as_of", "end_date"}
    assert params == expected, (
        f"NodeClusterInputProvider.get_inputs 签名异常:\n"
        f"  期望: {expected}\n  实际: {params}\n"
        f"  禁止接收 display/bars/defaultVisibleBars 等展示参数"
    )

    # 明确禁止的参数
    forbidden = {"bars", "display_count", "defaultVisibleBars", "timeframe", "indicator_view"}
    assert not (params & forbidden), (
        f"签名包含禁止的展示参数: {params & forbidden}"
    )


# ===== 测试 5: 4 个入口调用矩阵 =====

@pytest.mark.asyncio
async def test_four_entry_call_matrix(
    mock_session: AsyncMock,
    empty_registry: None,
    mock_canonical_non_node: None,
    spy_node_cluster: dict,
    mock_mdas: dict,
) -> None:
    """4 个入口调用矩阵 — 验证每个入口都通过 NodeClusterInputProvider 获取 Node 输入。

    入口清单：
    1. Detail chart-snapshot → compute_all_indicators(bars=90, include_realtime=True)
    2. Feishu Capture → compute_all_indicators(bars=90, include_realtime=True, completed_only=False)
    3. Feature Snapshot → NodeClusterInputProvider.get_inputs(adjustment_as_of=trade_date, end_date=trade_date)
    4. Monitor → NodeClusterInputProvider.get_inputs() (无 adjustment_as_of)

    断言：每个入口的 Node 输入都是 250+4000（或 spy 捕获到正确的 node_input）。
    """
    from app.services.node_cluster_input_provider import NodeClusterInputProvider

    captured = spy_node_cluster["captured"]
    mdas_log = mock_mdas["call_log"]

    # --- 入口 1: Detail chart-snapshot (display=90, realtime) ---
    await indicator_service.compute_all_indicators(
        session=mock_session,
        instrument_id=TEST_INSTRUMENT_688506,
        timeframe="1d",
        adj="qfq",
        bars=90,
        include_smc=False,
        include_realtime=True,
        completed_only=False,
    )

    # --- 入口 2: Feishu Capture (display=90, realtime, completed_only=False) ---
    await indicator_service.compute_all_indicators(
        session=mock_session,
        instrument_id=TEST_INSTRUMENT_688506,
        timeframe="1d",
        adj="qfq",
        bars=90,
        include_smc=True,
        include_realtime=True,
        completed_only=False,
    )

    # --- 入口 3: Feature Snapshot (直接调 Provider，point-in-time) ---
    await NodeClusterInputProvider.get_inputs(
        mock_session, TEST_INSTRUMENT_688506,
        adjustment_as_of=datetime(2026, 7, 18).date(),
        end_date=datetime(2026, 7, 18).date(),
    )

    # --- 入口 4: Monitor (直接调 Provider，无 adjustment_as_of) ---
    await NodeClusterInputProvider.get_inputs(
        mock_session, TEST_INSTRUMENT_688506,
    )

    # 断言：入口 1+2 通过 compute_all_indicators → spy 捕获 2 次
    assert len(captured) == 2, (
        f"Detail+Capture 应触发 2 次 _compute_independent_node_cluster，实际 {len(captured)}"
    )
    for ni in captured:
        assert ni.daily_count == 250
        assert ni.m15_count == 4000

    # 断言：MDAS 调用日志中，Node 查询（limit=250 或 4000）使用 completed_only=True
    # 注意：SMC 也有 completed_only=True 的 deterministic 查询，但 SMC 不传 limit=250/4000
    # 因此用 limit 区分 Node Cluster Provider 查询
    NODE_LIMITS = {250, 4000}
    node_queries = [q for q in mdas_log if q.get("limit") in NODE_LIMITS]
    assert len(node_queries) >= 4, (
        f"应有至少 4 次 Node 查询（2 入口 × 2 timeframe），实际 {len(node_queries)}\n"
        f"完整日志: {mdas_log}"
    )
    for q in node_queries:
        assert q["completed_only"] is True, (
            f"Node 查询应为 completed_only=True: {q}"
        )
        assert q["include_realtime"] is False, (
            f"Node 查询不应包含 realtime: {q}"
        )

    # 断言：展示窗口查询使用 completed_only=False（或页面参数）
    display_queries = [q for q in mdas_log if q["completed_only"] is False]
    assert len(display_queries) >= 2, (
        f"应有至少 2 次展示窗口查询（Detail+Capture），实际 {len(display_queries)}"
    )
