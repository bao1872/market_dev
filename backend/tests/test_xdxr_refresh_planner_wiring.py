"""G1B-3B2: XDXR planner 接入 AfterClose 的纯单元测试（wiring G-P）。

不连真实 Redis / DB / pytdx：用 fake adj_service / fake session / patched calendar。
验证 evidence-based refresh-set 在 legacy / bootstrap / stable / mismatch / missing /
due / stale / calendar-missing / MGET-outage 下都正确，且 detect 调用合同不变。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.services.adjustment_factor_service import CorporateActionScheduleState
from app.services.bars_scheduler_service import BarsSchedulerService
from app.services.xdxr_refresh_planner import rotation_bucket

TD = date(2026, 9, 11)
PREV_TD = date(2026, 9, 10)


@dataclass
class _FakeInstrument:
    id: uuid.UUID
    symbol: str


class _FakeAdjService:
    """fake AdjustmentFactorService：记录 detect 调用 + 返回脚本化 schedule。"""

    def __init__(self, schedule_states: dict[uuid.UUID, CorporateActionScheduleState | None]):
        self._schedule_states = schedule_states
        self.detect_calls: list[tuple[str, bool, object]] = []
        self.schedule_calls = 0

    def get_corporate_action_schedule_states(
        self, instrument_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, CorporateActionScheduleState | None]:
        self.schedule_calls += 1
        return {iid: self._schedule_states.get(iid) for iid in instrument_ids}

    async def detect_company_action_change(
        self, session, instrument_id: uuid.UUID, symbol: str, adapter,
        *, force_refresh: bool = False, effective_as_of=None,
    ) -> None:
        self.detect_calls.append((symbol, force_refresh, effective_as_of))
        return None


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def scalars(self) -> _Rows:
        return self


class _FakeSession:
    """fake AsyncSession：按 SQL 文本路由 prior-close / calendar 查询并计数。"""

    def __init__(
        self, *, prior_close_rows, calendar_dates, calendar_count, calendar_trade_date,
    ) -> None:
        self._prior_close_rows = prior_close_rows
        self._calendar_dates = calendar_dates
        self._calendar_count = calendar_count
        self._calendar_trade_date = calendar_trade_date
        self.execute_calls = 0
        self.scalar_calls = 0
        self.scalars_calls = 0

    async def execute(self, stmt):
        self.execute_calls += 1
        sql = str(stmt).lower()
        if "bars_daily" in sql:
            return _Rows(self._prior_close_rows)
        if "trading_calendar" in sql:
            return _Rows(self._calendar_dates)
        raise AssertionError(f"unexpected execute: {sql[:200]}")

    async def scalar(self, stmt):
        self.scalar_calls += 1
        sql = str(stmt).lower()
        if "count(" in sql:
            return self._calendar_count
        return self._calendar_trade_date

    async def scalars(self, stmt):
        self.scalars_calls += 1
        return _Rows(self._calendar_dates)


def _build_instruments(n: int, start: int = 600000) -> list[_FakeInstrument]:
    return [
        _FakeInstrument(uuid.uuid4(), f"{start + i:06d}")
        for i in range(n)
    ]


async def _run_rebuild(
    instruments: list[_FakeInstrument],
    eod: dict[str, Decimal | None] | None,
    schedule_states: dict[uuid.UUID, CorporateActionScheduleState | None],
    prior_close_rows: list[tuple[uuid.UUID, Decimal]],
    prev_trade=PREV_TD,
    cal_trade=TD,
    cal_count=3,
    cal_dates=None,
) -> tuple[dict, _FakeAdjService, _FakeSession]:
    if cal_dates is None:
        cal_dates = [date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]
    fake_adj = _FakeAdjService(schedule_states)
    fake_session = _FakeSession(
        prior_close_rows=prior_close_rows,
        calendar_dates=cal_dates,
        calendar_count=cal_count,
        calendar_trade_date=cal_trade,
    )
    service = BarsSchedulerService()
    with (
        patch(
            "app.services.adjustment_factor_service.AdjustmentFactorService",
            return_value=fake_adj,
        ),
        patch(
            "app.services.bars_scheduler_service.get_pytdx_adapter",
            return_value=MagicMock(),
        ),
        patch(
            "app.services.calendar_service.get_previous_trading_day_async",
            return_value=prev_trade,
        ),
    ):
        result = await service._rebuild_factors_if_needed(
            TD, instruments, db_session=fake_session,
            eod_previous_close_by_symbol=eod,
        )
    return result, fake_adj, fake_session


# =============================================================================
# G. legacy（evidence=None）→ 全市场 refresh，且 0 次 schedule MGET / 0 次 SQL
# =============================================================================


async def test_legacy_no_evidence_full_refresh() -> None:
    instruments = _build_instruments(6)
    result, fake_adj, fake_session = await _run_rebuild(
        instruments,
        eod=None,  # legacy fallback
        schedule_states={},
        prior_close_rows=[],
    )
    assert result["planner_mode"] == "legacy_full_refresh"
    assert result["refresh_requested"] == 6
    assert result["skipped_fresh"] == 0
    assert len(fake_adj.detect_calls) == 6
    # legacy 路径不读 schedule MGET / prior-close SQL / calendar coordinate
    assert fake_adj.schedule_calls == 0
    assert fake_session.execute_calls == 0
    for _symbol, force_refresh, effective_as_of in fake_adj.detect_calls:
        assert force_refresh is True
        assert effective_as_of == TD


# =============================================================================
# H. first bootstrap：snapshot evidence 有，schedule 全 None → 全市场 refresh
# =============================================================================


async def test_first_bootstrap_schedule_all_unknown() -> None:
    instruments = _build_instruments(6)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    # schedule 全 None（首次部署 Redis 为空）
    result, fake_adj, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states={}, prior_close_rows=prior,
    )
    assert result["planner_mode"] == "eod_previous_close"
    assert result["refresh_requested"] == 6
    assert result["refresh_reason_counts"].get("schedule_unknown") == 6
    assert len(fake_adj.detect_calls) == 6


# =============================================================================
# I. stable second day：age=1 + previous_close 相等 → 仅 rotation 命中
# =============================================================================


async def test_stable_second_day_only_rotation() -> None:
    instruments = _build_instruments(9)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    schedule_states = {
        inst.id: CorporateActionScheduleState(
            scanned_as_of=date(2026, 9, 10), next_event_date=None,
        )
        for inst in instruments
    }
    result, fake_adj, fake_session = await _run_rebuild(
        instruments, eod=eod, schedule_states=schedule_states, prior_close_rows=prior,
        cal_count=3,  # ordinal = 2 → 命中 bucket 2
    )
    expected = {
        inst.symbol for inst in instruments
        if rotation_bucket(inst.symbol, 3) == 2
    }
    refreshed = {c[0] for c in fake_adj.detect_calls}
    assert refreshed == expected
    assert result["refresh_requested"] == len(expected)
    # prior-close 仅 1 次批量 SQL
    assert fake_session.execute_calls == 1
    for reason in ("schedule_stale", "previous_close_mismatch", "known_event_due"):
        assert reason not in result["refresh_reason_counts"]


# =============================================================================
# J. previous-close mismatch（非 rotation）→ 必须 refresh
# =============================================================================


async def test_previous_close_mismatch_refreshes() -> None:
    instruments = _build_instruments(9)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    schedule_states = {
        inst.id: CorporateActionScheduleState(
            scanned_as_of=date(2026, 9, 10), next_event_date=None,
        )
        for inst in instruments
    }
    # 选一个非 rotation symbol 制造 mismatch
    target = next(
        inst for inst in instruments if rotation_bucket(inst.symbol, 3) != 2
    )
    eod[target.symbol] = Decimal("11.00")
    prior_map = {
        inst.id: c for inst, c in zip(instruments, [Decimal("10.00")] * 9, strict=True)
    }
    prior = [(inst.id, prior_map[inst.id]) for inst in instruments]
    result, fake_adj, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states=schedule_states, prior_close_rows=prior,
        cal_count=3,
    )
    assert target.symbol in {c[0] for c in fake_adj.detect_calls}
    assert result["refresh_reason_counts"].get("previous_close_mismatch", 0) >= 1


# =============================================================================
# K. prior close missing（DB 无昨日 close）→ UNKNOWN → refresh
# =============================================================================


async def test_prior_close_missing_unknown_refreshes() -> None:
    instruments = _build_instruments(9)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    schedule_states = {
        inst.id: CorporateActionScheduleState(
            scanned_as_of=date(2026, 9, 10), next_event_date=None,
        )
        for inst in instruments
    }
    target = next(
        inst for inst in instruments if rotation_bucket(inst.symbol, 3) != 2
    )
    # prior_close_rows 故意不含 target
    prior = [
        (inst.id, Decimal("10.00"))
        for inst in instruments if inst is not target
    ]
    result, fake_adj, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states=schedule_states, prior_close_rows=prior,
        cal_count=3,
    )
    assert target.symbol in {c[0] for c in fake_adj.detect_calls}
    assert result["refresh_reason_counts"].get("previous_close_unknown", 0) >= 1


# =============================================================================
# L. known event due（next_event_date <= trade_date）→ refresh
# =============================================================================


async def test_known_event_due_refreshes() -> None:
    instruments = _build_instruments(9)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    schedule_states = {
        inst.id: CorporateActionScheduleState(
            scanned_as_of=date(2026, 9, 10), next_event_date=None,
        )
        for inst in instruments
    }
    target = next(
        inst for inst in instruments if rotation_bucket(inst.symbol, 3) != 2
    )
    # 已知未来事件已到期
    schedule_states[target.id] = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 10), next_event_date=TD,
    )
    result, fake_adj, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states=schedule_states, prior_close_rows=prior,
        cal_count=3,
    )
    assert target.symbol in {c[0] for c in fake_adj.detect_calls}
    assert result["refresh_reason_counts"].get("known_event_due", 0) >= 1


# =============================================================================
# M. schedule stale（age=3）→ refresh
# =============================================================================


async def test_schedule_stale_refreshes() -> None:
    instruments = _build_instruments(9)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    schedule_states = {
        inst.id: CorporateActionScheduleState(
            scanned_as_of=date(2026, 9, 10), next_event_date=None,
        )
        for inst in instruments
    }
    target = next(
        inst for inst in instruments if rotation_bucket(inst.symbol, 3) != 0
    )
    # 旧 schedule 已 3 个交易日未更新
    schedule_states[target.id] = CorporateActionScheduleState(
        scanned_as_of=date(2026, 9, 8), next_event_date=None,
    )
    # cal_dates 覆盖 9-8..9-11，count=4 → ordinal=3
    cal_dates = [
        date(2026, 9, 8), date(2026, 9, 9),
        date(2026, 9, 10), date(2026, 9, 11),
    ]
    result, fake_adj, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states=schedule_states, prior_close_rows=prior,
        cal_count=4, cal_dates=cal_dates,
    )
    assert target.symbol in {c[0] for c in fake_adj.detect_calls}
    assert result["refresh_reason_counts"].get("schedule_stale", 0) >= 1


# =============================================================================
# N. calendar T 日缺失 → ordinal=None → 全部 refresh（trade_day_ordinal_invalid）
# =============================================================================


async def test_calendar_trade_date_missing_all_refresh() -> None:
    instruments = _build_instruments(6)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    schedule_states = {
        inst.id: CorporateActionScheduleState(
            scanned_as_of=date(2026, 9, 10), next_event_date=None,
        )
        for inst in instruments
    }
    result, fake_adj, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states=schedule_states, prior_close_rows=prior,
        cal_trade=None,  # T 日不在 TradingCalendar
        cal_count=0,
        cal_dates=[],
    )
    assert result["refresh_requested"] == 6
    assert result["refresh_reason_counts"].get("trade_day_ordinal_invalid") == 6


# =============================================================================
# O. MGET outage → schedule 全 None → 全部 refresh
# =============================================================================


async def test_mget_outage_all_refresh() -> None:
    instruments = _build_instruments(6)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    # schedule_states 空（模拟 MGET 全 None 安全退化）
    result, _, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states={}, prior_close_rows=prior,
    )
    assert result["refresh_requested"] == 6


# =============================================================================
# P. detect 调用合同：refresh 集 force_refresh=True + effective_as_of=trade_date
# =============================================================================


async def test_detect_call_contract_refresh_set() -> None:
    instruments = _build_instruments(9)
    eod = {inst.symbol: Decimal("10.00") for inst in instruments}
    prior = [(inst.id, Decimal("10.00")) for inst in instruments]
    schedule_states = {
        inst.id: CorporateActionScheduleState(
            scanned_as_of=date(2026, 9, 10), next_event_date=None,
        )
        for inst in instruments
    }
    result, fake_adj, _ = await _run_rebuild(
        instruments, eod=eod, schedule_states=schedule_states, prior_close_rows=prior,
        cal_count=3,
    )
    refreshed = {c[0] for c in fake_adj.detect_calls}
    assert refreshed == {
        inst.symbol for inst in instruments
        if rotation_bucket(inst.symbol, 3) == 2
    }
    for _symbol, force_refresh, effective_as_of in fake_adj.detect_calls:
        assert force_refresh is True
        assert effective_as_of == TD
    # skipped symbol 不被 detect
    all_symbols = {inst.symbol for inst in instruments}
    assert all_symbols - refreshed  # 存在 skipped


# =============================================================================
# 性能门禁：calendar / prior-close SQL 调用数不随股票数增长
# =============================================================================


async def test_calendar_queries_fixed_vs_instruments() -> None:
    cal_trade = TD
    cal_count = 4
    cal_dates = [
        date(2026, 9, 8), date(2026, 9, 9),
        date(2026, 9, 10), date(2026, 9, 11),
    ]
    service = BarsSchedulerService()
    counts = []
    for n in (9, 50):
        instruments = _build_instruments(n)
        eod = {inst.symbol: Decimal("10.00") for inst in instruments}
        prior = [(inst.id, Decimal("10.00")) for inst in instruments]
        # schedule key 匹配 instruments[0].id，确保 range 查询被触发
        schedule_states = {
            instruments[0].id: CorporateActionScheduleState(
                scanned_as_of=date(2026, 9, 10), next_event_date=None,
            )
        }
        fake_adj = _FakeAdjService(schedule_states)
        sess = _FakeSession(
            prior_close_rows=prior, calendar_dates=cal_dates,
            calendar_count=cal_count, calendar_trade_date=cal_trade,
        )
        with patch(
            "app.services.calendar_service.get_previous_trading_day_async",
            return_value=PREV_TD,
        ):
            await service._plan_xdxr_refresh_set(
                trade_date=TD, instruments=instruments,
                eod_previous_close_by_symbol=eod, adj_service=fake_adj,
                session=sess,
            )
        counts.append((sess.execute_calls, sess.scalar_calls, sess.scalars_calls))
    # N=9 与 N=50：prior-close 1 次 execute；calendar 2 次 scalar + 1 次 scalars
    assert counts[0] == counts[1] == (1, 2, 1)
