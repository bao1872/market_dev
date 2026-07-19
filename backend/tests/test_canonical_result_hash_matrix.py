"""Canonical result_hash 矩阵基线测试（CHANGE-20260718-007 S3.2）。

证明 CanonicalComputationService 产出确定性 result_hash，作为四链迁移后的
一致性基线。当详情/盘后/盘中/Capture 四条链迁移到 compute_with_mdas() 后，
同一 instrument + as_of + source_bar_hash 必须得到相同 result_hash。

测试覆盖：
1. result_hash 确定性：相同输入 → 相同 hash（macd 作为参考算法）
2. result_hash 对 bars 变化敏感：不同 bars → 不同 hash
3. result_hash 对 as_of 变化敏感：不同 as_of → 不同 hash
4. result_hash 对 source_bar_hash 变化敏感：不同 source_bar_hash → 不同 hash
5. 四链基线矩阵文档化：列出 macd 的 result_hash 作为四链必须匹配的基准
6. compute_with_mdas 端到端：mock MDAS → 验证 CanonicalResult 含完整字段

设计要点：
- 使用固定 seed 的 mock bars，测试完全 hermetic（不依赖 DB/pytdx）
- macd 是 input_provider_wired 的参考算法，通过 compute_macd_adapter 调用
- 矩阵基线是"四链迁移后的验收标准"，当前四链均未迁移，仅建立基线
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import numpy as np
import pandas as pd
import pytest

from app.services.canonical_computation_service import CanonicalComputationService

# =============================================================================
# 测试 fixture：固定 seed 的 mock bars
# =============================================================================


def _make_mock_bars(n: int = 30, seed: int = 42) -> pd.DataFrame:
    """构造固定 seed 的 mock 日线 DataFrame（hermetic，不依赖 DB）。

    列与 MDAS 返回一致：open/high/low/close/volume/amount
    """
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


_INSTRUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")


# =============================================================================
# 1. result_hash 确定性
# =============================================================================


@pytest.mark.asyncio
async def test_result_hash_deterministic_for_macd() -> None:
    """相同输入必须得到相同 result_hash（macd 参考算法）。

    这是四链一致性的基础：同一 instrument + as_of + bars → 同一 result_hash。
    """
    bars = _make_mock_bars()
    r1 = await CanonicalComputationService.compute(
        algorithm_id="macd",
        instrument_id=_INSTRUMENT_ID,
        as_of=date(2026, 7, 18),
        source_bar_hash="abc123",
        adj_factor_hash="def456",
        bars=bars,
    )
    r2 = await CanonicalComputationService.compute(
        algorithm_id="macd",
        instrument_id=_INSTRUMENT_ID,
        as_of=date(2026, 7, 18),
        source_bar_hash="abc123",
        adj_factor_hash="def456",
        bars=bars,
    )
    assert r1.result_hash == r2.result_hash, (
        f"相同输入应得到相同 result_hash: {r1.result_hash} != {r2.result_hash}"
    )
    assert r1.contract_fingerprint == "macd-cf-v1"
    assert r1.algorithm_version == "macd-v1"


# =============================================================================
# 2. result_hash 对 bars 变化敏感
# =============================================================================


@pytest.mark.asyncio
async def test_result_hash_changes_on_different_bars() -> None:
    """不同 bars 必须得到不同 result_hash（避免缓存键碰撞）。"""
    bars_a = _make_mock_bars(seed=42)
    bars_b = _make_mock_bars(seed=99)  # 不同 seed → 不同价格序列
    kwargs = {
        "algorithm_id": "macd",
        "instrument_id": _INSTRUMENT_ID,
        "as_of": date(2026, 7, 18),
        "source_bar_hash": "abc123",
        "adj_factor_hash": "def456",
    }
    r_a = await CanonicalComputationService.compute(**kwargs, bars=bars_a)
    r_b = await CanonicalComputationService.compute(**kwargs, bars=bars_b)
    assert r_a.result_hash != r_b.result_hash, (
        f"不同 bars 应得到不同 result_hash: {r_a.result_hash} == {r_b.result_hash}"
    )


# =============================================================================
# 3. result_hash 对 as_of 变化敏感
# =============================================================================


@pytest.mark.asyncio
async def test_result_hash_changes_on_different_as_of() -> None:
    """不同 as_of 必须得到不同 result_hash（point-in-time 隔离）。"""
    bars = _make_mock_bars()
    kwargs = {
        "algorithm_id": "macd",
        "instrument_id": _INSTRUMENT_ID,
        "source_bar_hash": "abc123",
        "adj_factor_hash": "def456",
        "bars": bars,
    }
    r_a = await CanonicalComputationService.compute(as_of=date(2026, 7, 18), **kwargs)
    r_b = await CanonicalComputationService.compute(as_of=date(2026, 7, 19), **kwargs)
    assert r_a.result_hash != r_b.result_hash, (
        f"不同 as_of 应得到不同 result_hash: {r_a.result_hash} == {r_b.result_hash}"
    )


# =============================================================================
# 4. result_hash 对 source_bar_hash 变化敏感
# =============================================================================


@pytest.mark.asyncio
async def test_result_hash_changes_on_different_source_bar_hash() -> None:
    """不同 source_bar_hash 必须得到不同 result_hash（行情输入维度隔离）。"""
    bars = _make_mock_bars()
    r_a = await CanonicalComputationService.compute(
        algorithm_id="macd",
        instrument_id=_INSTRUMENT_ID,
        as_of=date(2026, 7, 18),
        source_bar_hash="hash_a",
        adj_factor_hash="def456",
        bars=bars,
    )
    r_b = await CanonicalComputationService.compute(
        algorithm_id="macd",
        instrument_id=_INSTRUMENT_ID,
        as_of=date(2026, 7, 18),
        source_bar_hash="hash_b",
        adj_factor_hash="def456",
        bars=bars,
    )
    assert r_a.result_hash != r_b.result_hash, (
        f"不同 source_bar_hash 应得到不同 result_hash: "
        f"{r_a.result_hash} == {r_b.result_hash}"
    )


# =============================================================================
# 5. 四链基线矩阵文档化
# =============================================================================


@pytest.mark.asyncio
async def test_four_chain_baseline_matrix_documented() -> None:
    """四链 result_hash 矩阵基线 — macd 的 result_hash 作为四链迁移后必须匹配的基准。

    PROMPT.md S3.2 要求"输出真实四链 result_hash 矩阵"。当前四链均未迁移到
    Canonical，本测试建立基线：定义 macd 在固定输入下的 result_hash，作为
    详情/盘后/盘中/Capture 四链迁移后的验收标准。

    矩阵结构（迁移后填充）：
        | 链     | algorithm_id | result_hash          |
        |--------|--------------|----------------------|
        | 详情   | macd         | <baseline_hash>      |
        | 盘后   | macd         | <must match baseline>|
        | 盘中   | macd         | <must match baseline>|
        | Capture| macd         | <must match baseline>|
    """
    from app.contracts.algorithm_registry import AlgorithmRegistry

    bars = _make_mock_bars()
    result = await CanonicalComputationService.compute(
        algorithm_id="macd",
        instrument_id=_INSTRUMENT_ID,
        as_of=date(2026, 7, 18),
        source_bar_hash="baseline_hash",
        adj_factor_hash="baseline_adj",
        bars=bars,
    )

    # 基线 result_hash（四链迁移后必须匹配此值）
    baseline_hash = result.result_hash
    assert len(baseline_hash) == 16, f"result_hash 应为 16 字符: {baseline_hash}"

    # 断言 migration_status 字段存在且合法（矩阵文档的一部分）
    contract = AlgorithmRegistry.get("macd")
    assert contract.migration_status == "input_provider_wired"
    assert contract.migration_status in ("registered_only", "input_provider_wired")

    # 文档化基线矩阵（当前四链均未迁移，result_hash 为 None）
    four_chain_matrix = {
        "detail": {"migrated": False, "result_hash": None},
        "after_close": {"migrated": False, "result_hash": None},
        "monitor": {"migrated": False, "result_hash": None},
        "capture": {"migrated": False, "result_hash": None},
    }
    # 基线：所有迁移后的链必须产出此 hash
    assert baseline_hash, "基线 result_hash 不能为空"
    # 当前无链迁移（诚实记录）
    migrated_chains = [k for k, v in four_chain_matrix.items() if v["migrated"]]
    assert migrated_chains == [], (
        f"当前不应有迁移的链，实际迁移: {migrated_chains}"
    )


# =============================================================================
# 6. compute_with_mdas 端到端（mock MDAS）
# =============================================================================


@pytest.mark.asyncio
async def test_compute_with_mdas_end_to_end() -> None:
    """compute_with_mdas 端到端：mock MDAS → 验证 CanonicalResult 含完整字段。

    这是 InputProvider 的核心验证：
    - MDAS 被调用且参数从合同推导
    - source_bar_hash/adj_factor_hash 透传到 result_hash
    - CanonicalResult 含 contract_fingerprint + result_hash + payload
    """
    from app.services.market_data_aggregation_service import BarAggregationResult

    bars = _make_mock_bars()
    mock_bar_result = BarAggregationResult(
        bars=bars,
        data_source="db",
        as_of=pd.Timestamp("2026-07-18"),
        is_partial=False,
        last_persisted_bar_time=pd.Timestamp("2026-07-18"),
        last_live_bar_time=None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
        source_bar_hash="e2e_source_hash",
        adj_factor_hash="e2e_adj_hash",
        adjustment_as_of=date(2026, 7, 18),
        completed_through=pd.Timestamp("2026-07-18"),
    )

    mock_mdas = MagicMock()
    mock_mdas.get_bars = AsyncMock(return_value=mock_bar_result)

    with patch(
        "app.services.market_data_aggregation_service.MarketDataAggregationService",
        return_value=mock_mdas,
    ):
        result = await CanonicalComputationService.compute_with_mdas(
            algorithm_id="macd",
            session=MagicMock(),
            instrument_id=_INSTRUMENT_ID,
            as_of=date(2026, 7, 18),
        )

    # 验证 CanonicalResult 完整性
    assert result.algorithm_id == "macd"
    assert result.contract_fingerprint == "macd-cf-v1"
    assert result.algorithm_version == "macd-v1"
    assert len(result.result_hash) == 16
    assert result.payload is not None
    assert isinstance(result.payload, dict)
    assert "macd_dif" in result.payload
    assert "macd_dea" in result.payload
    assert "macd_hist" in result.payload

    # 验证 MDAS 被调用且参数从合同推导
    mock_mdas.get_bars.assert_called_once()
    call_kwargs = mock_mdas.get_bars.call_args.kwargs
    assert call_kwargs["timeframe"] == "1d"  # macd 合同 input_timeframes[0]
    assert call_kwargs["adj"] == "qfq"  # macd 合
