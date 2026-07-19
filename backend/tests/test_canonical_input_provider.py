"""Canonical InputProvider 集成测试（CHANGE-20260718-007 S3.2）。

验证 compute_with_mdas() 的核心行为：
1. 通过 MDAS 获取行情（mock MDAS，验证 get_bars 被调用且参数从合同推导）
2. 拒绝 registered_only 算法（抛 ContractViolationError）
3. source_bar_hash/adj_factor_hash 透传到 compute()
4. timeframe 校验（不在 input_timeframes 时抛 ContractViolationError）
5. kernel_extra_kwargs 透传（如 fast/slow/signal）
6. 未注册算法抛 AlgorithmNotFoundError

设计要点：
- mock MarketDataAggregationService（不连真实 DB/pytdx）
- 测试 hermetic，不依赖外部状态
- macd 作为 input_provider_wired 参考算法
- smc/bollinger 等作为 registered_only 对照
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import numpy as np
import pandas as pd
import pytest

from app.services.canonical_computation_service import (
    AlgorithmNotFoundError,
    CanonicalComputationService,
    ContractViolationError,
)
from app.services.market_data_aggregation_service import BarAggregationResult

_INSTRUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")


def _make_mock_bars(n: int = 30, seed: int = 42) -> pd.DataFrame:
    """构造固定 seed 的 mock 日线 DataFrame。"""
    rng = np.random.default_rng(seed)
    prices = 100.0 + np.cumsum(rng.standard_normal(n) * 0.5)
    return pd.DataFrame(
        {
            "open": prices - 0.1,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": rng.integers(1000, 10000, size=n).astype(float),
            "amount": rng.integers(100000, 1000000, size=n).astype(float),
        },
        index=pd.date_range("2026-06-01", periods=n, freq="B"),
    )


def _make_mock_bar_result(bars: pd.DataFrame | None = None) -> BarAggregationResult:
    """构造 mock BarAggregationResult（含 source_bar_hash/adj_factor_hash）。"""
    return BarAggregationResult(
        bars=bars if bars is not None else _make_mock_bars(),
        data_source="db",
        as_of=pd.Timestamp("2026-07-18"),
        is_partial=False,
        last_persisted_bar_time=pd.Timestamp("2026-07-18"),
        last_live_bar_time=None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
        source_bar_hash="test_source_hash",
        adj_factor_hash="test_adj_hash",
        adjustment_as_of=date(2026, 7, 18),
        completed_through=pd.Timestamp("2026-07-18"),
    )


def _patch_mdas(bar_result: BarAggregationResult | None = None):
    """Patch MarketDataAggregationService 返回 mock bar_result。"""
    mock_mdas = MagicMock()
    mock_mdas.get_bars = AsyncMock(return_value=bar_result or _make_mock_bar_result())
    return patch(
        "app.services.market_data_aggregation_service.MarketDataAggregationService",
        return_value=mock_mdas,
    )


# =============================================================================
# 1. compute_with_mdas 通过 MDAS 获取行情
# =============================================================================


@pytest.mark.asyncio
async def test_compute_with_mdas_fetches_via_mdas() -> None:
    """compute_with_mdas 必须调用 MDAS.get_bars，且参数从合同推导。"""
    bar_result = _make_mock_bar_result()
    with _patch_mdas(bar_result) as mock_mdas_cls:
        result = await CanonicalComputationService.compute_with_mdas(
            algorithm_id="macd",
            session=MagicMock(),
            instrument_id=_INSTRUMENT_ID,
            as_of=date(2026, 7, 18),
        )

    # MDAS 被实例化且 get_bars 被调用
    mock_mdas_cls.assert_called_once()
    mock_mdas = mock_mdas_cls.return_value
    mock_mdas.get_bars.assert_called_once()

    # 验证参数从 macd 合同推导
    call_kwargs = mock_mdas.get_bars.call_args.kwargs
    assert call_kwargs["timeframe"] == "1d"  # macd.input_timeframes[0]
    assert call_kwargs["adj"] == "qfq"  # macd.adjustment_mode
    assert call_kwargs["completed_only"] is True  # macd.completed_only
    assert call_kwargs["warmup_bars"] == 250  # macd.warmup_bars
    assert call_kwargs["include_realtime"] is False  # not completed_only
    assert call_kwargs["instrument_id"] == _INSTRUMENT_ID

    # 结果含正确的 contract_fingerprint
    assert result.contract_fingerprint == "macd-cf-v1"
    assert result.algorithm_id == "macd"


# =============================================================================
# 2. compute_with_mdas 拒绝 registered_only 算法
# =============================================================================


@pytest.mark.asyncio
async def test_compute_with_mdas_rejects_registered_only() -> None:
    """registered_only 算法不能经 compute_with_mdas 调用（抛 ContractViolationError）。

    smc 的 migration_status=registered_only（compute_smc_dto callable 不存在）。
    """
    with _patch_mdas():
        with pytest.raises(ContractViolationError) as exc_info:
            await CanonicalComputationService.compute_with_mdas(
                algorithm_id="smc",
                session=MagicMock(),
                instrument_id=_INSTRUMENT_ID,
            )
    assert "registered_only" in str(exc_info.value)
    assert "smc" in str(exc_info.value)


@pytest.mark.asyncio
async def test_compute_with_mdas_rejects_bollinger_registered_only() -> None:
    """bollinger 也是 registered_only（compute_bollinger_bands callable 不存在）。"""
    with _patch_mdas():
        with pytest.raises(ContractViolationError) as exc_info:
            await CanonicalComputationService.compute_with_mdas(
                algorithm_id="bollinger",
                session=MagicMock(),
                instrument_id=_INSTRUMENT_ID,
            )
    assert "registered_only" in str(exc_info.value)


# =============================================================================
# 3. source_bar_hash/adj_factor_hash 透传
# =============================================================================


@pytest.mark.asyncio
async def test_compute_with_mdas_passes_hashes_to_compute() -> None:
    """MDAS 返回的 source_bar_hash/adj_factor_hash 必须透传到 result_hash 计算。

    验证方式：相同 bars 但不同 source_bar_hash → 不同 result_hash。
    """
    bars = _make_mock_bars()

    # 第一次：source_bar_hash = "hash_a"
    bar_result_a = _make_mock_bar_result(bars)
    bar_result_a.source_bar_hash = "hash_a"
    bar_result_a.adj_factor_hash = "adj_a"

    # 第二次：相同 bars 但 source_bar_hash = "hash_b"
    bar_result_b = _make_mock_bar_result(bars)
    bar_result_b.source_bar_hash = "hash_b"
    bar_result_b.adj_factor_hash = "adj_b"

    with _patch_mdas(bar_result_a):
        result_a = await CanonicalComputationService.compute_with_mdas(
            algorithm_id="macd",
            session=MagicMock(),
            instrument_id=_INSTRUMENT_ID,
            as_of=date(2026, 7, 18),
        )

    with _patch_mdas(bar_result_b):
        result_b = await CanonicalComputationService.compute_with_mdas(
            algorithm_id="macd",
            session=MagicMock(),
            instrument_id=_INSTRUMENT_ID,
            as_of=date(2026, 7, 18),
        )

    # 相同 bars 但不同 hash → 不同 result_hash
    assert result_a.result_hash != result_b.result_hash, (
        f"不同 source_bar_hash 应得到不同 result_hash: "
        f"{result_a.result_hash} == {result_b.result_hash}"
    )


# =============================================================================
# 4. timeframe 校验
# =============================================================================


@pytest.mark.asyncio
async def test_compute_with_mdas_validates_timeframe() -> None:
    """timeframe 不在 input_timeframes 时抛 ContractViolationError。

    macd 合同 input_timeframes=("1d", "15m", "1h", "1w", "1mo")，
    传入 "5m" 应被拒绝。
    """
    with _patch_mdas():
        with pytest.raises(ContractViolationError) as exc_info:
            await CanonicalComputationService.compute_with_mdas(
                algorithm_id="macd",
                session=MagicMock(),
                instrument_id=_INSTRUMENT_ID,
                timeframe="5m",  # 不在 macd.input_timeframes 中
            )
    assert "5m" in str(exc_info.value)
    assert "input_timeframes" in str(exc_info.value)


@pytest.mark.asyncio
async def test_compute_with_mdas_accepts_valid_timeframe() -> None:
    """合法 timeframe（在 input_timeframes 中）应正常工作。"""
    with _patch_mdas():
        # macd 支持 1d/15m/1h/1w/1mo
        result = await CanonicalComputationService.compute_with_mdas(
            algorithm_id="macd",
            session=MagicMock(),
            instrument_id=_INSTRUMENT_ID,
            timeframe="15m",
        )
    assert result.algorithm_id == "macd"
    assert len(result.result_hash) == 16


# =============================================================================
# 5. kernel_extra_kwargs 透传
# =============================================================================


@pytest.mark.asyncio
async def test_compute_with_mdas_passes_kernel_extra_kwargs() -> None:
    """kernel_extra_kwargs（如 fast/slow/signal）必须透传到 kernel。"""
    bars = _make_mock_bars()

    # 默认参数 (12, 26, 9)
    with _patch_mdas(_make_mock_bar_result(bars)):
        result_default = await CanonicalComputationService.compute_with_mdas(
            algorithm_id="macd",
            session=MagicMock(),
            instrument_id=_INSTRUMENT_ID,
            as_of=date(2026, 7, 18),
        )

    # 自定义参数 (5, 35, 5)
    with _patch_mdas(_make_mock_bar_result(bars)):
        result_custom = await CanonicalComputationService.compute_with_mdas(
            algorithm_id="macd",
            session=MagicMock(),
            instrument_id=_INSTRUMENT_ID,
            as_of=date(2026, 7, 18),
            fast=5,
            slow=35,
            signal=5,
        )

    # 不同参数 → 不同 MACD 值 → 不同 result_hash
    assert result_default.result_hash != result_custom.result_hash, (
        f"不同 fast/slow/signal 应得到不同 result_hash: "
        f"{result_default.result_hash} == {result_custom.result_hash}"
    )


# =============================================================================
# 6. 未注册算法抛 AlgorithmNotFoundError
# =============================================================================


@pytest.mark.asyncio
async def test_compute_with_mdas_raises_for_unknown_algorithm() -> None:
    """未注册算法抛 AlgorithmNotFoundError（而非 ContractViolationError）。"""
    with _patch_mdas():
        with pytest.raises(AlgorithmNotFoundError) as exc_info:
            await CanonicalComputationService.compute_with_mdas(
                algorithm_id="definitely_not_registered",
                session=MagicMock(),
                instrument_id=_INSTRUMENT_ID,
            )
    assert exc_info.value.algorithm_id == "definitely_not_registered"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
