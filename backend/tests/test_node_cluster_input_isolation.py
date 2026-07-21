"""[NC-01/NC-02/NC-03 V2] Node Cluster 输入合同隔离测试。

验证：
1. _load_node_cluster_inputs 固定使用 completed qfq（include_realtime=False,
   completed_only=True），与页面参数结构隔离。
2. daily limit = DAILY_HISTORY_BARS = 250（合同常量，非页面 bars 参数）。
3. 15m limit = NODE_CLUSTER_LOW_BARS = 4000（合同常量，非页面 bars 参数）。
4. load_15m=False 时跳过 15m 查询（性能优化，Node 进入 degraded 模式）。
5. 60/90/120 不变性：页面 bars 参数（60/90/120/250）不影响 Node Cluster 输入。
   - 90 是前端 defaultVisibleBars（飞书舞台），不传后端 API；
   - 250 是 DAILY_HISTORY_BARS（Node daily 输入合同常量）；
   - 4000 是 NODE_CLUSTER_LOW_BARS（Node 15m 输入合同常量）。
6. 静态合同：_load_node_cluster_inputs 函数签名不含 bars 参数（结构隔离）。

运行方式：
- pytest（需要 APP_ENV=test + TEST_DATABASE_URL）:
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_node_cluster_input_isolation.py
- standalone（不需要 DB，用于 Phase 1A 验证）:
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
from app.services.indicator_service import _load_node_cluster_inputs

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


def _make_agg_result(bars: pd.DataFrame) -> MagicMock:
    """构造 MDAS BarAggregationResult mock。"""
    result = MagicMock()
    result.bars = bars
    return result


# ============================================================
# 1. _load_node_cluster_inputs 合同验证
# ============================================================

@pytest.mark.asyncio
async def test_load_node_cluster_inputs_uses_completed_qfq_for_daily() -> None:
    """[NC-01] daily 查询必须使用 include_realtime=False, completed_only=True。"""
    daily_bars_df = _make_bars(300)
    expected_15m_df = _make_bars(4000, freq="15min")

    with patch(
        "app.services.indicator_service.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(daily_bars_df),  # daily 查询
                _make_agg_result(expected_15m_df),  # 15m 查询
            ]
        )

        daily_bars, bars_15min = await _load_node_cluster_inputs(
            MagicMock(), MagicMock(), "qfq",
        )

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
async def test_load_node_cluster_inputs_uses_completed_qfq_for_15m() -> None:
    """[NC-02] 15m 查询必须使用 include_realtime=False, completed_only=True。"""
    daily_bars_df = _make_bars(300)
    expected_15m_df = _make_bars(4000, freq="15min")

    with patch(
        "app.services.indicator_service.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(daily_bars_df),
                _make_agg_result(expected_15m_df),
            ]
        )

        daily_bars, bars_15min = await _load_node_cluster_inputs(
            MagicMock(), MagicMock(), "qfq",
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


@pytest.mark.asyncio
async def test_load_node_cluster_inputs_daily_tailed_to_250() -> None:
    """[NC-03] daily 返回必须 tail(DAILY_HISTORY_BARS)，不能返回全量。"""
    daily_bars_df = _make_bars(1000)  # 1000 根，超过 250
    expected_15m_df = _make_bars(4000, freq="15min")

    with patch(
        "app.services.indicator_service.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(daily_bars_df),
                _make_agg_result(expected_15m_df),
            ]
        )

        daily_bars, _ = await _load_node_cluster_inputs(
            MagicMock(), MagicMock(), "qfq",
        )

    assert len(daily_bars) == DAILY_HISTORY_BARS, (
        f"daily_bars 必须被 tail 到 DAILY_HISTORY_BARS={DAILY_HISTORY_BARS}，"
        f"实际={len(daily_bars)}"
    )


@pytest.mark.asyncio
async def test_load_node_cluster_inputs_load_15m_false_skips_15m_query() -> None:
    """load_15m=False 时跳过 15m 查询（性能优化，Node 进入 degraded 模式）。"""
    daily_bars_df = _make_bars(300)

    with patch(
        "app.services.indicator_service.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[_make_agg_result(daily_bars_df)]
        )

        daily_bars, bars_15min = await _load_node_cluster_inputs(
            MagicMock(), MagicMock(), "qfq",
            load_15m=False,
        )

    # 只应有一次 daily 查询，不应有 15m 查询
    assert mock_mdas.get_bars.call_count == 1, (
        "load_15m=False 时只应查询 daily，不应查询 15m"
    )
    assert mock_mdas.get_bars.call_args_list[0].kwargs["timeframe"] == "1d"
    assert bars_15min.empty, "load_15m=False 时 bars_15min 必须为空 DataFrame"


@pytest.mark.asyncio
async def test_load_node_cluster_inputs_passes_adjustment_as_of() -> None:
    """adjustment_as_of 必须透传到 MDAS（保证四链 hash 一致）。"""
    adj_anchor = date(2024, 6, 30)

    with patch(
        "app.services.indicator_service.MarketDataAggregationService"
    ) as mock_mdas_cls:
        mock_mdas = mock_mdas_cls.return_value
        mock_mdas.get_bars = AsyncMock(
            side_effect=[
                _make_agg_result(_make_bars(300)),
                _make_agg_result(_make_bars(4000, freq="15min")),
            ]
        )

        await _load_node_cluster_inputs(
            MagicMock(), MagicMock(), "qfq",
            adjustment_as_of=adj_anchor,
        )

    # daily 和 15m 查询都应透传 adjustment_as_of
    for call in mock_mdas.get_bars.call_args_list:
        assert call.kwargs["adjustment_as_of"] == adj_anchor, (
            "adjustment_as_of 必须透传到 MDAS（display 与 Node 共用同一锚点）"
        )


# ============================================================
# 2. 60/90/120 不变性验证
# ============================================================

def test_load_node_cluster_inputs_signature_excludes_bars_param() -> None:
    """[NC-03] _load_node_cluster_inputs 函数签名不含 bars 参数（结构隔离）。

    这是 60/90/120 不变性的根本保证：页面 bars 参数无法进入 Node Cluster 输入。
    """
    sig = inspect.signature(_load_node_cluster_inputs)
    assert "bars" not in sig.parameters, (
        "NC-03 违规：_load_node_cluster_inputs 签名不得包含 bars 参数，"
        "否则页面显示需求会污染 Node Cluster 计算"
    )
    # 确认合同参数存在
    assert "load_15m" in sig.parameters, "load_15m 是 needs_15min 控制参数"
    assert "adjustment_as_of" in sig.parameters, "adjustment_as_of 是复权锚点"


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
    # DAILY_HISTORY_BARS 和 NODE_CLUSTER_LOW_BARS 不得是 60/90/120
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
# 3. 静态合同：compute_all_indicators 调用 _load_node_cluster_inputs
# ============================================================

def test_compute_all_indicators_calls_load_node_cluster_inputs() -> None:
    """[NC-01/NC-02] compute_all_indicators 必须通过 _load_node_cluster_inputs 加载 Node 输入。

    通过 AST 静态分析验证，确保不被误改回直接 MDAS 查询。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod.compute_all_indicators)
    tree = ast.parse(source)

    # 收集所有函数调用
    call_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.append(node.func.attr)

    assert "_load_node_cluster_inputs" in call_names, (
        "compute_all_indicators 必须调用 _load_node_cluster_inputs 加载 Node 输入"
    )


def test_compute_all_indicators_node_cluster_uses_node_daily_bars() -> None:
    """[NC-01] _compute_independent_node_cluster 必须接收 node_daily_bars，不是 daily_bars。

    通过 AST 静态分析验证 Node Cluster 输入隔离。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod.compute_all_indicators)
    tree = ast.parse(source)

    # 查找 _compute_independent_node_cluster 调用
    found_node_cluster_call = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_compute_independent_node_cluster":
                found_node_cluster_call = True
                # 第一个位置参数必须是 node_daily_bars（Name 节点）
                assert len(node.args) >= 1, (
                    "_compute_independent_node_cluster 必须有第一个位置参数"
                )
                first_arg = node.args[0]
                assert isinstance(first_arg, ast.Name) and first_arg.id == "node_daily_bars", (
                    "NC-01 违规：_compute_independent_node_cluster 第一个参数必须是 node_daily_bars，"
                    "不是 daily_bars（display 可能含 realtime partial bar）"
                )

    assert found_node_cluster_call, (
        "compute_all_indicators 必须调用 _compute_independent_node_cluster"
    )


def test_compute_all_indicators_no_direct_15m_query_with_realtime() -> None:
    """[NC-02] compute_all_indicators 不得直接查询 15m with include_realtime=True for Node。

    通过源码字符串扫描验证：bars_15min 不得来自 include_realtime=True 的直接 MDAS 查询。
    """
    import app.services.indicator_service as mod

    source = inspect.getsource(mod.compute_all_indicators)
    # 旧的违规模式：r15 = await _mdas.get_bars(... include_realtime=True ... limit=NODE_CLUSTER_LOW_BARS)
    # 已替换为 _load_node_cluster_inputs 调用
    # 验证：源码中不应有 include_realtime=True 与 limit=NODE_CLUSTER_LOW_BARS 同时出现
    assert "include_realtime=True, limit=NODE_CLUSTER_LOW_BARS" not in source, (
        "NC-02 违规：compute_all_indicators 不得直接查询 15m with include_realtime=True"
    )


# ============================================================
# standalone 运行入口（不需要 pytest/DB，用于 Phase 1A 验证）
# ============================================================

def _run_standalone_tests() -> int:
    """不依赖 pytest 的 standalone 测试运行器。

    Returns:
        失败的测试数量（0 = 全部通过）
    """
    import asyncio

    failures: list[str] = []

    async def run_async_tests() -> None:
        # 1. test_load_node_cluster_inputs_uses_completed_qfq_for_daily
        try:
            daily_bars_df = _make_bars(300)
            expected_15m_df = _make_bars(4000, freq="15min")
            with patch(
                "app.services.indicator_service.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(daily_bars_df),
                        _make_agg_result(expected_15m_df),
                    ]
                )
                await _load_node_cluster_inputs(MagicMock(), MagicMock(), "qfq")
            daily_call = mock_mdas.get_bars.call_args_list[0]
            assert daily_call.kwargs["include_realtime"] is False
            assert daily_call.kwargs["completed_only"] is True
            assert daily_call.kwargs["limit"] == DAILY_HISTORY_BARS
            print("  ✓ test_load_node_cluster_inputs_uses_completed_qfq_for_daily")
        except AssertionError as e:
            failures.append(f"daily qfq: {e}")

        # 2. test_load_node_cluster_inputs_uses_completed_qfq_for_15m
        try:
            daily_bars_df = _make_bars(300)
            expected_15m_df = _make_bars(4000, freq="15min")
            with patch(
                "app.services.indicator_service.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(daily_bars_df),
                        _make_agg_result(expected_15m_df),
                    ]
                )
                await _load_node_cluster_inputs(MagicMock(), MagicMock(), "qfq")
            call_15m = mock_mdas.get_bars.call_args_list[1]
            assert call_15m.kwargs["include_realtime"] is False
            assert call_15m.kwargs["completed_only"] is True
            assert call_15m.kwargs["limit"] == NODE_CLUSTER_LOW_BARS
            print("  ✓ test_load_node_cluster_inputs_uses_completed_qfq_for_15m")
        except AssertionError as e:
            failures.append(f"15m qfq: {e}")

        # 3. test_load_node_cluster_inputs_daily_tailed_to_250
        try:
            daily_bars_df = _make_bars(1000)
            expected_15m_df = _make_bars(4000, freq="15min")
            with patch(
                "app.services.indicator_service.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(daily_bars_df),
                        _make_agg_result(expected_15m_df),
                    ]
                )
                daily_bars, _ = await _load_node_cluster_inputs(
                    MagicMock(), MagicMock(), "qfq",
                )
            assert len(daily_bars) == DAILY_HISTORY_BARS
            print("  ✓ test_load_node_cluster_inputs_daily_tailed_to_250")
        except AssertionError as e:
            failures.append(f"daily tail: {e}")

        # 4. test_load_node_cluster_inputs_load_15m_false_skips_15m_query
        try:
            daily_bars_df = _make_bars(300)
            with patch(
                "app.services.indicator_service.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[_make_agg_result(daily_bars_df)]
                )
                _, bars_15min = await _load_node_cluster_inputs(
                    MagicMock(), MagicMock(), "qfq",
                    load_15m=False,
                )
            assert mock_mdas.get_bars.call_count == 1
            assert bars_15min.empty
            print("  ✓ test_load_node_cluster_inputs_load_15m_false_skips_15m_query")
        except AssertionError as e:
            failures.append(f"load_15m=False: {e}")

        # 5. test_load_node_cluster_inputs_passes_adjustment_as_of
        try:
            adj_anchor = date(2024, 6, 30)
            with patch(
                "app.services.indicator_service.MarketDataAggregationService"
            ) as mock_mdas_cls:
                mock_mdas = mock_mdas_cls.return_value
                mock_mdas.get_bars = AsyncMock(
                    side_effect=[
                        _make_agg_result(_make_bars(300)),
                        _make_agg_result(_make_bars(4000, freq="15min")),
                    ]
                )
                await _load_node_cluster_inputs(
                    MagicMock(), MagicMock(), "qfq",
                    adjustment_as_of=adj_anchor,
                )
            for call in mock_mdas.get_bars.call_args_list:
                assert call.kwargs["adjustment_as_of"] == adj_anchor
            print("  ✓ test_load_node_cluster_inputs_passes_adjustment_as_of")
        except AssertionError as e:
            failures.append(f"adjustment_as_of: {e}")

    asyncio.run(run_async_tests())

    # 6. 静态合同测试（同步）
    try:
        sig = inspect.signature(_load_node_cluster_inputs)
        assert "bars" not in sig.parameters
        assert "load_15m" in sig.parameters
        print("  ✓ test_load_node_cluster_inputs_signature_excludes_bars_param")
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

    try:
        test_compute_all_indicators_calls_load_node_cluster_inputs()
        print("  ✓ test_compute_all_indicators_calls_load_node_cluster_inputs")
    except AssertionError as e:
        failures.append(f"AST call: {e}")

    try:
        test_compute_all_indicators_node_cluster_uses_node_daily_bars()
        print("  ✓ test_compute_all_indicators_node_cluster_uses_node_daily_bars")
    except AssertionError as e:
        failures.append(f"AST node_daily_bars: {e}")

    try:
        test_compute_all_indicators_no_direct_15m_query_with_realtime()
        print("  ✓ test_compute_all_indicators_no_direct_15m_query_with_realtime")
    except AssertionError as e:
        failures.append(f"AST no direct 15m: {e}")

    return len(failures)


if __name__ == "__main__":
    print("=" * 70)
    print("Node Cluster 输入合同隔离测试 (NC-01/NC-02/NC-03)")
    print("=" * 70)
    failed = _run_standalone_tests()
    print("=" * 70)
    if failed == 0:
        print("全部通过 ✓")
        raise SystemExit(0)
    else:
        print(f"失败 {failed} 项")
        raise SystemExit(1)
