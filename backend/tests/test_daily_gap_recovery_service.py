"""R2 multi-day 日线缺口恢复 owner 单元测试（纯单元，mock session / provider）。

覆盖契约：
1. 无 gap → no-op
2. gaps 自动按日期升序
3. sparse → 不调用 bulk repair
4. market-wide → 调 bulk repair
5. reference 必须 < target（严格早于目标日）
6. 找不到完整 reference → fail closed
7. bulk residual → 调 pytdx-first sparse repair
8. residual=0 → 不做额外 provider I/O
9. 第一天失败 → 不继续修第二天
10. repair 后 continuity 仍失败 → 整体 failure（unresolved_dates 非空）
11. dry-run 不写库
12. 重跑幂等
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import daily_gap_recovery_service as rec
from app.services.daily_gap_recovery_service import (
    DailyGapRecoveryBlockedError,
    find_previous_complete_trade_date,
    recover_recent_daily_gaps,
)
from app.services.eod_daily_refresh_service import DailyGap

pytestmark = pytest.mark.pure_unit

TD = date(2026, 9, 11)
PREV = date(2026, 9, 10)
D9 = date(2026, 9, 9)


def _gap(d: date, covered: int, eligible: int = 100) -> DailyGap:
    return DailyGap(
        trade_date=d,
        covered=covered,
        eligible=eligible,
        coverage=covered / eligible,
        missing_count=eligible - covered,
        is_total_gap=covered == 0,
    )


def _mw_gap(d: date) -> DailyGap:
    """coverage=0 → market_wide_gap。"""
    return _gap(d, 0)


def _sparse_gap(d: date) -> DailyGap:
    """missing/eligible=0.05 < 0.20 → sparse_symbol_gap。"""
    return _gap(d, 95)


def _inst(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol)


class _ScalarResult:
    def __init__(self, rows: list[date]) -> None:
        self._rows = rows

    def all(self) -> list[date]:
        return list(self._rows)


def _install_market_wide(
    monkeypatch: pytest.MonkeyPatch,
    *,
    find_missing_side_effect: list[list],
    scan_before: list[DailyGap],
    scan_after: list[DailyGap],
    prev_ref: date | None = PREV,
    repair_result: SimpleNamespace | None = None,
    fill_result: int = 1,
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """安装 market-wide happy-path 依赖，返回 (prev_ref, repair, fill)。"""
    monkeypatch.setattr(
        rec, "scan_daily_continuity", AsyncMock(side_effect=[scan_before, scan_after])
    )
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    prev = AsyncMock(return_value=prev_ref)
    monkeypatch.setattr(rec, "find_previous_complete_trade_date", prev)
    monkeypatch.setattr(rec, "compare_db_vs_ths_for_date", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(rec, "validate_consistency", MagicMock())
    monkeypatch.setattr(
        rec, "find_missing_daily_instruments", AsyncMock(side_effect=find_missing_side_effect)
    )
    repair = AsyncMock(
        return_value=repair_result
        or SimpleNamespace(requested=100, fetched=99, inserted=99, failed_symbols=[])
    )
    monkeypatch.setattr(rec, "repair_market_wide_daily_gap", repair)
    fill = AsyncMock(return_value=fill_result)
    monkeypatch.setattr(rec, "fill_missing_daily_instruments", fill)
    return prev, repair, fill


# ── 1. 无 gap → no-op ────────────────────────────────────────────────
async def test_no_gap_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rec, "scan_daily_continuity", AsyncMock(return_value=[]))
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))

    res = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert res.gaps_before == []
    assert res.days == []
    assert res.is_complete


# ── 2. gaps 自动按日期升序 ───────────────────────────────────────────
async def test_gaps_processed_in_ascending_date_order(monkeypatch: pytest.MonkeyPatch) -> None:
    d1, d2 = date(2026, 9, 8), D9
    monkeypatch.setattr(
        rec,
        "scan_daily_continuity",
        AsyncMock(side_effect=[[_sparse_gap(d2), _sparse_gap(d1)], []]),
    )
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    monkeypatch.setattr(rec, "find_missing_daily_instruments", AsyncMock(return_value=[]))

    res = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert [d.trade_date for d in res.days] == [d1, d2]


# ── 3. sparse → 不调用 bulk repair ───────────────────────────────────
async def test_sparse_gap_uses_pytdx_first_not_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rec, "scan_daily_continuity", AsyncMock(side_effect=[[_sparse_gap(TD)], []])
    )
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    monkeypatch.setattr(
        rec,
        "find_missing_daily_instruments",
        AsyncMock(side_effect=[[_inst("600000")], []]),
    )
    fill = AsyncMock(return_value=1)
    monkeypatch.setattr(rec, "fill_missing_daily_instruments", fill)
    repair = AsyncMock()
    monkeypatch.setattr(rec, "repair_market_wide_daily_gap", repair)

    res = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert repair.await_count == 0
    assert fill.await_count == 1
    assert res.days[0].mode == "sparse_symbol_gap"
    assert res.days[0].sparse_attempted == 1


# ── 4 + 5. market-wide → 调 bulk repair；reference 严格早于 target ────
async def test_market_wide_gap_uses_bulk_repair_with_reference_before_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev, repair, _fill = _install_market_wide(
        monkeypatch,
        scan_before=[_mw_gap(TD)],
        scan_after=[],
        find_missing_side_effect=[[], []],
        repair_result=SimpleNamespace(requested=100, fetched=98, inserted=98, failed_symbols=[]),
    )

    res = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert repair.await_count == 1
    assert prev.await_count == 1
    # reference 必须是「严格早于目标日」的交易日
    assert prev.await_args.kwargs["before"] == TD
    assert res.days[0].mode == "market_wide_gap"
    assert res.days[0].reference_trade_date == PREV
    assert res.days[0].bulk_inserted == 98


# ── 6. 找不到完整 reference → fail closed ────────────────────────────
async def test_missing_complete_reference_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prev, repair, _fill = _install_market_wide(
        monkeypatch,
        scan_before=[_mw_gap(TD)],
        scan_after=[],
        find_missing_side_effect=[[], []],
        prev_ref=None,
    )

    with pytest.raises(DailyGapRecoveryBlockedError):
        await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert repair.await_count == 0


# ── 7. bulk residual → 调 pytdx-first sparse repair ──────────────────
async def test_bulk_residual_falls_back_to_pytdx_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prev, repair, fill = _install_market_wide(
        monkeypatch,
        scan_before=[_mw_gap(TD)],
        scan_after=[],
        find_missing_side_effect=[[_inst("600000")], []],
    )

    res = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert repair.await_count == 1
    assert fill.await_count == 1
    residual_arg = fill.await_args.args[1]
    assert [i.symbol for i in residual_arg] == ["600000"]
    assert res.days[0].sparse_attempted == 1


# ── 8. residual=0 → 不做额外 provider I/O ────────────────────────────
async def test_zero_residual_makes_no_extra_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prev, _repair, fill = _install_market_wide(
        monkeypatch,
        scan_before=[_mw_gap(TD)],
        scan_after=[],
        find_missing_side_effect=[[], []],
    )

    res = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert fill.await_count == 0
    assert res.days[0].sparse_attempted == 0


# ── 9. 第一天失败 → 不继续修第二天 ───────────────────────────────────
async def test_first_day_failure_stops_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    d1, d2 = date(2026, 9, 9), PREV
    monkeypatch.setattr(
        rec,
        "scan_daily_continuity",
        AsyncMock(return_value=[_sparse_gap(d1), _sparse_gap(d2)]),
    )
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    monkeypatch.setattr(
        rec, "find_missing_daily_instruments", AsyncMock(return_value=[_inst("600000")])
    )
    fill = AsyncMock(side_effect=RuntimeError("provider down"))
    monkeypatch.setattr(rec, "fill_missing_daily_instruments", fill)

    with pytest.raises(RuntimeError, match="provider down"):
        await recover_recent_daily_gaps(MagicMock(), through=TD)

    # 只处理了第一天；第二天未进入 provider I/O
    assert fill.await_count == 1


# ── 10. repair 后 continuity 仍失败 → 整体 failure ───────────────────
async def test_unresolved_after_repair_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rec,
        "scan_daily_continuity",
        AsyncMock(side_effect=[[_sparse_gap(TD)], [_sparse_gap(TD)]]),
    )
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    monkeypatch.setattr(
        rec,
        "find_missing_daily_instruments",
        AsyncMock(side_effect=[[_inst("600000")], [_inst("600000")]]),
    )
    monkeypatch.setattr(rec, "fill_missing_daily_instruments", AsyncMock(return_value=0))

    res = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert res.is_complete is False
    assert res.unresolved_dates == [TD]
    assert res.days[0].failed_symbols == ["600000"]


# ── 11. dry-run 不写库 ───────────────────────────────────────────────
async def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rec,
        "scan_daily_continuity",
        AsyncMock(side_effect=[[_mw_gap(TD)], [_mw_gap(TD)]]),
    )
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    monkeypatch.setattr(rec, "find_previous_complete_trade_date", AsyncMock(return_value=PREV))
    compare = AsyncMock()
    monkeypatch.setattr(rec, "compare_db_vs_ths_for_date", compare)
    validate = MagicMock()
    monkeypatch.setattr(rec, "validate_consistency", validate)
    monkeypatch.setattr(
        rec, "find_missing_daily_instruments", AsyncMock(return_value=[_inst("600000")])
    )
    repair = AsyncMock(
        return_value=SimpleNamespace(requested=100, fetched=99, inserted=0, failed_symbols=[])
    )
    monkeypatch.setattr(rec, "repair_market_wide_daily_gap", repair)
    fill = AsyncMock()
    monkeypatch.setattr(rec, "fill_missing_daily_instruments", fill)

    res = await recover_recent_daily_gaps(MagicMock(), through=TD, dry_run=True)

    # 一致性 A/B 门禁（写前只读）不执行
    assert compare.await_count == 0
    assert validate.call_count == 0
    # sparse residual 会写库 → dry-run 禁止执行
    assert fill.await_count == 0
    # bulk 以 dry_run=True 调用，且不要求 consistency report
    assert repair.await_args.kwargs["dry_run"] is True
    assert repair.await_args.kwargs["consistency_report"] is None
    assert res.is_complete is False


# ── 12. 重跑幂等 ─────────────────────────────────────────────────────
async def test_rerun_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rec,
        "scan_daily_continuity",
        AsyncMock(side_effect=[[_sparse_gap(TD)], [], [], []]),
    )
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    monkeypatch.setattr(rec, "find_missing_daily_instruments", AsyncMock(return_value=[]))
    monkeypatch.setattr(rec, "fill_missing_daily_instruments", AsyncMock(return_value=0))

    first = await recover_recent_daily_gaps(MagicMock(), through=TD)
    second = await recover_recent_daily_gaps(MagicMock(), through=TD)

    assert len(first.days) == 1
    assert second.days == []
    assert second.is_complete


# ── reference 选择：跳过不完整交易日 / 全不完整返回 None ──────────────
async def test_find_previous_complete_trade_date_skips_incomplete_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=_ScalarResult([PREV, D9]))
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    # PREV 覆盖率不足，D9 完整 → 必须选 D9
    monkeypatch.setattr(
        rec, "count_covered_daily_instruments", AsyncMock(side_effect=[50, 100])
    )

    found = await find_previous_complete_trade_date(session, before=TD)

    assert found == D9


async def test_find_previous_complete_trade_date_returns_none_when_all_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.scalars = AsyncMock(return_value=_ScalarResult([PREV, D9]))
    monkeypatch.setattr(rec, "count_active_a_share_instruments", AsyncMock(return_value=100))
    monkeypatch.setattr(rec, "count_covered_daily_instruments", AsyncMock(return_value=10))

    assert await find_previous_complete_trade_date(session, before=TD) is None


# ── pytdx adapter 注入测试 ──────────────────────────────────────────
async def test_recover_recent_daily_gaps_with_pytdx_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev, repair, _fill = _install_market_wide(
        monkeypatch,
        scan_before=[_mw_gap(TD)],
        scan_after=[],
        find_missing_side_effect=[[], []],
        repair_result=SimpleNamespace(requested=100, fetched=98, inserted=98, failed_symbols=[]),
    )
    mock_pytdx_gate = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(rec, "compare_db_vs_pytdx_for_date", mock_pytdx_gate)

    fake_adapter = object()
    res = await recover_recent_daily_gaps(MagicMock(), through=TD, adapter=fake_adapter)

    assert repair.await_count == 1
    assert repair.await_args.kwargs["adapter"] is fake_adapter
    assert mock_pytdx_gate.await_count == 1
    assert mock_pytdx_gate.await_args.kwargs["adapter"] is fake_adapter
    assert res.days[0].bulk_inserted == 98
