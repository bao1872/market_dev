"""[CP-V3-D2] 安全 bootstrap 测试：基于已有审计证据写入版本基线。

验证 PROMPT.md §3 要求："设计并实现安全 bootstrap：
- 已有审计证据确认一致的股票可只写版本基线，不重复重算；
- 无充分证据的股票按小批次审计；
- 只有确认不一致的股票进入重建；
- 每批可恢复、可停止、有限流；
- 不得在本轮修改生产 DB。"

覆盖：
1. bootstrap 对 consistent 且 NULL 的股票写入版本基线
2. bootstrap 跳过 needs_rebuild 股票（不写入版本基线）
3. bootstrap 跳过 degraded 股票
4. bootstrap 幂等性（已写入的不会重复写入）
5. bootstrap 分批 commit（batch_size 控制）
6. bootstrap 传入 dry_run_plan 时不内部调用 dry_run

约束：
- 使用测试 DB（db_session fixture，事务性回滚）
- 不依赖生产数据
- 不调用真实 dry_run（构造 mock ReconciliationPlan）
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.constants.factor_contract import (
    FACTOR_ALGORITHM_VERSION,
    FACTOR_RECONCILIATION_VERSION,
)
from app.services.factor_reconciliation import (
    ReconciliationItem,
    ReconciliationPlan,
    bootstrap_factor_version_baseline,
    find_stale_version_instruments,
)


async def _create_instrument(
    db_session,
    *,
    symbol: str,
    status: str = "active",
    factor_algorithm_version: str | None = None,
    factor_reconciliation_version: int | None = None,
    factor_reconciled_at: datetime | None = None,
) -> uuid.UUID:
    """创建测试 instrument 并返回其 id。"""
    from app.models.instrument import Instrument

    instrument = Instrument(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        market="SZ",
        status=status,
        factor_algorithm_version=factor_algorithm_version,
        factor_reconciliation_version=factor_reconciliation_version,
        factor_reconciled_at=factor_reconciled_at,
    )
    db_session.add(instrument)
    await db_session.flush()
    return instrument.id


async def _get_factor_version_fields(db_session, instrument_id) -> dict:
    """读取 instrument 的 3 个因子版本字段。"""
    result = await db_session.execute(
        text(
            "SELECT factor_algorithm_version, factor_reconciliation_version, "
            "factor_reconciled_at FROM instruments WHERE id = :id"
        ),
        {"id": instrument_id},
    )
    row = result.first()
    return {
        "factor_algorithm_version": row.factor_algorithm_version,
        "factor_reconciliation_version": row.factor_reconciliation_version,
        "factor_reconciled_at": row.factor_reconciled_at,
    }


def _make_dry_run_plan(
    *,
    needs_rebuild_symbols: list[str] | None = None,
    degraded_symbols: list[str] | None = None,
    total_audited: int = 100,
    consistent_count: int = 90,
) -> ReconciliationPlan:
    """构造 mock ReconciliationPlan（避免实际调用 dry_run）。"""
    items: list[ReconciliationItem] = []
    for sym in (needs_rebuild_symbols or []):
        items.append(
            ReconciliationItem(
                instrument_id=uuid.uuid4(),
                symbol=sym,
                earliest_affected=datetime(2026, 1, 1).date(),
                before_hash="hash_placeholder",
                mismatch_count=10,
                reason="value_mismatch",
            )
        )
    return ReconciliationPlan(
        items=items,
        total_audited=total_audited,
        consistent_count=consistent_count,
        error_count=0,
        degraded_count=len(degraded_symbols or []),
        degraded_symbols=degraded_symbols or [],
    )


# =============================================================================
# 测试 1: bootstrap 对 consistent 且 NULL 的股票写入版本基线
# =============================================================================


@pytest.mark.asyncio
async def test_d2_bootstrap_stamps_consistent_null(db_session) -> None:
    """[CP-V3-D2] consistent 且 NULL 的股票被写入版本基线。"""
    # 创建 3 只 NULL 版本的 active 股票
    id1 = await _create_instrument(db_session, symbol="BOOT01")
    id2 = await _create_instrument(db_session, symbol="BOOT02")
    id3 = await _create_instrument(db_session, symbol="BOOT03")

    # 构造 dry_run_plan：无 needs_rebuild，无 degraded，3 只全 consistent
    plan = _make_dry_run_plan(
        needs_rebuild_symbols=[],
        degraded_symbols=[],
        total_audited=3,
        consistent_count=3,
    )

    result = await bootstrap_factor_version_baseline(
        db_session, dry_run_plan=plan, batch_size=10,
    )

    assert result["total_null"] == 3
    assert result["stamped"] == 3
    assert result["skipped_needs_rebuild"] == 0
    assert result["skipped_degraded"] == 0
    assert result["errors"] == 0

    # 验证写入
    db_session.expire_all()
    for instrument_id in (id1, id2, id3):
        fields = await _get_factor_version_fields(db_session, instrument_id)
        assert fields["factor_algorithm_version"] == FACTOR_ALGORITHM_VERSION
        assert fields["factor_reconciliation_version"] == FACTOR_RECONCILIATION_VERSION
        assert fields["factor_reconciled_at"] is not None


# =============================================================================
# 测试 2: bootstrap 跳过 needs_rebuild 股票
# =============================================================================


@pytest.mark.asyncio
async def test_d2_bootstrap_skips_needs_rebuild(db_session) -> None:
    """[CP-V3-D2] needs_rebuild 股票不写入版本基线（由 after-close 重建后 stamp）。"""
    # 创建 2 只 NULL 版本的 active 股票
    id_consistent = await _create_instrument(db_session, symbol="CONS01")
    id_needs_rebuild = await _create_instrument(db_session, symbol="NEEDS01")

    # 构造 dry_run_plan：NEEDS01 在 needs_rebuild 列表中
    plan = _make_dry_run_plan(
        needs_rebuild_symbols=["NEEDS01"],
        degraded_symbols=[],
        total_audited=2,
        consistent_count=1,
    )

    result = await bootstrap_factor_version_baseline(
        db_session, dry_run_plan=plan, batch_size=10,
    )

    assert result["total_null"] == 2
    assert result["stamped"] == 1  # 只有 CONS01 被写入
    assert result["skipped_needs_rebuild"] == 1  # NEEDS01 跳过
    assert result["skipped_degraded"] == 0

    # 验证 CONS01 已写入，NEEDS01 仍为 NULL
    db_session.expire_all()
    cons_fields = await _get_factor_version_fields(db_session, id_consistent)
    assert cons_fields["factor_algorithm_version"] == FACTOR_ALGORITHM_VERSION

    needs_fields = await _get_factor_version_fields(db_session, id_needs_rebuild)
    assert needs_fields["factor_algorithm_version"] is None


# =============================================================================
# 测试 3: bootstrap 跳过 degraded 股票
# =============================================================================


@pytest.mark.asyncio
async def test_d2_bootstrap_skips_degraded(db_session) -> None:
    """[CP-V3-D2] degraded 股票不写入版本基线（数据缺口，需先回补数据）。"""
    await _create_instrument(db_session, symbol="CONS02")
    id_degraded = await _create_instrument(db_session, symbol="DEGR01")

    plan = _make_dry_run_plan(
        needs_rebuild_symbols=[],
        degraded_symbols=["DEGR01"],
        total_audited=2,
        consistent_count=1,
    )

    result = await bootstrap_factor_version_baseline(
        db_session, dry_run_plan=plan, batch_size=10,
    )

    assert result["total_null"] == 2
    assert result["stamped"] == 1
    assert result["skipped_needs_rebuild"] == 0
    assert result["skipped_degraded"] == 1

    db_session.expire_all()
    degr_fields = await _get_factor_version_fields(db_session, id_degraded)
    assert degr_fields["factor_algorithm_version"] is None


# =============================================================================
# 测试 4: bootstrap 幂等性（已写入的不会重复写入）
# =============================================================================


@pytest.mark.asyncio
async def test_d2_bootstrap_idempotent(db_session) -> None:
    """[CP-V3-D2] 已写入版本基线的股票不会重复写入（WHERE factor_algorithm_version IS NULL）。"""
    # 创建 1 只已写入版本基线的股票，2 只 NULL
    id_stamped = await _create_instrument(
        db_session,
        symbol="STAMPED01",
        factor_algorithm_version=FACTOR_ALGORITHM_VERSION,
        factor_reconciliation_version=FACTOR_RECONCILIATION_VERSION,
        factor_reconciled_at=datetime.now(UTC),
    )
    await _create_instrument(db_session, symbol="NULL001")
    await _create_instrument(db_session, symbol="NULL002")

    plan = _make_dry_run_plan(
        needs_rebuild_symbols=[],
        degraded_symbols=[],
        total_audited=3,
        consistent_count=3,
    )

    result = await bootstrap_factor_version_baseline(
        db_session, dry_run_plan=plan, batch_size=10,
    )

    # total_null 只统计 NULL 的股票（2 只），stamped 的不在 total_null 中
    assert result["total_null"] == 2
    assert result["stamped"] == 2

    # 验证 STAMPED01 的版本字段未被覆盖（仍是原值）
    db_session.expire_all()
    stamped_fields = await _get_factor_version_fields(db_session, id_stamped)
    assert stamped_fields["factor_algorithm_version"] == FACTOR_ALGORITHM_VERSION


# =============================================================================
# 测试 5: bootstrap 分批 commit（batch_size 控制）
# =============================================================================


@pytest.mark.asyncio
async def test_d2_bootstrap_batch_commit(db_session) -> None:
    """[CP-V3-D2] batch_size 控制每批写入数量，分批 commit。"""
    # 创建 5 只 NULL 版本的 active 股票
    ids = []
    for i in range(5):
        ids.append(await _create_instrument(db_session, symbol=f"BATCH{i:02d}"))

    plan = _make_dry_run_plan(
        needs_rebuild_symbols=[],
        degraded_symbols=[],
        total_audited=5,
        consistent_count=5,
    )

    # batch_size=2，应分 3 批（2+2+1）
    result = await bootstrap_factor_version_baseline(
        db_session, dry_run_plan=plan, batch_size=2,
    )

    assert result["total_null"] == 5
    assert result["stamped"] == 5

    # 验证全部写入
    db_session.expire_all()
    for instrument_id in ids:
        fields = await _get_factor_version_fields(db_session, instrument_id)
        assert fields["factor_algorithm_version"] == FACTOR_ALGORITHM_VERSION


# =============================================================================
# 测试 6: bootstrap 传入 dry_run_plan 时不内部调用 dry_run
# =============================================================================


@pytest.mark.asyncio
async def test_d2_bootstrap_uses_provided_plan(db_session) -> None:
    """[CP-V3-D2] 传入 dry_run_plan 时不内部调用 dry_run（避免重复审计）。"""
    await _create_instrument(db_session, symbol="PLAN01")

    plan = _make_dry_run_plan(
        needs_rebuild_symbols=[],
        degraded_symbols=[],
        total_audited=1,
        consistent_count=1,
    )

    # patch FactorReconciliationTask.dry_run 验证不被调用
    with patch(
        "app.services.factor_reconciliation.FactorReconciliationTask.dry_run",
        new_callable=AsyncMock,
    ) as mock_dry_run:
        result = await bootstrap_factor_version_baseline(
            db_session, dry_run_plan=plan, batch_size=10,
        )
        mock_dry_run.assert_not_called()

    assert result["stamped"] == 1
    assert result["dry_run_total_audited"] == 1


# =============================================================================
# 测试 7: bootstrap 后 find_stale 只识别版本变化的影响集
# =============================================================================


@pytest.mark.asyncio
async def test_d2_bootstrap_reduces_stale_set(db_session) -> None:
    """[CP-V3-D2] bootstrap 后 find_stale_version_instruments 不再返回 NULL 股票。

    验证 bootstrap 的核心价值：将 8272 NULL 缩减为只识别真实版本变化的影响集。
    """
    # 创建 3 只 NULL 股票
    await _create_instrument(db_session, symbol="STALE01")
    await _create_instrument(db_session, symbol="STALE02")
    await _create_instrument(db_session, symbol="STALE03")

    # bootstrap 前：3 只全为 stale（NULL）
    stale_before = await find_stale_version_instruments(db_session)
    stale_symbols_before = {s[1] for s in stale_before}
    assert stale_symbols_before == {"STALE01", "STALE02", "STALE03"}

    # bootstrap
    plan = _make_dry_run_plan(
        needs_rebuild_symbols=[],
        degraded_symbols=[],
        total_audited=3,
        consistent_count=3,
    )
    await bootstrap_factor_version_baseline(
        db_session, dry_run_plan=plan, batch_size=10,
    )

    # bootstrap 后：0 只 stale（全部已写入当前版本）
    stale_after = await find_stale_version_instruments(db_session)
    assert len(stale_after) == 0
