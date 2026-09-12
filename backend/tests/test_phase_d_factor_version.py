"""[CP-V3-D] Phase D 因子版本字段写入与影响集识别测试。

验证 DEVELOP.md §4.1 要求："写入已有instrument因子版本字段，并验证下次盘后影响集能够识别。"

覆盖：
1. stamp_factor_reconciliation_version 正确写入 3 个字段
2. find_stale_version_instruments 识别 NULL（从未对账）的股票
3. find_stale_version_instruments 识别版本不匹配的股票
4. 已 stamp 当前版本的股票不被识别为 stale
5. 非 active 股票不被识别（即使版本 stale）
6. 部分股票已 stamp、部分 NULL 时只返回 NULL/stale 的

约束：
- 使用测试 DB（db_session fixture，事务性回滚）
- 不依赖生产数据
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from app.constants.factor_contract import (
    FACTOR_ALGORITHM_VERSION,
    FACTOR_RECONCILIATION_VERSION,
)
from app.models.instrument import Instrument
from app.services.factor_consistency_audit import FactorAuditResult
from app.services.factor_reconciliation import (
    FactorReconciliationTask,
    find_stale_version_instruments,
    stamp_factor_reconciliation_version,
    stamp_factor_reconciliation_versions_by_symbols,
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


# =============================================================================
# 测试 1: stamp 正确写入 3 个字段
# =============================================================================


@pytest.mark.asyncio
async def test_d_stamp_writes_all_three_fields(db_session) -> None:
    """[CP-V3-D] stamp_factor_reconciliation_version 写入算法版本/对账版本/对账时间。"""
    instrument_id = await _create_instrument(db_session, symbol="STAMP01")

    # 初始为 NULL
    fields = await _get_factor_version_fields(db_session, instrument_id)
    assert fields["factor_algorithm_version"] is None
    assert fields["factor_reconciliation_version"] is None
    assert fields["factor_reconciled_at"] is None

    # stamp
    await stamp_factor_reconciliation_version(db_session, instrument_id)

    # 验证写入
    db_session.expire_all()
    fields = await _get_factor_version_fields(db_session, instrument_id)
    assert fields["factor_algorithm_version"] == FACTOR_ALGORITHM_VERSION
    assert fields["factor_reconciliation_version"] == FACTOR_RECONCILIATION_VERSION
    assert fields["factor_reconciled_at"] is not None
    # 时间应为近期（写入后 5 秒内）
    now = datetime.now(UTC)
    stamped = fields["factor_reconciled_at"]
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    delta = abs((now - stamped).total_seconds())
    assert delta < 5, f"factor_reconciled_at 应为近期，实际 delta={delta}s"


# =============================================================================
# 测试 2: find 识别 NULL（从未对账）的股票
# =============================================================================


@pytest.mark.asyncio
async def test_d_find_identifies_null_version_instruments(db_session) -> None:
    """[CP-V3-D] find_stale_version_instruments 识别 NULL 版本的股票。"""
    instr_id = await _create_instrument(db_session, symbol="NULL01")

    stale = await find_stale_version_instruments(db_session)
    stale_ids = [item[0] for item in stale]
    assert instr_id in stale_ids


# =============================================================================
# 测试 3: find 识别版本不匹配的股票
# =============================================================================


@pytest.mark.asyncio
async def test_d_find_identifies_mismatched_version(db_session) -> None:
    """[CP-V3-D] find_stale_version_instruments 识别版本不匹配的股票。"""
    # 旧算法版本
    instr_old_algo = await _create_instrument(
        db_session,
        symbol="OLDALG01",
        factor_algorithm_version="fq-v0",  # 旧版本
        factor_reconciliation_version=FACTOR_RECONCILIATION_VERSION,
        factor_reconciled_at=datetime.now(UTC),
    )
    # 旧对账版本
    instr_old_recon = await _create_instrument(
        db_session,
        symbol="OLDREC01",
        factor_algorithm_version=FACTOR_ALGORITHM_VERSION,
        factor_reconciliation_version=0,  # 旧版本
        factor_reconciled_at=datetime.now(UTC),
    )

    stale = await find_stale_version_instruments(db_session)
    stale_ids = [item[0] for item in stale]
    assert instr_old_algo in stale_ids, "旧算法版本应被识别"
    assert instr_old_recon in stale_ids, "旧对账版本应被识别"


# =============================================================================
# 测试 4: 已 stamp 当前版本的股票不被识别
# =============================================================================


@pytest.mark.asyncio
async def test_d_current_version_not_stale(db_session) -> None:
    """[CP-V3-D] 已 stamp 当前版本的股票不在 stale 列表中。"""
    instr_current = await _create_instrument(
        db_session,
        symbol="CURRENT01",
        factor_algorithm_version=FACTOR_ALGORITHM_VERSION,
        factor_reconciliation_version=FACTOR_RECONCILIATION_VERSION,
        factor_reconciled_at=datetime.now(UTC),
    )

    stale = await find_stale_version_instruments(db_session)
    stale_ids = [item[0] for item in stale]
    assert instr_current not in stale_ids, "当前版本不应被识别为 stale"


# =============================================================================
# 测试 5: 非 active 股票不被识别（即使版本 stale）
# =============================================================================


@pytest.mark.asyncio
async def test_d_inactive_instruments_excluded(db_session) -> None:
    """[CP-V3-D] 非 active 股票不被 find_stale_version_instruments 识别。"""
    instr_inactive = await _create_instrument(
        db_session,
        symbol="INACT01",
        status="delisted",  # 非活跃
        factor_algorithm_version=None,  # NULL 版本
    )

    stale = await find_stale_version_instruments(db_session)
    stale_ids = [item[0] for item in stale]
    assert instr_inactive not in stale_ids, "非 active 股票不应被识别"


# =============================================================================
# 测试 6: 混合场景——只返回 stale/NULL 的
# =============================================================================


@pytest.mark.asyncio
async def test_d_mixed_scenario_only_returns_stale(db_session) -> None:
    """[CP-V3-D] 混合场景：3 只 NULL + 2 只 stale + 2 只 current → 只返回 5 只。"""
    # 3 只 NULL
    null_ids = [
        await _create_instrument(db_session, symbol=f"MIX_NULL_{i}")
        for i in range(3)
    ]
    # 2 只 stale（旧版本）
    stale_ids = [
        await _create_instrument(
            db_session,
            symbol=f"MIX_STALE_{i}",
            factor_algorithm_version="fq-v0",
            factor_reconciliation_version=0,
            factor_reconciled_at=datetime.now(UTC),
        )
        for i in range(2)
    ]
    # 2 只 current
    current_ids = [
        await _create_instrument(
            db_session,
            symbol=f"MIX_CURR_{i}",
            factor_algorithm_version=FACTOR_ALGORITHM_VERSION,
            factor_reconciliation_version=FACTOR_RECONCILIATION_VERSION,
            factor_reconciled_at=datetime.now(UTC),
        )
        for i in range(2)
    ]

    stale = await find_stale_version_instruments(db_session)
    stale_ids_returned = {item[0] for item in stale}

    # NULL + stale 应全部返回（5 只）
    for nid in null_ids:
        assert nid in stale_ids_returned, f"NULL instrument {nid} 应在 stale 列表中"
    for sid in stale_ids:
        assert sid in stale_ids_returned, f"stale instrument {sid} 应在 stale 列表中"

    # current 不应返回
    for cid in current_ids:
        assert cid not in stale_ids_returned, (
            f"current instrument {cid} 不应在 stale 列表中"
        )

    # 总数 = 3 NULL + 2 stale = 5
    assert len(stale_ids_returned) == 5, (
        f"应返回 5 只 stale，实际 {len(stale_ids_returned)}"
    )


# =============================================================================
# [G1B-3A] 影响集识别强化 + dry_run 批量查询 + 批量 stamp
# =============================================================================


async def test_g1b3a_find_stale_identifies_null_reconciled_at(db_session) -> None:
    """[G1B-3A] 版本号正确但 factor_reconciled_at IS NULL → 视为 stale。

    有版本号但从未留下成功对账时间，不能视为 baseline 已证明。
    """
    instr = await _create_instrument(
        db_session,
        symbol="NULLRECON01",
        factor_algorithm_version=FACTOR_ALGORITHM_VERSION,
        factor_reconciliation_version=FACTOR_RECONCILIATION_VERSION,
        factor_reconciled_at=None,  # 关键：从未留下成功对账时间
    )

    stale = await find_stale_version_instruments(db_session)
    stale_ids = [item[0] for item in stale]
    assert instr in stale_ids, "有版本号但无 reconciled_at 应视为 stale"


async def test_g1b3a_dry_run_symbols_single_instrument_query(db_session) -> None:
    """[G1B-3A] dry_run(symbols=...) 只发起 1 次 Instrument SELECT，而非 N 次。

    100 只指定股票 → 单次批量查询 instrument 身份，audit 仍逐股串行。
    """
    syms = [f"{600000 + i:06d}" for i in range(100)]
    for s in syms:
        db_session.add(Instrument(symbol=s, name=s, market="SH", status="active"))
    await db_session.flush()

    # mock auditor：返回一致，不连 pytdx / DB 其他表
    task = FactorReconciliationTask()
    task._auditor = MagicMock()
    task._auditor.audit_single_stock = AsyncMock(
        return_value=FactorAuditResult(
            instrument_id=uuid.uuid4(),
            symbol="x",
            is_consistent=True,
            stored_count=1,
            expected_count=1,
            missing_factor_count=0,
        )
    )

    # spy on session.execute：统计查询次数
    calls: list[object] = []
    orig_execute = db_session.execute

    async def _spy(*args, **kwargs):
        calls.append(args[0])
        return await orig_execute(*args, **kwargs)

    db_session.execute = _spy  # type: ignore[assignment]

    plan = await task.dry_run(db_session, symbols=syms, batch_size=50, max_mismatches=20)

    # 100 只全部审计到、全部一致
    assert plan.total_audited == 100
    assert plan.consistent_count == 100
    # 关键：只发起了 1 次 session.execute（单次批量 Instrument SELECT）
    assert len(calls) == 1, f"期望仅 1 次查询，实际 {len(calls)} 次"


async def test_g1b3a_dry_run_symbols_missing_not_found(db_session) -> None:
    """[G1B-3A] symbols 中含不存在股票 → total_audited 少于 requested。

    scheduler 层据此 FACTOR_AUDIT_SCOPE_INCOMPLETE fail-closed（见
    test_bars_scheduler_factor_audit）。
    """
    await _create_instrument(db_session, symbol="EXIST01")

    task = FactorReconciliationTask()
    task._auditor = MagicMock()
    task._auditor.audit_single_stock = AsyncMock(
        return_value=FactorAuditResult(
            instrument_id=uuid.uuid4(),
            symbol="x",
            is_consistent=True,
            stored_count=1,
            expected_count=1,
            missing_factor_count=0,
        )
    )

    plan = await task.dry_run(
        db_session,
        symbols=["EXIST01", "GHOST99"],
        batch_size=50,
        max_mismatches=20,
    )
    # 只有 EXIST01 被审计到，GHOST99 未找到
    assert plan.total_audited == 1


async def test_g1b3a_stamp_symbols_dedup_and_no_commit(db_session) -> None:
    """[G1B-3A] stamp_factor_reconciliation_versions_by_symbols 去重 + 不 commit。

    去重 symbols；只 flush 不 commit（由调用方事务拥有）。
    """
    sym_a = "STAMPA01"
    sym_b = "STAMPB02"
    await _create_instrument(db_session, symbol=sym_a)
    await _create_instrument(db_session, symbol=sym_b)

    # 监控 commit 是否在本函数内被调用
    commit_spy = MagicMock()
    db_session.commit = commit_spy  # type: ignore[assignment]

    # 含重复 symbol
    stamped = await stamp_factor_reconciliation_versions_by_symbols(
        db_session, [sym_a, sym_b, sym_a],
    )
    assert stamped == 2, f"去重后应 stamp 2 只，实际 {stamped}"

    # 不 commit（仅 flush，由调用方统一提交）
    commit_spy.assert_not_called()

    # flush 后同会话内可见更新
    db_session.expire_all()
    fields_a = await _get_factor_version_fields(db_session, (
        await _get_id_by_symbol(db_session, sym_a)
    ))
    assert fields_a["factor_algorithm_version"] == FACTOR_ALGORITHM_VERSION
    assert fields_a["factor_reconciled_at"] is not None


async def _get_id_by_symbol(db_session, symbol: str) -> uuid.UUID:
    """辅助：按 symbol 查 instrument id。"""
    from sqlalchemy import select as _select

    result = await db_session.execute(
        _select(Instrument.id).where(Instrument.symbol == symbol)
    )
    return result.scalar_one()
