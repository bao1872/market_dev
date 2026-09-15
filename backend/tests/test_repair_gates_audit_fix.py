"""外部审计修复回归测试（ROUND — AUDIT FIX for fae8446a）。

锁住独立审计发现的 4 个问题 + 缺回归测试，全部纯单元（mock session / provider）：

TEST A  [P0-1] 历史 market-wide 回补门禁失败 → 向上传播，不回退 legacy 逐股 loop
TEST B  [P0-2] same-day market-wide 回补门禁失败 → 不包装成 SnapshotProviderError
TEST C  [P0-2] raw 回补后 is_consistent=False / error=None / degraded=None
              （普通 mismatch）→ 必须进入 ReconciliationPlan.items（触发 rebuild），
              不得被当「通过」直接清零
TEST D  [P0-2] raw 回补后 is_consistent=True → degraded 正确解除
TEST E  [P0-2] raw 回补后仍 degraded / error → 保持 degraded（门禁继续 fail-closed）
TEST F  [P1-5] 修复窗口用 TradingCalendar 的 previous trading day P，而非 D-10 自然日猜测

覆盖点：bars_scheduler_service 的 _run_historical_gap_repair /
_run_same_day_market_wide_repair / _repair_degraded_raw_daily_and_reaudit。
不新建 framework，纯单元 mock；生产路径无新增逻辑分支。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.bars_scheduler_service import (
    BatchResult,
    BarsSchedulerService,
    FactorIntegrityBlockedError,
)
from app.services.daily_gap_recovery_service import DailyGapRecoveryBlockedError
from app.services.daily_gap_repair_service import SourceConsistencyError
from app.services.factor_reconciliation import ReconciliationItem, ReconciliationPlan
from app.services.factor_consistency_audit import FactorConsistencyAuditor

pytestmark = pytest.mark.pure_unit

TD = date(2026, 9, 14)  # 周一
PREV = date(2026, 9, 11)  # 周五（TD 的真正前一交易日，绝非 TD-10）


def _inst(symbol: str = "000032"):
    return SimpleNamespace(id=uuid.uuid4(), symbol=symbol)


def _plan(
    degraded_symbols,
    *,
    items=None,
    consistent=0,
    errors=0,
    degraded_count=None,
):
    return ReconciliationPlan(
        items=list(items or []),
        total_audited=len(degraded_symbols) + consistent + errors + len(items or []),
        consistent_count=consistent,
        error_count=errors,
        degraded_count=degraded_count if degraded_count is not None else len(degraded_symbols),
        degraded_symbols=list(degraded_symbols),
    )


class _FakeAudit:
    """模拟 FactorConsistencyAuditor.audit_single_stock 的返回值。"""

    def __init__(
        self,
        *,
        degraded_reason=None,
        missing_event_dates=(),
        error=None,
        is_consistent=True,
    ) -> None:
        self.degraded_reason = degraded_reason
        self.missing_event_dates = tuple(missing_event_dates)
        self.error = error
        self.is_consistent = is_consistent


def _patch_parts(monkeypatch, *, audit_degraded, prev_trading=PREV, post_plan):
    """集中打桩 Part C 的依赖。"""
    monkeypatch.setattr(
        FactorConsistencyAuditor,
        "audit_single_stock",
        AsyncMock(return_value=_FakeAudit(degraded_reason="bars_daily_gap", missing_event_dates=(TD,))),
    )
    # fetch_ths_raw_daily：返回一条当日 bar，并记录调用参数供 TEST F 校验
    captured = {}

    async def _fetch(client, symbol, start, end, **kw):
        captured["start"] = start
        captured["end"] = end
        return [{"datetime": TD.isoformat(), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1}]

    monkeypatch.setattr(
        "app.services.ths_raw_daily_provider.fetch_ths_raw_daily", _fetch,
    )
    monkeypatch.setattr(
        "app.services.daily_gap_repair_service.bulk_insert_raw_daily_repair",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        AsyncMock(return_value=prev_trading),
    )
    monkeypatch.setattr(
        "app.services.factor_reconciliation.FactorReconciliationTask.dry_run",
        AsyncMock(return_value=post_plan),
    )
    return captured


# ============================================================================
# TEST A [P0-1] 历史 market-wide 回补门禁失败 → 向上传播，不回退 legacy
# ============================================================================

@pytest.mark.asyncio
async def test_historical_recovery_blocked_propagates_not_legacy(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    result = BatchResult()

    async def _boom(*a, **k):
        raise DailyGapRecoveryBlockedError("reference missing")

    monkeypatch.setattr(
        "app.services.daily_gap_recovery_service.recover_recent_daily_gaps", _boom,
    )

    with pytest.raises(DailyGapRecoveryBlockedError):
        await svc._run_historical_gap_repair(MagicMock(), TD, result)

    # 绝不能落到 legacy_fallback（绕过 repair owner 数据合同）
    assert result.daily_mode != "legacy_fallback"


@pytest.mark.asyncio
async def test_historical_recovery_source_consistency_propagates(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    result = BatchResult()

    async def _boom(*a, **k):
        raise SourceConsistencyError("consistency failed")

    monkeypatch.setattr(
        "app.services.daily_gap_recovery_service.recover_recent_daily_gaps", _boom,
    )

    with pytest.raises(SourceConsistencyError):
        await svc._run_historical_gap_repair(MagicMock(), TD, result)


# ============================================================================
# TEST B [P0-2] same-day market-wide 回补门禁失败 → 不包装成 SnapshotProviderError
# ============================================================================

@pytest.mark.asyncio
async def test_sameday_recovery_source_consistency_not_snapshot_error(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    result = BatchResult()

    async def _boom(*a, **k):
        raise SourceConsistencyError("consistency failed")

    monkeypatch.setattr(
        "app.services.daily_gap_recovery_service.recover_recent_daily_gaps", _boom,
    )

    # 必须是原来的 SourceConsistencyError 向上传播，而不是被包成 SnapshotProviderError
    with pytest.raises(SourceConsistencyError):
        await svc._run_same_day_market_wide_repair(MagicMock(), TD, eligible=100, adapter=None, result=result)


@pytest.mark.asyncio
async def test_sameday_recovery_low_coverage_raises_daily_gap_blocked(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    result = BatchResult()

    # recover 成功但回补后覆盖率不足 → 应以 DailyGapRecoveryBlockedError 传播
    monkeypatch.setattr(
        "app.services.daily_gap_recovery_service.recover_recent_daily_gaps",
        AsyncMock(return_value=SimpleNamespace(days=[])),
    )
    # 仍有 50/100 未覆盖 → coverage=0.5 < 0.90
    monkeypatch.setattr(
        "app.services.eod_daily_refresh_service.find_missing_daily_instruments",
        AsyncMock(return_value=[SimpleNamespace(symbol=f"{i:06d}") for i in range(50)]),
    )

    with pytest.raises(DailyGapRecoveryBlockedError):
        await svc._run_same_day_market_wide_repair(MagicMock(), TD, eligible=100, adapter=None, result=result)


# ============================================================================
# TEST C [P0-2] raw 回补后普通 mismatch → 进入 items（触发 rebuild）
# ============================================================================

@pytest.mark.asyncio
async def test_repair_post_mismatch_enters_items_not_cleared(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    inst = _inst()
    instruments = [inst]
    plan = _plan(["000032"])  # 初始 degraded

    post_item = ReconciliationItem(
        instrument_id=inst.id, symbol="000032",
        earliest_affected=TD, before_hash="x",
        mismatch_count=1, reason="value_mismatch",
    )
    # 回补后变为 mismatch（is_consistent=False / error=None / degraded=None）
    post = _plan([], items=[post_item])

    _patch_parts(monkeypatch, audit_degraded=True, post_plan=post)

    out = await svc._repair_degraded_raw_daily_and_reaudit(plan, TD, MagicMock(), instruments)

    # 关键：degraded 解除且 mismatch 进入 items，触发后续 rebuild
    assert out.degraded_count == 0
    assert out.degraded_symbols == []
    assert len(out.items) == 1
    assert out.items[0].symbol == "000032"


# ============================================================================
# TEST D [P0-2] raw 回补后 consistent → degraded 正确解除
# ============================================================================

@pytest.mark.asyncio
async def test_repair_post_consistent_releases_degraded(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    inst = _inst()
    instruments = [inst]
    plan = _plan(["000032"])

    # 回补后变为 consistent
    post = _plan([], consistent=1)
    _patch_parts(monkeypatch, audit_degraded=True, post_plan=post)

    out = await svc._repair_degraded_raw_daily_and_reaudit(plan, TD, MagicMock(), instruments)

    assert out.degraded_count == 0
    assert out.degraded_symbols == []
    # 一致的不应进 items（无需 rebuild）
    assert out.items == []


# ============================================================================
# TEST E [P0-2] raw 回补后仍 degraded / error → 保持 fail-closed
# ============================================================================

@pytest.mark.asyncio
async def test_repair_post_still_degraded_keeps_blocking(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    inst = _inst()
    instruments = [inst]
    plan = _plan(["000032"])

    # 回补后仍 degraded（根因未消除）
    post = _plan(["000032"], degraded_count=1)
    _patch_parts(monkeypatch, audit_degraded=True, post_plan=post)

    out = await svc._repair_degraded_raw_daily_and_reaudit(plan, TD, MagicMock(), instruments)

    # 不得清零放行，门禁（调用方 FactorIntegrityBlockedError）继续生效
    assert out.degraded_count == 1
    assert out.degraded_symbols == ["000032"]


@pytest.mark.asyncio
async def test_repair_post_error_keeps_blocking(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    inst = _inst()
    instruments = [inst]
    plan = _plan(["000032"])

    # 回补后审计 error
    post = _plan([], errors=1)
    _patch_parts(monkeypatch, audit_degraded=True, post_plan=post)

    out = await svc._repair_degraded_raw_daily_and_reaudit(plan, TD, MagicMock(), instruments)

    assert out.degraded_count == 1


# ============================================================================
# TEST F [P1-5] 修复窗口 = TradingCalendar 的 previous trading day [P, D]
# ============================================================================

@pytest.mark.asyncio
async def test_repair_window_uses_previous_trading_day(monkeypatch):
    svc = BarsSchedulerService(fetch_processes=1)
    inst = _inst()
    instruments = [inst]
    plan = _plan(["000032"])

    post = _plan([], consistent=1)
    captured = _patch_parts(monkeypatch, audit_degraded=True, prev_trading=PREV, post_plan=post)

    await svc._repair_degraded_raw_daily_and_reaudit(plan, TD, MagicMock(), instruments)

    # 起点必须是 TradingCalendar 的 P（TD 前一交易日），绝不能是 TD-10 自然日猜测
    assert captured["start"] == PREV
    assert captured["end"] == TD
    assert captured["start"] != TD - timedelta(days=10)
