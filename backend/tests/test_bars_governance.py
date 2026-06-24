"""数据治理集成测试（校验/保留/对账）。

测试内容：
1. validate_bars 5 条校验规则（正常+异常用例）
2. apply_retention_policy dry_run 模式（统计不删除）
3. reconcile_instrument 3 维度对账（缺失/多余/不一致）

How to Run:
    pytest tests/test_bars_governance.py -v
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.services.bars_retention import (
    RetentionResult,
    apply_retention_policy,
    get_retention_config,
)
from app.services.bars_validator import validate_bars
from app.services.reconcile_bars import ReconcileResult

# ============================================================
# 1. validate_bars 校验规则测试（5 条规则）
# ============================================================


def _build_valid_bars(n: int = 5) -> pd.DataFrame:
    """构造合法的行情 DataFrame（OHLC 关系成立、非负、无 NaN）。"""
    dates = pd.date_range("2026-06-16", periods=n, freq="D")
    return pd.DataFrame({
        "datetime": dates,
        "open": [10.0 + i * 0.1 for i in range(n)],
        "high": [10.5 + i * 0.1 for i in range(n)],
        "low": [9.8 + i * 0.1 for i in range(n)],
        "close": [10.2 + i * 0.1 for i in range(n)],
        "volume": [100000 + i * 1000 for i in range(n)],
        "amount": [1000000.0 + i * 10000 for i in range(n)],
    })


def test_validate_bars_normal() -> None:
    """规则全通过：合法数据应返回 is_valid=True。"""
    df = _build_valid_bars()
    result = validate_bars(df, symbol="000001", period="d")
    assert result.is_valid is True
    assert len(result.errors) == 0


def test_validate_bars_empty() -> None:
    """空 DataFrame 视为合法（无数据可校验）。"""
    result = validate_bars(pd.DataFrame(), symbol="000001", period="d")
    assert result.is_valid is True


def test_validate_bars_ohlc_violation() -> None:
    """规则 1：OHLC 关系不成立（high < open）。"""
    df = _build_valid_bars(1)
    df.loc[0, "high"] = 9.0  # high < open(10.0)，违反 high >= max(open, close, low)
    result = validate_bars(df, symbol="000001", period="d")
    assert result.is_valid is False
    assert any("OHLC" in e for e in result.errors)


def test_validate_bars_negative_volume() -> None:
    """规则 2：volume 为负数。"""
    df = _build_valid_bars(1)
    df.loc[0, "volume"] = -100
    result = validate_bars(df, symbol="000001", period="d")
    assert result.is_valid is False
    assert any("volume" in e and "负" in e for e in result.errors)


def test_validate_bars_nan_values() -> None:
    """规则 4：OHLCV 含 NaN。"""
    df = _build_valid_bars(1)
    df.loc[0, "close"] = float("nan")
    result = validate_bars(df, symbol="000001", period="d")
    assert result.is_valid is False
    assert any("NaN" in e for e in result.errors)


def test_validate_bars_zero_price() -> None:
    """规则 2：价格为 0（非正）。"""
    df = _build_valid_bars(1)
    df.loc[0, "close"] = 0.0
    result = validate_bars(df, symbol="000001", period="d")
    assert result.is_valid is False
    assert any("价格" in e or "正" in e for e in result.errors)


def test_validate_bars_abnormal_price() -> None:
    """规则 5：异常价格 close >= 100000。"""
    df = _build_valid_bars(1)
    df.loc[0, "close"] = 200000.0
    result = validate_bars(df, symbol="000001", period="d")
    assert result.is_valid is False
    assert any("超过上限" in e or "上限" in e for e in result.errors)


# ============================================================
# 2. apply_retention_policy dry_run 测试
# ============================================================


def test_retention_config() -> None:
    """验证保留策略配置：1 张永久保留 + 3 张限期保留。"""
    config = get_retention_config()
    assert len(config) == 4

    permanent = [c for c in config if c["is_permanent"]]
    assert len(permanent) == 1
    permanent_names = {c["table_name"] for c in permanent}
    assert permanent_names == {"bars_daily"}

    limited = [c for c in config if not c["is_permanent"]]
    assert len(limited) == 3
    config_by_table = {c["table_name"]: c for c in limited}
    assert config_by_table["bars_15min"]["retention_days"] == 730
    assert config_by_table["bars_60min"]["retention_days"] == 730
    assert config_by_table["bars_minute"]["retention_days"] == 30


@pytest.mark.asyncio
async def test_retention_dry_run() -> None:
    """dry_run 模式：只统计不删除，返回各表待删除数。"""
    # 构造 mock session：execute 返回 scalar count
    mock_session = AsyncMock()

    # 模拟 count 查询返回值
    call_count = 0
    mock_scalars = []

    for _ in range(4):
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0  # 无过期数据
        mock_scalars.append(mock_result)

    async def mock_execute(*args, **kwargs):
        nonlocal call_count
        result = mock_scalars[call_count]
        call_count += 1
        return result

    mock_session.execute = mock_execute

    results = await apply_retention_policy(mock_session, dry_run=True)

    assert len(results) == 4
    # 永久保留的表返回 deleted_count=0, cutoff_date=None
    for r in results:
        assert isinstance(r, RetentionResult)
        assert r.deleted_count == 0  # mock 返回 0


@pytest.mark.asyncio
async def test_retention_permanent_tables_not_cleaned() -> None:
    """永久保留的表（daily/weekly/monthly）不参与清理。"""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 0
    mock_session.execute = AsyncMock(return_value=mock_result)

    results = await apply_retention_policy(mock_session, dry_run=True)

    permanent_results = [r for r in results if r.cutoff_date is None]
    assert len(permanent_results) == 1
    permanent_names = {r.table_name for r in permanent_results}
    assert permanent_names == {"bars_daily"}


# ============================================================
# 3. reconcile_instrument 对账测试（3 维度）
# ============================================================


def test_reconcile_result_dataclass() -> None:
    """验证 ReconcileResult 数据类字段完整性。"""
    result = ReconcileResult(
        instrument_id="test-uuid",
        symbol="000001",
        period="d",
        db_count=100,
        source_count=100,
        missing_count=0,
        extra_count=0,
        mismatch_count=0,
        mismatches=[],
    )
    assert result.instrument_id == "test-uuid"
    assert result.symbol == "000001"
    assert result.period == "d"
    assert result.db_count == 100
    assert result.source_count == 100
    assert result.missing_count == 0
    assert result.extra_count == 0
    assert result.mismatch_count == 0
    assert result.mismatches == []


@pytest.mark.asyncio
async def test_reconcile_missing_data() -> None:
    """维度 1：DB 缺失（pytdx 有数据，DB 无）。"""
    from app.services.reconcile_bars import reconcile_instrument

    # mock session：DB 查询返回空
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []  # DB 无数据
    mock_session.execute = AsyncMock(return_value=mock_result)

    # mock pytdx：返回有数据
    mock_pytdx = MagicMock()
    source_dates = pd.date_range("2026-06-16", periods=3, freq="D")
    mock_pytdx.get_daily_bars.return_value = pd.DataFrame({
        "datetime": source_dates,
        "open": [10.0, 10.1, 10.2],
        "high": [10.5, 10.6, 10.7],
        "low": [9.8, 9.9, 10.0],
        "close": [10.2, 10.3, 10.4],
        "volume": [100000, 110000, 120000],
        "amount": [1000000.0, 1100000.0, 1200000.0],
    })

    import uuid as uuid_mod
    test_uuid = uuid_mod.UUID("12345678-1234-1234-1234-123456789012")

    with patch("app.services.reconcile_bars.get_pytdx_adapter", return_value=mock_pytdx):
        result = await reconcile_instrument(
            mock_session,
            instrument_id=test_uuid,
            symbol="000001",
            period="d",
            start_date=date(2026, 6, 16),
            end_date=date(2026, 6, 18),
        )

    # DB 无数据，pytdx 有 3 条 → 全部缺失
    assert result.db_count == 0
    assert result.source_count == 3
    assert result.missing_count == 3
    assert result.extra_count == 0


@pytest.mark.asyncio
async def test_reconcile_extra_data() -> None:
    """维度 2：DB 多余（DB 有数据，pytdx 无）。"""
    from app.services.reconcile_bars import reconcile_instrument

    # mock session：DB 有 3 条数据（_query_db_bars 只查 trade_date, close 两列）
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [
        (date(2026, 6, 16), Decimal("10.2")),
        (date(2026, 6, 17), Decimal("10.3")),
        (date(2026, 6, 18), Decimal("10.4")),
    ]
    mock_session.execute = AsyncMock(return_value=mock_result)

    # mock pytdx：返回空
    mock_pytdx = MagicMock()
    mock_pytdx.get_daily_bars.return_value = pd.DataFrame()

    import uuid as uuid_mod
    test_uuid = uuid_mod.UUID("12345678-1234-1234-1234-123456789012")

    with patch("app.services.reconcile_bars.get_pytdx_adapter", return_value=mock_pytdx):
        result = await reconcile_instrument(
            mock_session,
            instrument_id=test_uuid,
            symbol="000001",
            period="d",
            start_date=date(2026, 6, 16),
            end_date=date(2026, 6, 18),
        )

    # DB 有 3 条，pytdx 无 → 全部多余
    assert result.db_count == 3
    assert result.source_count == 0
    assert result.extra_count == 3
    assert result.missing_count == 0


@pytest.mark.asyncio
async def test_reconcile_mismatch() -> None:
    """维度 3：值不一致（close 差异超过 0.01）。"""
    from app.services.reconcile_bars import reconcile_instrument

    # mock session：DB 有 1 条数据，close=10.20（_query_db_bars 只查 trade_date, close 两列）
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [
        (date(2026, 6, 16), Decimal("10.20")),
    ]
    mock_session.execute = AsyncMock(return_value=mock_result)

    # mock pytdx：返回 1 条数据，close=10.50（差异 0.30 > 0.01）
    mock_pytdx = MagicMock()
    mock_pytdx.get_daily_bars.return_value = pd.DataFrame({
        "datetime": pd.to_datetime(["2026-06-16"]),
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.50],
        "volume": [100000],
        "amount": [1000000.0],
    })

    import uuid as uuid_mod
    test_uuid = uuid_mod.UUID("12345678-1234-1234-1234-123456789012")

    with patch("app.services.reconcile_bars.get_pytdx_adapter", return_value=mock_pytdx):
        result = await reconcile_instrument(
            mock_session,
            instrument_id=test_uuid,
            symbol="000001",
            period="d",
            start_date=date(2026, 6, 16),
            end_date=date(2026, 6, 16),
        )

    # DB close=10.20, pytdx close=10.50, 差异 0.30 > 0.01 → mismatch
    assert result.db_count == 1
    assert result.source_count == 1
    assert result.mismatch_count == 1
    assert len(result.mismatches) == 1
    assert result.missing_count == 0
    assert result.extra_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
