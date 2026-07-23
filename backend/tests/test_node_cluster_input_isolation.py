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
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.constants.indicator_contract import (
    DAILY_HISTORY_BARS,
    INDICATOR_BARS,
    NODE_CLUSTER_LOW_BARS,
)
from app.services.node_cluster_input_provider import (
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
