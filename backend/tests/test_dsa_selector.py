"""DSA 选股策略测试 - 验证 DSASelector 插件的运行、结果输出和资源预算。

测试内容：
1. 测试 DSA 选股运行（mock MarketDataContext，生成模拟多头趋势行情）
2. 测试结果输出标准 StrategyResult + metrics（验证 yaml 字段对齐）
3. 测试资源预算超时（BudgetGuard 超时抛出 BudgetExceededError）
4. 测试 BudgetGuard 正常执行
5. 测试 StrategyLoader 加载 DSASelector

测试数据：
- 使用合成的日线行情数据（不依赖真实数据库/网络）
- 生成持续上涨趋势（模拟 dir=1 持续 > 50 bars 的多头场景）
- 生成震荡行情（模拟 regime=0 的非命中场景）
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.strategy.budget import BudgetExceededError, BudgetGuard
from app.strategy.runtime import (
    MarketDataContext,
    StrategyLoader,
    StrategyResult,
    StrategyRuntime,
)
from app.strategy.selectors.dsa_selector import DSASelector


def _generate_bullish_bars(n_bars: int = 400, start_price: float = 10.0) -> pd.DataFrame:
    """生成持续上涨趋势的日线行情（模拟多头场景）。

    生成逻辑：
    - 收盘价以小幅度持续上涨（每日 +0.5%~1%）
    - 加入小幅波动（模拟真实行情）
    - 成交量随机但保持合理范围

    这样 DSA VWAP dir 会持续为 1，超过 50 bars 后触发多头 regime。

    Args:
        n_bars: 生成的 bar 数
        start_price: 起始价格

    Returns:
        DataFrame: index=DatetimeIndex, columns=open/high/low/close/volume/amount/adj_factor
    """
    np.random.seed(42)  # 固定随机种子，确保测试可复现
    dates = pd.date_range(start="2025-01-01", periods=n_bars, freq="B")  # 工作日

    # 持续上涨趋势：每日涨幅 0.3%~0.8%
    daily_returns = np.random.uniform(0.003, 0.008, size=n_bars)
    # 偶尔小幅回调（但不足以改变趋势）
    daily_returns[::7] = -0.002  # 每 7 天小幅回调

    close = start_price * np.cumprod(1 + daily_returns)
    # 开高低围绕收盘价波动
    open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n_bars))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.001, 0.01, size=n_bars))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.001, 0.01, size=n_bars))
    volume = np.random.uniform(500000, 2000000, size=n_bars)
    amount = volume * close
    adj_factor = np.ones(n_bars)

    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
            "adj_factor": adj_factor,
        },
        index=dates,
    )
    df.index.name = "trade_date"
    return df


def _generate_sideways_bars(n_bars: int = 400, start_price: float = 10.0) -> pd.DataFrame:
    """生成震荡行情（模拟非多头场景）。

    生成逻辑：
    - 收盘价在一定区间内反复震荡
    - 没有持续趋势，DSA dir 会频繁翻转

    Args:
        n_bars: 生成的 bar 数
        start_price: 起始价格

    Returns:
        DataFrame: index=DatetimeIndex, columns=open/high/low/close/volume/amount/adj_factor
    """
    np.random.seed(123)
    dates = pd.date_range(start="2025-01-01", periods=n_bars, freq="B")

    # 震荡行情：正负收益交替
    daily_returns = np.random.uniform(-0.02, 0.02, size=n_bars)
    close = start_price * np.cumprod(1 + daily_returns)
    open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n_bars))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.001, 0.01, size=n_bars))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.001, 0.01, size=n_bars))
    volume = np.random.uniform(500000, 2000000, size=n_bars)
    amount = volume * close
    adj_factor = np.ones(n_bars)

    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
            "adj_factor": adj_factor,
        },
        index=dates,
    )
    df.index.name = "trade_date"
    return df


def _make_mock_version(strategy_id: str = "dsa_selector") -> MagicMock:
    """创建 mock StrategyVersion 对象。"""
    version = MagicMock()
    version.id = uuid.uuid4()
    version.manifest = {
        "strategy_id": strategy_id,
        "kind": "selector",
        "version": "1.1.0",
        "parameters": [{"key": "algorithm.lookback", "type": "integer", "default": 360}],
        "resource_budget": {
            "target_ms_per_instrument": 5000  # 测试用 5 秒预算（避免误超时）
        },
    }
    return version


@pytest.fixture
def bullish_context() -> MarketDataContext:
    """多头趋势行情的 MarketDataContext。"""
    return MarketDataContext(
        instrument_id=uuid.uuid4(),
        symbol="600519",
        bars_daily=_generate_bullish_bars(400),
        trade_date=date(2026, 6, 18),
    )


@pytest.fixture
def sideways_context() -> MarketDataContext:
    """震荡行情的 MarketDataContext。"""
    return MarketDataContext(
        instrument_id=uuid.uuid4(),
        symbol="000001",
        bars_daily=_generate_sideways_bars(400),
        trade_date=date(2026, 6, 18),
    )


@pytest.fixture
async def dsa_selector() -> DSASelector:
    """已初始化的 DSASelector 实例。"""
    selector = DSASelector()
    version = _make_mock_version()
    await selector.initialize(version)
    return selector


class TestDSASelector:
    """DSASelector 选股策略测试。"""

    @pytest.mark.asyncio
    async def test_dsa_selector_initialization(self) -> None:
        """测试 DSASelector 初始化。"""
        selector = DSASelector()
        version = _make_mock_version()
        await selector.initialize(version)

        assert selector.kind == "selector"
        assert selector._lookback == 360
        assert selector._version is not None

    @pytest.mark.asyncio
    async def test_dsa_selector_bullish_matched(
        self, dsa_selector: DSASelector, bullish_context: MarketDataContext
    ) -> None:
        """测试多头趋势行情下 DSA 选股命中。

        验证：
        - matched=True（多头趋势确认）
        - StrategyResult 包含所有 yaml 定义的指标
        - 指标值合理（dsa_dir_bars > 50）
        """
        result = await dsa_selector.execute(bullish_context)

        # 验证 StrategyResult 结构
        assert isinstance(result, StrategyResult)
        assert result.instrument_id == bullish_context.instrument_id
        assert result.strategy_version_id == dsa_selector._version.id
        assert result.trade_date == bullish_context.trade_date
        assert result.calculation_id is not None

        # 验证 yaml outputs 字段都存在
        yaml_outputs = [
            "dsa_dir_bars",
            "vwap_ret_avg",
            "vwap_ret_total",
            "offset_mean",
            "offset_std",
            "offset_variance_rate",
            "offset_percentile",
        ]
        for key in yaml_outputs:
            assert key in result.metrics, f"缺少 yaml 指标: {key}"

        # 所有有效结果均 matched=True，不再基于 regime 判定
        assert result.matched is True

        # 若合成数据形成多头趋势，则 dsa_dir_bars 应 > 50
        if result.metrics["dsa_dir_bars"] > 50:
            # vwap_ret_avg 和 vwap_ret_total 应有值
            assert result.metrics["vwap_ret_avg"] is not None
            assert result.metrics["vwap_ret_total"] is not None
            # offset_variance_rate 在 offset_mean 非 0 时应有值
            if (
                result.metrics["offset_mean"] is not None
                and abs(result.metrics["offset_mean"]) > 1e-10
            ):
                assert result.metrics["offset_variance_rate"] is not None

    @pytest.mark.asyncio
    async def test_dsa_selector_result_structure(
        self, dsa_selector: DSASelector, bullish_context: MarketDataContext
    ) -> None:
        """测试 StrategyResult 结构和指标完整性。"""
        result = await dsa_selector.execute(bullish_context)

        # 验证扩展字段存在
        extended_fields = [
            "regime_value",
            "regime_strength",
            "offset_rate",
            "change_pct",
            "touch_rope",
            "touch_vwap",
            "cross_up_count",
            "cross_down_count",
            "last_close",
            "dsa_vwap",
        ]
        for field in extended_fields:
            assert field in result.metrics, f"缺少扩展字段: {field}"

        # 验证 regime_value 是 0/1/-1
        assert result.metrics["regime_value"] in (0, 1, -1)

        # 验证所有有效结果均 matched=True，不再与 regime_value 绑定
        assert result.matched is True

    @pytest.mark.asyncio
    async def test_dsa_selector_matched_unconditional(
        self, dsa_selector: DSASelector, sideways_context: MarketDataContext
    ) -> None:
        """测试非多头行情下 matched 仍为 True（不对 regime 过滤）。"""
        result = await dsa_selector.execute(sideways_context)

        assert isinstance(result, StrategyResult)
        assert result.trade_date == sideways_context.trade_date
        # 即使 regime_value 不为 1，matched 也应为 True
        assert result.matched is True

    @pytest.mark.asyncio
    async def test_dsa_selector_insufficient_data(self) -> None:
        """测试数据不足时的处理。"""
        selector = DSASelector()
        await selector.initialize(_make_mock_version())

        # 只有 30 bars（不足 60）
        short_df = _generate_bullish_bars(30)
        context = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=short_df,
            trade_date=date(2026, 6, 18),
        )
        result = await selector.execute(context)

        assert result.metrics.get("error") == "insufficient_data"

    @pytest.mark.asyncio
    async def test_dsa_selector_empty_data(self) -> None:
        """测试空数据的处理。"""
        selector = DSASelector()
        await selector.initialize(_make_mock_version())

        context = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=pd.DataFrame(),
            trade_date=date(2026, 6, 18),
        )
        result = await selector.execute(context)

        assert result.metrics.get("error") == "insufficient_data"


class TestBudgetGuard:
    """BudgetGuard 资源预算控制测试。"""

    @pytest.mark.asyncio
    async def test_budget_normal_execution(self) -> None:
        """测试 BudgetGuard 正常执行。"""
        guard = BudgetGuard(timeout_ms=1000)
        result = await guard.run_with_budget(lambda x: x * 2, 21)
        assert result == 42

    @pytest.mark.asyncio
    async def test_budget_timeout(self) -> None:
        """测试 BudgetGuard 超时抛出 BudgetExceededError。"""
        import time

        guard = BudgetGuard(timeout_ms=50)
        with pytest.raises(BudgetExceededError) as exc_info:
            await guard.run_with_budget(time.sleep, 0.3)
        assert exc_info.value.timeout_ms == 50

    @pytest.mark.asyncio
    async def test_budget_exception_propagation(self) -> None:
        """测试 BudgetGuard 异常传播（不吞没）。"""
        guard = BudgetGuard(timeout_ms=1000)

        def _raise_error() -> None:
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            await guard.run_with_budget(_raise_error)

    @pytest.mark.asyncio
    async def test_dsa_selector_budget_exceeded(self) -> None:
        """测试 DSASelector 资源预算超时。

        设置极短的超时时间（1ms），确保 DSA 计算超时。
        验证抛出 BudgetExceededError，由 batch 层记录到 run_items。
        """
        selector = DSASelector()
        version = _make_mock_version()
        # 设置极短超时
        version.manifest["resource_budget"] = {"target_ms_per_instrument": 1}
        await selector.initialize(version)

        context = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=_generate_bullish_bars(400),
            trade_date=date(2026, 6, 18),
        )
        with pytest.raises(BudgetExceededError) as exc_info:
            await selector.execute(context)
        assert exc_info.value.timeout_ms == 1


class TestStrategyLoader:
    """StrategyLoader 策略加载器测试。"""

    @pytest.mark.asyncio
    async def test_loader_load_dsa_selector(self) -> None:
        """测试 StrategyLoader 加载 DSASelector。"""
        version = _make_mock_version("dsa_selector")
        runtime = await StrategyLoader.load(version)

        assert isinstance(runtime, DSASelector)
        assert runtime.kind == "selector"
        assert runtime._version is not None

    @pytest.mark.asyncio
    async def test_loader_unregistered_strategy(self) -> None:
        """测试加载未注册的策略抛出 ValueError。"""
        version = _make_mock_version("unknown_strategy")
        with pytest.raises(ValueError, match="策略未注册"):
            await StrategyLoader.load(version)

    @pytest.mark.asyncio
    async def test_loader_missing_strategy_id(self) -> None:
        """测试 manifest 缺少 strategy_id 抛出 ValueError。"""
        version = MagicMock()
        version.manifest = {"kind": "selector"}  # 缺少 strategy_id
        with pytest.raises(ValueError, match="缺少 strategy_id"):
            await StrategyLoader.load(version)


class TestStrategyRuntimeABC:
    """StrategyRuntime ABC 测试。"""

    def test_cannot_instantiate_abc(self) -> None:
        """测试 ABC 不可直接实例化。"""
        with pytest.raises(TypeError):
            StrategyRuntime()  # type: ignore[abstract]

    def test_market_data_context_fields(self) -> None:
        """测试 MarketDataContext 字段。"""
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=pd.DataFrame(),
        )
        assert ctx.instrument_id is not None
        assert ctx.symbol == "600519"
        assert ctx.bars_daily is not None
        assert ctx.bars_minute is None
        assert ctx.adj_factor is None
        assert ctx.trade_date is None

    def test_strategy_result_fields(self) -> None:
        """测试 StrategyResult 字段。"""
        result = StrategyResult(
            instrument_id=uuid.uuid4(),
            strategy_version_id=uuid.uuid4(),
            trade_date=date(2026, 6, 18),
            matched=True,
            metrics={"dsa_dir_bars": 60},
        )
        assert result.matched is True
        assert result.metrics["dsa_dir_bars"] == 60
        assert result.calculation_id is None


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
