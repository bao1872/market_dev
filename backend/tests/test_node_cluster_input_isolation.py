"""[CP-V3-A] Node Cluster 输入合同隔离测试。

验证 NodeClusterInputProvider 是四链唯一 Node 输入入口：
1. Provider 固定使用 completed qfq（include_realtime=False, completed_only=True），
   与页面参数结构隔离。
2. daily limit = DAILY_HISTORY_BARS = 250（合同常量，非页面 bars 参数）。
3. 15m limit = NODE_CLUSTER_LOW_BARS = 4000（合同常量，非页面 bars 参数）。
4. Node 无条件加载完整 250+4000（不再支持 load_15m=False / needs_15min 控制）。
5. availability 三态状态机：
   - 250+4000: available
   - <4000 且 history_exhausted=True: degraded / INSUFFICIENT_15M_HISTORY
   - <4000 且 history_exhausted=False: unavailable / INPUT_CONTRACT_VIOLATION
   - daily<10: unavailable / INSUFFICIENT_DAILY_BARS
   - m15==0: unavailable / MISSING_15M_BARS
6. 60/90/120 不变性：Provider 签名不含 bars/display_count/defaultVisibleBars/
   页面 timeframe/indicator_view/released strategy keys（结构隔离）。
7. compute_all_indicators 必须通过 NodeClusterInputProvider.get_inputs 加载 Node 输入。
8. _compute_independent_node_cluster 第一参数必须是 node_input（NodeClusterInput 对象）。

运行方式：
- pytest（需要 APP_ENV=test + TEST_DATABASE_URL）:
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_node_cluster_input_isolation.py
- standalone（不需要 DB，用于 Phase 1 V3 验证）:
    cd backend && python -m tests.test_node_cluster_input_isolation
"""
from __future__ import annotations

import ast
import inspect
import uuid
from contextlib import contextmanager
from dataclasses import replace as _replace
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.constants.indicator_contract import (
    DAILY_HISTORY_BARS,
    INDICATOR_BARS,
    NODE_CLUSTER_LOW_BARS,
)
from app.services.business_date_adjustment_context import (
    BusinessDateAdjustmentContext,
    BusinessDateAdjustmentService,
    BusinessDateAdjustmentUnavailableError,
)
from app.services.node_cluster_input_provider import (
    NodeAdjustmentContextMismatchError,
    NodeClusterInput,
    NodeClusterInputProvider,
)

# ============================================================
# 测试辅助
# ============================================================


def _make_bars(n: int, freq: str = "D") -> pd.DataFrame:
    """构造 n 根测试 bars（DatetimeIndex + OHLCV）。"""
    idx = pd.date_range("2024-01-01", periods=n, freq=freq)
    return pd.DataFrame(
        {
            "open": range(n),
            "high": [i + 1 for i in range(n)],
            "low": [i - 1 for i in range(n)],
            "close": [i + 0.5 for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        },
        index=idx,
    )


def _make_agg_result(
    bars: pd.DataFrame,
    *,
    history_exhausted: bool = False,
    source_bar_hash: str = "src-hash-001",
    adj_factor_hash: str = "adj-hash-001",
) -> MagicMock:
    """构造 MDAS BarAggregationResult mock（含 [CP-V3-A] 新增字段）。"""
    result = MagicMock()
    result.bars = bars
    result.history_exhausted = history_exhausted
    result.source_bar_hash = source_bar_hash
    result.adj_factor_hash = adj_factor_hash
    return result


# ============================================================
# 1. Provider 合同验证（daily / 15m 查询参数）
# ============================================================


@pytest.mark.asyncio
async def test_provider_uses_completed_qfq_for_daily() -> None:
    """[NC-01] daily 查询必须使用 include_realtime=False, completed_only=True, adj=qfq。"""
    daily_bars_df = _make_bars(250)
    expected_15m_df = _make_bars(4000, freq="15min")

    with patch(
        "app.services.node_cluster_input_provider.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(daily_bars_df, source_bar_hash="d1", adj_factor_hash="a1"),
                _make_agg_result(expected_15m_df, source_bar_hash="m1", adj_factor_hash="a2"),
            ]
        )

        node_input = await NodeClusterInputProvider.get_inputs(
            MagicMock(), MagicMock(),
        )

    assert isinstance(node_input, NodeClusterInput)

    # 验证 daily 查询参数
    daily_call = mock_mdas.get_bars.call_args_list[0]
    assert daily_call.kwargs["timeframe"] == "1d"
    assert daily_call.kwargs["adj"] == "qfq"
    assert daily_call.kwargs["include_realtime"] is False, (
        "NC-01 违规：Node daily 输入必须 include_realtime=False"
    )
    assert daily_call.kwargs["completed_only"] is True, (
        "NC-01 违规：Node daily 输入必须 completed_only=True"
    )
    assert daily_call.kwargs["limit"] == DAILY_HISTORY_BARS, (
        f"NC-03 违规：Node daily limit 必须是 DAILY_HISTORY_BARS={DAILY_HISTORY_BARS}"
    )


@pytest.mark.asyncio
async def test_provider_uses_completed_qfq_for_15m() -> None:
    """[NC-02] 15m 查询必须使用 include_realtime=False, completed_only=True, adj=qfq。"""
    daily_bars_df = _make_bars(250)
    expected_15m_df = _make_bars(4000, freq="15min")

    with patch(
        "app.services.node_cluster_input_provider.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(daily_bars_df, source_bar_hash="d1", adj_factor_hash="a1"),
                _make_agg_result(expected_15m_df, source_bar_hash="m1", adj_factor_hash="a2"),
            ]
        )

        await NodeClusterInputProvider.get_inputs(
            MagicMock(), MagicMock(),
        )

    # 验证 15m 查询参数
    call_15m = mock_mdas.get_bars.call_args_list[1]
    assert call_15m.kwargs["timeframe"] == "15m"
    assert call_15m.kwargs["adj"] == "qfq"
    assert call_15m.kwargs["include_realtime"] is False, (
        "NC-02 违规：Node 15m 输入必须 include_realtime=False"
    )
    assert call_15m.kwargs["completed_only"] is True, (
        "NC-02 违规：Node 15m 输入必须 completed_only=True"
    )
    assert call_15m.kwargs["limit"] == NODE_CLUSTER_LOW_BARS, (
        f"NC-03 违规：Node 15m limit 必须是 NODE_CLUSTER_LOW_BARS={NODE_CLUSTER_LOW_BARS}"
    )
    # 必须始终调用 2 次（daily + 15m），不允许跳过 15m
    assert mock_mdas.get_bars.call_count == 2, (
        "Node 无条件加载 250+4000，必须始终查询 daily + 15m"
    )


@pytest.mark.asyncio
async def test_provider_passes_adjustment_as_of() -> None:
    """adjustment_as_of 必须透传到 MDAS（保证四链 hash 一致）。"""
    adj_anchor = date(2024, 6, 30)

    with patch(
        "app.services.node_cluster_input_provider.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(_make_bars(250)),
                _make_agg_result(_make_bars(4000, freq="15min")),
            ]
        )

        node_input = await NodeClusterInputProvider.get_inputs(
            MagicMock(), MagicMock(),
            adjustment_as_of=adj_anchor,
        )

    # daily 和 15m 查询都应透传 adjustment_as_of
    for call in mock_mdas.get_bars.call_args_list:
        assert call.kwargs["adjustment_as_of"] == adj_anchor, (
            "adjustment_as_of 必须透传到 MDAS（display 与 Node 共用同一锚点）"
        )
    assert node_input.adjustment_as_of == adj_anchor


@pytest.mark.asyncio
async def test_provider_passes_end_date() -> None:
    """[CP-V3-A] end_date 必须透传到 MDAS（Feature Snapshot point-in-time 行情截止）。"""
    end_anchor = date(2024, 6, 30)

    with patch(
        "app.services.node_cluster_input_provider.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(_make_bars(250)),
                _make_agg_result(_make_bars(4000, freq="15min")),
            ]
        )

        await NodeClusterInputProvider.get_inputs(
            MagicMock(), MagicMock(),
            end_date=end_anchor,
        )

    for call in mock_mdas.get_bars.call_args_list:
        assert call.kwargs["end_date"] == end_anchor, (
            "end_date 必须透传到 MDAS（Feature Snapshot point-in-time 行情截止日）"
        )


@pytest.mark.asyncio
async def test_provider_returns_full_diagnostic_fields() -> None:
    """[CP-V3-A] NodeClusterInput 必须包含 requested/count/hash/history/availability 字段。"""
    with patch(
        "app.services.node_cluster_input_provider.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(
                    _make_bars(250),
                    history_exhausted=False,
                    source_bar_hash="daily-hash",
                    adj_factor_hash="daily-adj",
                ),
                _make_agg_result(
                    _make_bars(4000, freq="15min"),
                    history_exhausted=False,
                    source_bar_hash="m15-hash",
                    adj_factor_hash="m15-adj",
                ),
            ]
        )

        node_input = await NodeClusterInputProvider.get_inputs(
            MagicMock(), MagicMock(),
        )

    # count 字段
    assert node_input.daily_count == 250
    assert node_input.m15_count == 4000
    assert node_input.daily_requested == DAILY_HISTORY_BARS
    assert node_input.m15_requested == NODE_CLUSTER_LOW_BARS
    # hash 字段
    assert node_input.daily_source_hash == "daily-hash"
    assert node_input.m15_source_hash == "m15-hash"
    assert node_input.daily_adj_factor_hash == "daily-adj"
    assert node_input.m15_adj_factor_hash == "m15-adj"
    # history_exhausted
    assert node_input.daily_history_exhausted is False
    assert node_input.m15_history_exhausted is False
    # availability 状态机
    assert node_input.availability == "available"
    assert node_input.degraded_reason is None


# ============================================================
# 2. availability 三态状态机
# ============================================================


def test_availability_state_machine_available() -> None:
    """[CP-V3-A] 250+4000 → available。"""
    avail, reason = NodeClusterInputProvider._compute_availability(
        daily_count=250, m15_count=4000,
        daily_history_exhausted=False, m15_history_exhausted=False,
    )
    assert avail == "available"
    assert reason is None


def test_availability_state_machine_degraded_insufficient_15m_history() -> None:
    """[CP-V3-A] m15<4000 且 history_exhausted=True → degraded/INSUFFICIENT_15M_HISTORY。"""
    avail, reason = NodeClusterInputProvider._compute_availability(
        daily_count=250, m15_count=144,
        daily_history_exhausted=False, m15_history_exhausted=True,
    )
    assert avail == "degraded"
    assert reason == "INSUFFICIENT_15M_HISTORY"


def test_availability_state_machine_unavailable_input_contract_violation() -> None:
    """[CP-V3-A] m15<4000 且 history_exhausted=False → unavailable/INPUT_CONTRACT_VIOLATION。

    场景：DB 实际有 8160 根 15m bar，但 MDAS 仅返回 1872（系统未取满）。
    必须禁止生成看似正常的 Profile。
    """
    avail, reason = NodeClusterInputProvider._compute_availability(
        daily_count=250, m15_count=1872,
        daily_history_exhausted=False, m15_history_exhausted=False,
    )
    assert avail == "unavailable"
    assert reason == "INPUT_CONTRACT_VIOLATION"


def test_availability_state_machine_unavailable_insufficient_daily_bars() -> None:
    """[CP-V3-A] daily<10 → unavailable/INSUFFICIENT_DAILY_BARS。"""
    avail, reason = NodeClusterInputProvider._compute_availability(
        daily_count=9, m15_count=4000,
        daily_history_exhausted=True, m15_history_exhausted=False,
    )
    assert avail == "unavailable"
    assert reason == "INSUFFICIENT_DAILY_BARS"


def test_availability_state_machine_unavailable_missing_15m_bars() -> None:
    """[CP-V3-A] m15==0 → unavailable/MISSING_15M_BARS。"""
    avail, reason = NodeClusterInputProvider._compute_availability(
        daily_count=250, m15_count=0,
        daily_history_exhausted=False, m15_history_exhausted=True,
    )
    assert avail == "unavailable"
    assert reason == "MISSING_15M_BARS"


def test_availability_state_machine_priority_over_history_exhausted() -> None:
    """[CP-V3-A] daily<10 优先级最高，即使 history_exhausted=True 仍 unavailable。"""
    avail, reason = NodeClusterInputProvider._compute_availability(
        daily_count=5, m15_count=0,
        daily_history_exhausted=True, m15_history_exhausted=True,
    )
    assert avail == "unavailable"
    assert reason == "INSUFFICIENT_DAILY_BARS"


# ============================================================
# 3. 60/90/120 不变性验证
# ============================================================


def test_provider_signature_excludes_display_params() -> None:
    """[NC-03] Provider.get_inputs 签名不得包含展示参数。

    这是 60/90/120 不变性的根本保证：页面 bars/display_count/defaultVisibleBars/
    页面 timeframe/indicator_view/released strategy keys 无法进入 Node Cluster 输入。
    """
    sig = inspect.signature(NodeClusterInputProvider.get_inputs)
    forbidden = {
        "bars", "display_count", "defaultVisibleBars",
        "timeframe", "indicator_view", "strategy_keys",
    }
    for param in forbidden:
        assert param not in sig.parameters, (
            f"NC-03 违规：Provider.get_inputs 签名不得包含 {param}，"
            "否则展示需求会污染 Node Cluster 计算"
        )
    # 确认合同参数存在
    assert "adjustment_as_of" in sig.parameters, "adjustment_as_of 是复权锚点"
    assert "end_date" in sig.parameters, "end_date 是 point-in-time 行情截止日"


def test_indicator_bars_1d_equals_daily_history_bars() -> None:
    """[NC-03] INDICATOR_BARS['1d'] 必须 == DAILY_HISTORY_BARS（合同常量对齐）。"""
    assert INDICATOR_BARS["1d"] == DAILY_HISTORY_BARS == 250


def test_indicator_bars_15m_equals_node_cluster_low_bars() -> None:
    """[NC-03] INDICATOR_BARS['15m'] 必须 == NODE_CLUSTER_LOW_BARS（合同常量对齐）。"""
    assert INDICATOR_BARS["15m"] == NODE_CLUSTER_LOW_BARS == 4000


def test_node_cluster_constants_not_polluted_by_display_window() -> None:
    """[NC-03] 60/90/120 显示窗口不得进入 Node Cluster 合同常量。

    90 是前端 defaultVisibleBars（飞书舞台），不传后端 API；
    250 是 DAILY_HISTORY_BARS（Node daily 输入）；
    4000 是 NODE_CLUSTER_LOW_BARS（Node 15m 输入）。
    90/120 不得出现在 Node Cluster 输入合同中。
    """
    assert DAILY_HISTORY_BARS not in (60, 90, 120), (
        "DAILY_HISTORY_BARS 不得是显示窗口值 60/90/120"
    )
    assert NODE_CLUSTER_LOW_BARS not in (60, 90, 120), (
        "NODE_CLUSTER_LOW_BARS 不得是显示窗口值 60/90/120"
    )
    # 合同值固定
    assert DAILY_HISTORY_BARS == 250
    assert NODE_CLUSTER_LOW_BARS == 4000


# ============================================================
# 4. 静态合同：compute_all_indicators 必须通过 NodeClusterInputProvider
# ============================================================


def test_compute_all_indicators_calls_provider_get_inputs() -> None:
    """[CP-V3-A] compute_all_indicators 必须通过 NodeClusterInputProvider.get_inputs 加载 Node 输入。

    通过 AST 静态分析验证，确保不被误改回直接 MDAS 查询或旧的 _load_node_cluster_inputs。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod.compute_all_indicators)
    tree = ast.parse(source)

    # 收集所有函数调用（Name + Attribute）
    call_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.append(node.func.attr)

    assert "get_inputs" in call_names, (
        "compute_all_indicators 必须调用 NodeClusterInputProvider.get_inputs 加载 Node 输入"
    )


def test_compute_all_indicators_does_not_call_legacy_load_node_cluster_inputs() -> None:
    """[CP-V3-A] compute_all_indicators 不得调用已废弃的 _load_node_cluster_inputs。

    旧路径必须被完全替换为 NodeClusterInputProvider.get_inputs。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod.compute_all_indicators)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "_load_node_cluster_inputs", (
                "compute_all_indicators 不得调用已废弃的 _load_node_cluster_inputs，"
                "必须改为 NodeClusterInputProvider.get_inputs"
            )


def test_compute_all_indicators_node_cluster_uses_node_input() -> None:
    """[CP-V3-A] _compute_independent_node_cluster 第一参数必须是 node_input。

    通过 AST 静态分析验证 Node Cluster 输入隔离：禁止直接传 daily_bars/bars_15min
    等拆分字段，必须传 NodeClusterInput 对象。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod.compute_all_indicators)
    tree = ast.parse(source)

    found_node_cluster_call = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_compute_independent_node_cluster":
                found_node_cluster_call = True
                assert len(node.args) >= 1, (
                    "_compute_independent_node_cluster 必须有第一个位置参数"
                )
                first_arg = node.args[0]
                assert isinstance(first_arg, ast.Name) and first_arg.id == "node_input", (
                    "CP-V3-A 违规：_compute_independent_node_cluster 第一个参数必须是 node_input"
                    "（NodeClusterInput 对象），不是 daily_bars 或其他拆分字段"
                )

    assert found_node_cluster_call, (
        "compute_all_indicators 必须调用 _compute_independent_node_cluster"
    )


def test_compute_independent_node_cluster_rejects_unavailable() -> None:
    """[CP-V3-A] _compute_independent_node_cluster 必须在 availability==unavailable 时提前返回。

    通过源码扫描验证：unavailable 状态下禁止生成 Profile（INPUT_CONTRACT_VIOLATION 等）。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod._compute_independent_node_cluster)
    # 必须包含 availability 门禁
    assert "unavailable" in source, (
        "_compute_independent_node_cluster 必须检查 availability==unavailable"
    )
    assert "INPUT_CONTRACT_VIOLATION" in source or "degraded_reason" in source, (
        "_compute_independent_node_cluster 必须保留 degraded_reason 透传"
    )


def test_compute_all_indicators_no_direct_15m_query_with_realtime() -> None:
    """[NC-02] compute_all_indicators 不得直接查询 15m with include_realtime=True for Node。

    通过源码字符串扫描验证：Node 15m bars 必须来自 Provider（completed qfq），
    不得来自 include_realtime=True 的直接 MDAS 查询。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod.compute_all_indicators)
    # 旧的违规模式：直接 MDAS get_bars with include_realtime=True, limit=NODE_CLUSTER_LOW_BARS
    assert "include_realtime=True, limit=NODE_CLUSTER_LOW_BARS" not in source, (
        "NC-02 违规：compute_all_indicators 不得直接查询 15m with include_realtime=True"
    )


# ============================================================
# 5. to_dict 序列化（Monitor payload 用）
# ============================================================


def test_provider_to_dict_includes_four_chain_comparison_fields() -> None:
    """[CP-V3-A] to_dict 必须包含四链可比较的 hash/count/availability 字段。"""
    # 构造一个 NodeClusterInput 实例
    node_input = NodeClusterInput(
        daily_bars=_make_bars(250),
        bars_15m=_make_bars(4000, freq="15min"),
        daily_source_hash="dh",
        daily_adj_factor_hash="da",
        m15_source_hash="mh",
        m15_adj_factor_hash="ma",
        daily_count=250,
        m15_count=4000,
        daily_requested=250,
        m15_requested=4000,
        daily_history_exhausted=False,
        m15_history_exhausted=False,
        availability="available",
        degraded_reason=None,
        adjustment_as_of=date(2024, 6, 30),
    )
    d = NodeClusterInputProvider.to_dict(node_input)

    # 四链必须可比字段
    required_fields = {
        "daily_bars_count", "bars_15m_count",
        "daily_requested_count", "bars_15m_requested_count",
        "daily_source_hash", "bars_15m_source_hash",
        "daily_adj_factor_hash", "bars_15m_adj_factor_hash",
        "daily_history_exhausted", "bars_15m_history_exhausted",
        "availability", "degraded_reason", "adjustment_as_of",
    }
    assert required_fields.issubset(d.keys()), (
        f"to_dict 缺少字段: {required_fields - set(d.keys())}"
    )
    assert d["availability"] == "available"
    assert d["daily_bars_count"] == 250
    assert d["bars_15m_count"] == 4000
    assert d["daily_source_hash"] == "dh"
    assert d["adjustment_as_of"] == "2024-06-30"


# ============================================================
# standalone 运行入口（不需要 pytest/DB，用于 Phase 1 V3 验证）
# ============================================================


def _run_standalone_tests() -> int:
    """不依赖 pytest 的 standalone 测试运行器。

    Returns:
        失败的测试数量（0 = 全部通过）
    """
    import asyncio

    failures: list[str] = []

    async def run_async_tests() -> None:
        # 1. test_provider_uses_completed_qfq_for_daily
        try:
            daily_bars_df = _make_bars(250)
            expected_15m_df = _make_bars(4000, freq="15min")
            with patch(
                "app.services.node_cluster_input_provider.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(daily_bars_df),
                        _make_agg_result(expected_15m_df),
                    ]
                )
                await NodeClusterInputProvider.get_inputs(MagicMock(), MagicMock())
            daily_call = mock_mdas.get_bars.call_args_list[0]
            assert daily_call.kwargs["include_realtime"] is False
            assert daily_call.kwargs["completed_only"] is True
            assert daily_call.kwargs["limit"] == DAILY_HISTORY_BARS
            print("  ✓ test_provider_uses_completed_qfq_for_daily")
        except AssertionError as e:
            failures.append(f"daily qfq: {e}")

        # 2. test_provider_uses_completed_qfq_for_15m
        try:
            daily_bars_df = _make_bars(250)
            expected_15m_df = _make_bars(4000, freq="15min")
            with patch(
                "app.services.node_cluster_input_provider.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(daily_bars_df),
                        _make_agg_result(expected_15m_df),
                    ]
                )
                await NodeClusterInputProvider.get_inputs(MagicMock(), MagicMock())
            call_15m = mock_mdas.get_bars.call_args_list[1]
            assert call_15m.kwargs["include_realtime"] is False
            assert call_15m.kwargs["completed_only"] is True
            assert call_15m.kwargs["limit"] == NODE_CLUSTER_LOW_BARS
            print("  ✓ test_provider_uses_completed_qfq_for_15m")
        except AssertionError as e:
            failures.append(f"15m qfq: {e}")

        # 3. test_provider_passes_adjustment_as_of
        try:
            adj_anchor = date(2024, 6, 30)
            with patch(
                "app.services.node_cluster_input_provider.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(_make_bars(250)),
                        _make_agg_result(_make_bars(4000, freq="15min")),
                    ]
                )
                await NodeClusterInputProvider.get_inputs(
                    MagicMock(), MagicMock(),
                    adjustment_as_of=adj_anchor,
                )
            for call in mock_mdas.get_bars.call_args_list:
                assert call.kwargs["adjustment_as_of"] == adj_anchor
            print("  ✓ test_provider_passes_adjustment_as_of")
        except AssertionError as e:
            failures.append(f"adjustment_as_of: {e}")

        # 4. test_provider_passes_end_date
        try:
            end_anchor = date(2024, 6, 30)
            with patch(
                "app.services.node_cluster_input_provider.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(_make_bars(250)),
                        _make_agg_result(_make_bars(4000, freq="15min")),
                    ]
                )
                await NodeClusterInputProvider.get_inputs(
                    MagicMock(), MagicMock(),
                    end_date=end_anchor,
                )
            for call in mock_mdas.get_bars.call_args_list:
                assert call.kwargs["end_date"] == end_anchor
            print("  ✓ test_provider_passes_end_date")
        except AssertionError as e:
            failures.append(f"end_date: {e}")

        # 5. test_provider_returns_full_diagnostic_fields
        try:
            with patch(
                "app.services.node_cluster_input_provider.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(
                            _make_bars(250),
                            history_exhausted=False,
                            source_bar_hash="daily-hash",
                            adj_factor_hash="daily-adj",
                        ),
                        _make_agg_result(
                            _make_bars(4000, freq="15min"),
                            history_exhausted=False,
                            source_bar_hash="m15-hash",
                            adj_factor_hash="m15-adj",
                        ),
                    ]
                )
                node_input = await NodeClusterInputProvider.get_inputs(
                    MagicMock(), MagicMock(),
                )
            assert node_input.daily_count == 250
            assert node_input.m15_count == 4000
            assert node_input.daily_source_hash == "daily-hash"
            assert node_input.m15_source_hash == "m15-hash"
            assert node_input.availability == "available"
            print("  ✓ test_provider_returns_full_diagnostic_fields")
        except AssertionError as e:
            failures.append(f"diagnostic fields: {e}")

    asyncio.run(run_async_tests())

    # 6. availability 状态机（同步）
    avail_cases = [
        ("available", 250, 4000, False, False, "available", None),
        ("degraded", 250, 144, False, True, "degraded", "INSUFFICIENT_15M_HISTORY"),
        ("contract_violation", 250, 1872, False, False, "unavailable", "INPUT_CONTRACT_VIOLATION"),
        ("insufficient_daily", 9, 4000, True, False, "unavailable", "INSUFFICIENT_DAILY_BARS"),
        ("missing_15m", 250, 0, False, True, "unavailable", "MISSING_15M_BARS"),
    ]
    for name, dc, mc, dhe, mhe, exp_avail, exp_reason in avail_cases:
        try:
            avail, reason = NodeClusterInputProvider._compute_availability(
                daily_count=dc, m15_count=mc,
                daily_history_exhausted=dhe, m15_history_exhausted=mhe,
            )
            assert avail == exp_avail, f"{name}: expected {exp_avail}, got {avail}"
            assert reason == exp_reason, f"{name}: expected {exp_reason}, got {reason}"
            print(f"  ✓ test_availability_state_machine_{name}")
        except AssertionError as e:
            failures.append(f"availability {name}: {e}")

    # 7. 静态合同测试（同步）
    try:
        sig = inspect.signature(NodeClusterInputProvider.get_inputs)
        forbidden = {
            "bars", "display_count", "defaultVisibleBars",
            "timeframe", "indicator_view", "strategy_keys",
        }
        for param in forbidden:
            assert param not in sig.parameters
        assert "adjustment_as_of" in sig.parameters
        assert "end_date" in sig.parameters
        print("  ✓ test_provider_signature_excludes_display_params")
    except AssertionError as e:
        failures.append(f"signature: {e}")

    try:
        assert INDICATOR_BARS["1d"] == DAILY_HISTORY_BARS == 250
        print("  ✓ test_indicator_bars_1d_equals_daily_history_bars")
    except AssertionError as e:
        failures.append(f"INDICATOR_BARS[1d]: {e}")

    try:
        assert INDICATOR_BARS["15m"] == NODE_CLUSTER_LOW_BARS == 4000
        print("  ✓ test_indicator_bars_15m_equals_node_cluster_low_bars")
    except AssertionError as e:
        failures.append(f"INDICATOR_BARS[15m]: {e}")

    try:
        assert DAILY_HISTORY_BARS not in (60, 90, 120)
        assert NODE_CLUSTER_LOW_BARS not in (60, 90, 120)
        assert DAILY_HISTORY_BARS == 250
        assert NODE_CLUSTER_LOW_BARS == 4000
        print("  ✓ test_node_cluster_constants_not_polluted_by_display_window")
    except AssertionError as e:
        failures.append(f"constants pollution: {e}")

    # 8. AST 静态合同测试
    try:
        test_compute_all_indicators_calls_provider_get_inputs()
        print("  ✓ test_compute_all_indicators_calls_provider_get_inputs")
    except AssertionError as e:
        failures.append(f"AST get_inputs: {e}")

    try:
        test_compute_all_indicators_does_not_call_legacy_load_node_cluster_inputs()
        print("  ✓ test_compute_all_indicators_does_not_call_legacy_load_node_cluster_inputs")
    except AssertionError as e:
        failures.append(f"AST no legacy: {e}")

    try:
        test_compute_all_indicators_node_cluster_uses_node_input()
        print("  ✓ test_compute_all_indicators_node_cluster_uses_node_input")
    except AssertionError as e:
        failures.append(f"AST node_input: {e}")

    try:
        test_compute_independent_node_cluster_rejects_unavailable()
        print("  ✓ test_compute_independent_node_cluster_rejects_unavailable")
    except AssertionError as e:
        failures.append(f"AST unavailable reject: {e}")

    try:
        test_compute_all_indicators_no_direct_15m_query_with_realtime()
        print("  ✓ test_compute_all_indicators_no_direct_15m_query_with_realtime")
    except AssertionError as e:
        failures.append(f"AST no direct 15m: {e}")

    # 9. to_dict 测试
    try:
        test_provider_to_dict_includes_four_chain_comparison_fields()
        print("  ✓ test_provider_to_dict_includes_four_chain_comparison_fields")
    except AssertionError as e:
        failures.append(f"to_dict: {e}")

    return len(failures)


# =============================================================================
# C1：NodeClusterInputProvider + BusinessDateAdjustmentContext 绑定
# =============================================================================


# ---- C1 测试辅助 ----


def _wire_raw(monkeypatch, raw: pd.DataFrame) -> None:
    """monkeypatch repository 的 raw daily 读取（不连 DB）。"""
    monkeypatch.setattr(
        "app.services.business_date_adjustment_context.get_raw_daily_close_series",
        AsyncMock(return_value=raw),
    )


class _FakeXdxrAdapter:
    """返回固定 XDXR DataFrame 的假 adapter。"""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def get_xdxr_info(self, symbol: str, **kwargs: object) -> pd.DataFrame:
        return self._df


def _raw_df(pairs: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": [pd.Timestamp(d) for d, _ in pairs],
        "close": [c for _, c in pairs],
    })


def _xdxr_df(events: list[dict]) -> pd.DataFrame:
    rows = []
    for e in events:
        rows.append({
            "date": pd.Timestamp(e["date"]),
            "category": e.get("category", 1),
            "fenhong": e.get("fenhong", 0.0),
            "songzhuangu": e.get("songzhuangu", 0.0),
            "peigu": e.get("peigu", 0.0),
            "peigujia": e.get("peigujia", 0.0),
        })
    return pd.DataFrame(rows, columns=[
        "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
    ])


def _make_bar(dt: str, close: float) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(dt)])
    idx.name = "bar_time"
    return pd.DataFrame(
        {
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [1],
        },
        index=idx,
    )


def _make_named_bars(dates: list[date], close: float = 20.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    idx.name = "bar_time"
    return pd.DataFrame(
        {
            "open": [close] * len(dates),
            "high": [close] * len(dates),
            "low": [close] * len(dates),
            "close": [close] * len(dates),
            "volume": [1] * len(dates),
        },
        index=idx,
    )


async def _build_context(
    monkeypatch,
    *,
    instrument_id: uuid.UUID,
    raw: pd.DataFrame,
    xdxr: pd.DataFrame,
    business_date: date,
    expected_completed_through: date,
    symbol: str = "600519",
) -> BusinessDateAdjustmentContext:
    _wire_raw(monkeypatch, raw)
    adapter = _FakeXdxrAdapter(xdxr)
    return await BusinessDateAdjustmentService().build_business_date_adjustment_context(
        MagicMock(),
        instrument_id=instrument_id,
        symbol=symbol,
        business_date=business_date,
        expected_completed_through=expected_completed_through,
        adapter=adapter,
    )


@contextmanager
def _patch_mdas(
    daily_bars: pd.DataFrame,
    m15_bars: pd.DataFrame,
    *,
    daily_hash: str = "daily-raw-abc",
    m15_hash: str = "m15-raw-def",
    daily_exhausted: bool = False,
    m15_exhausted: bool = False,
):
    daily_agg = MagicMock()
    daily_agg.bars = daily_bars
    daily_agg.source_bar_hash = daily_hash
    daily_agg.history_exhausted = daily_exhausted
    daily_agg.adj_factor_hash = ""
    m15_agg = MagicMock()
    m15_agg.bars = m15_bars
    m15_agg.source_bar_hash = m15_hash
    m15_agg.history_exhausted = m15_exhausted
    m15_agg.adj_factor_hash = ""

    with patch(
        "app.services.node_cluster_input_provider.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mdas = mock_mdas_cls.return_value
        mdas.get_bars = AsyncMock(side_effect=[daily_agg, m15_agg])
        yield mdas


# ---- C1 测试 ----


@pytest.mark.asyncio
async def test_legacy_mode_adj_qfq_and_context_hash_none() -> None:
    """Legacy mode（adjustment_context=None）行为必须与历史完全一致。"""
    daily_df = _make_bars(250)
    m15_df = _make_bars(4000, freq="15min")

    with patch(
        "app.services.node_cluster_input_provider.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(daily_df, source_bar_hash="d1", adj_factor_hash="a1"),
                _make_agg_result(m15_df, source_bar_hash="m1", adj_factor_hash="a2"),
            ]
        )
        result = await NodeClusterInputProvider.get_inputs(
            MagicMock(), uuid.UUID(int=1),
        )

    daily_call = mock_mdas.get_bars.call_args_list[0]
    m15_call = mock_mdas.get_bars.call_args_list[1]
    assert daily_call.kwargs["adj"] == "qfq"
    assert m15_call.kwargs["adj"] == "qfq"
    assert daily_call.kwargs["completed_only"] is True
    assert daily_call.kwargs["include_realtime"] is False
    assert daily_call.kwargs["limit"] == 250
    assert m15_call.kwargs["limit"] == 4000
    # Legacy 回显 MDAS adj_factor_hash
    assert result.daily_adj_factor_hash == "a1"
    assert result.m15_adj_factor_hash == "a2"
    assert result.adjustment_context_hash is None


@pytest.mark.asyncio
async def test_context_mode_mdas_uses_adj_none_and_source_hash_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context mode：MDAS 必须 adj='none'，且 source hash 原样透传（不重算）。"""
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    daily_bars = _make_bar("2026-09-11", 20.0)
    m15_bars = pd.concat([
        _make_bar("2026-09-11", 20.0),
        _make_bar("2026-09-12", 10.0),
    ])

    with _patch_mdas(daily_bars, m15_bars) as mdas:
        result = await NodeClusterInputProvider.get_inputs(
            MagicMock(), uuid.UUID(int=1),
            adjustment_context=ctx,
        )

    daily_call = mdas.get_bars.call_args_list[0]
    m15_call = mdas.get_bars.call_args_list[1]
    assert daily_call.kwargs["adj"] == "none"
    assert m15_call.kwargs["adj"] == "none"
    # Context mode 不把 factor 锚点传给 MDAS raw 请求
    assert "adjustment_as_of" not in daily_call.kwargs
    assert "adjustment_as_of" not in m15_call.kwargs
    # source hash 原样透传
    assert result.daily_source_hash == "daily-raw-abc"
    assert result.m15_source_hash == "m15-raw-def"


@pytest.mark.asyncio
async def test_context_mode_10for10_same_coordinate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10送10：历史 daily / 历史 15m / 当日 completed 15m 都在 business-date 坐标。"""
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    daily_bars = _make_bar("2026-09-11", 20.0)
    m15_bars = pd.concat([
        _make_bar("2026-09-11", 20.0),
        _make_bar("2026-09-12", 10.0),
    ])

    with _patch_mdas(daily_bars, m15_bars):
        result = await NodeClusterInputProvider.get_inputs(
            MagicMock(), uuid.UUID(int=1),
            adjustment_context=ctx,
        )

    assert abs(
        float(result.daily_bars.loc[pd.Timestamp("2026-09-11"), "close"]) - 10.0
    ) < 1e-9
    assert abs(
        float(result.bars_15m.loc[pd.Timestamp("2026-09-11"), "close"]) - 10.0
    ) < 1e-9
    # 当日 completed 15m 在 business-date anchor(=1.0) 坐标
    assert abs(
        float(result.bars_15m.loc[pd.Timestamp("2026-09-12"), "close"]) - 10.0
    ) < 1e-9


@pytest.mark.asyncio
async def test_context_mode_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """factor_hash / context_hash 必须来自 Context；daily/15m 共享同一 factor 坐标。"""
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    daily_bars = _make_bar("2026-09-11", 20.0)
    m15_bars = _make_bar("2026-09-11", 20.0)

    with _patch_mdas(daily_bars, m15_bars):
        result = await NodeClusterInputProvider.get_inputs(
            MagicMock(), uuid.UUID(int=1),
            adjustment_context=ctx,
        )

    assert result.daily_adj_factor_hash == ctx.factor_hash
    assert result.m15_adj_factor_hash == ctx.factor_hash
    assert result.adjustment_context_hash == ctx.context_hash

    payload = NodeClusterInputProvider.to_dict(result)
    assert payload["adjustment_context_hash"] == ctx.context_hash
    assert payload["daily_source_hash"] == "daily-raw-abc"
    assert payload["bars_15m_source_hash"] == "m15-raw-def"


@pytest.mark.asyncio
async def test_context_instrument_mismatch_zero_mdas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )
    bad = _replace(ctx, instrument_id=uuid.uuid4())

    with _patch_mdas(_make_bar("2026-09-11", 20.0), _make_bar("2026-09-11", 20.0)) as mdas:
        with pytest.raises(NodeAdjustmentContextMismatchError) as ei:
            await NodeClusterInputProvider.get_inputs(
                MagicMock(), uuid.UUID(int=1),
                adjustment_context=bad,
            )
    assert ei.value.reason == "instrument_id_mismatch"
    mdas.get_bars.assert_not_called()


@pytest.mark.asyncio
async def test_context_adjustment_as_of_mismatch_zero_mdas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    with _patch_mdas(_make_bar("2026-09-11", 20.0), _make_bar("2026-09-11", 20.0)) as mdas:
        with pytest.raises(NodeAdjustmentContextMismatchError) as ei:
            await NodeClusterInputProvider.get_inputs(
                MagicMock(), uuid.UUID(int=1),
                adjustment_as_of=date(2026, 9, 11),
                adjustment_context=ctx,
            )
    assert ei.value.reason == "adjustment_as_of_mismatch"
    mdas.get_bars.assert_not_called()


@pytest.mark.asyncio
async def test_context_future_end_date_zero_mdas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    with _patch_mdas(_make_bar("2026-09-11", 20.0), _make_bar("2026-09-11", 20.0)) as mdas:
        with pytest.raises(NodeAdjustmentContextMismatchError) as ei:
            await NodeClusterInputProvider.get_inputs(
                MagicMock(), uuid.UUID(int=1),
                end_date=date(2026, 9, 13),
                adjustment_context=ctx,
            )
    assert ei.value.reason == "end_date_after_context_business_date"
    mdas.get_bars.assert_not_called()


@pytest.mark.asyncio
async def test_context_freshness_proven_false_zero_mdas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2B integrity 失败必须 zero-MDAS，且直接传播 BusinessDateAdjustmentUnavailableError。"""
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )
    bad = _replace(ctx, freshness_proven=False)

    with _patch_mdas(_make_bar("2026-09-11", 20.0), _make_bar("2026-09-11", 20.0)) as mdas:
        with pytest.raises(BusinessDateAdjustmentUnavailableError) as ei:
            await NodeClusterInputProvider.get_inputs(
                MagicMock(), uuid.UUID(int=1),
                adjustment_context=bad,
            )
    assert ei.value.reason == "context_freshness_not_proven"
    mdas.get_bars.assert_not_called()


@pytest.mark.asyncio
async def test_context_tampered_factor_df_zero_mdas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """篡改 factor payload 必须 zero-MDAS，绝不 fallback 到 legacy MDAS qfq。"""
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)]),
        xdxr=_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )
    ctx.factor_df.loc[0, "adj_factor"] = 123.0

    with _patch_mdas(_make_bar("2026-09-11", 20.0), _make_bar("2026-09-11", 20.0)) as mdas:
        with pytest.raises(BusinessDateAdjustmentUnavailableError) as ei:
            await NodeClusterInputProvider.get_inputs(
                MagicMock(), uuid.UUID(int=1),
                adjustment_context=ctx,
            )
    assert ei.value.reason == "context_factor_hash_mismatch"
    mdas.get_bars.assert_not_called()


@pytest.mark.asyncio
async def test_context_mode_point_in_time_end_date_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """end_date=None 时 Context mode 必须默认取 Context.business_date（禁止未来泄漏）。"""
    # 构造无公司行为的 context，business_date=2026-08-10
    base_end = date(2026, 8, 9)
    raw_dates = [base_end - timedelta(days=i) for i in range(150)]
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([(d.isoformat(), 20.0) for d in raw_dates]),
        xdxr=_xdxr_df([]),
        business_date=date(2026, 8, 10),
        expected_completed_through=base_end,
    )

    daily_bars = _make_named_bars(raw_dates)
    m15_bars = _make_named_bars(raw_dates[:144])

    with _patch_mdas(daily_bars, m15_bars) as mdas:
        await NodeClusterInputProvider.get_inputs(
            MagicMock(), uuid.UUID(int=1),
            adjustment_context=ctx,
        )
        daily_call = mdas.get_bars.call_args_list[0]
        assert daily_call.kwargs["end_date"] == date(2026, 8, 10)
        assert "adjustment_as_of" not in daily_call.kwargs


@pytest.mark.asyncio
async def test_context_mode_availability_state_machine_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """availability / history_exhausted 状态机在 context mode 下保持原合同。"""
    base_end = date(2026, 9, 11)
    daily_dates = [base_end - timedelta(days=i) for i in range(250)]
    m15_dates = [base_end - timedelta(days=i) for i in range(144)]
    ctx = await _build_context(
        monkeypatch,
        instrument_id=uuid.UUID(int=1),
        raw=_raw_df([(d.isoformat(), 20.0) for d in daily_dates]),
        xdxr=_xdxr_df([]),
        business_date=date(2026, 9, 12),
        expected_completed_through=base_end,
    )

    daily_bars = _make_named_bars(daily_dates)
    m15_bars = _make_named_bars(m15_dates)

    with _patch_mdas(
        daily_bars, m15_bars, m15_exhausted=True
    ) as mdas:
        result = await NodeClusterInputProvider.get_inputs(
            MagicMock(), uuid.UUID(int=1),
            adjustment_context=ctx,
        )
        assert mdas.get_bars.call_args_list[0].kwargs["completed_only"] is True
        assert mdas.get_bars.call_args_list[0].kwargs["include_realtime"] is False

    assert result.daily_count == 250
    assert result.m15_count == 144
    assert result.m15_history_exhausted is True
    assert result.availability == "degraded"
    assert result.degraded_reason == "INSUFFICIENT_15M_HISTORY"


if __name__ == "__main__":
    print("=" * 70)
    print("Node Cluster 输入合同隔离测试 [CP-V3-A]")
    print("=" * 70)
    failed = _run_standalone_tests()
    print("=" * 70)
    if failed == 0:
        print("全部通过 ✓")
        raise SystemExit(0)
    else:
        print(f"失败 {failed} 项")
        raise SystemExit(1)
