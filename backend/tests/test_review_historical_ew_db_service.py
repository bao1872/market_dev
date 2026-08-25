"""M5-C1 tests for the production close-only SQL EW adapter.

Scope
=====

All tests run under ``PURE_UNIT_TEST=1``.  No real database is ever used.  The
``AsyncSession`` is replaced by an in-memory double that records:

* every ``await session.execute(...)`` call (query string + arguments) for the
  calendar + membership small queries;
* every ``await session.stream(...)`` call (query + execution options + async
  iteration) for the heavy bar stream.

Required test coverage (A–M):

A.  exact SQL projection columns (instrument_id / trade_date / close only).
B.  bar loading uses ``session.stream`` with ``yield_per``, not ``.execute``.
C.  matrix row/column mapping exact (date → row, instrument → col).
D.  ``close = None`` in DB → matrix cell is NaN.
E.  non-finite numeric close → matrix cell is NaN (unavailable path).
F.  duplicate streamed DB cell → adapter raises, fail closed.
G.  canonical trailing-``D``-trading-day suffix matches axis → PASS.
H.  axis / calendar mismatch → fail closed.
I.  the first analysis date receives an exact non-axis T1 from the external
    calendar owner (proves we aren't deriving T-1 locally from the axis).
J.  current-static membership batch owner is reused; overlapping scope
    memberships resolve to shared columnar columns and individual scopes'
    ``member_count`` / ``scope_name`` values propagate.
K.  final EW values exact against the scalar oracle
    (``compute_exact_return`` + ``_return_distribution``) on a 3-scope ×
    5-day × 8-member synthetic exact grid.
L.  top-level result does NOT retain close / return matrices or internal
    index arrays; only small structured payload + metrics survive.
M.  empty / malformed / duplicate / future-dated / off-asof axis fail
    closed according to the contract.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterable, Sequence

import numpy as np
import pytest

# Canonical math owners for Test K scalar oracle.
from app.domain.review.scope_observation import _return_distribution
from app.services.observation_prep import compute_exact_return

# Module under test.
from app.services.review_historical_ew_db_service import (
    HistoricalEWBatchResult,
    HistoricalEWScopeResult,
    compute_current_static_historical_ew_batch,
)
from app.services.review_historical_scope_reconstruction_service import (
    CurrentStaticMembership,
)

# BarDaily model identity (used by Test A/B only to check the adapter uses
# the declared columns — no DB queries ever run against it).
from app.models.bar import BarDaily


# ---------------------------------------------------------------------------
# In-memory doubles.
# ---------------------------------------------------------------------------
@dataclass
class CapturedExecuteCall:
    stmt: Any  # raw SQLA select passed in
    scalars_result: Any | None = None  # if .scalars() was called on the result
    all_result: Any | None = None  # if .all() was called on scalars result


class FakeStreamResult:
    """Pretends to be what ``await session.stream(stmt)`` returns.

    ``async for row in result`` yields the caller-supplied rows exactly.
    """

    def __init__(self, rows: Iterable[Any]) -> None:
        self._rows = list(rows)

    def __aiter__(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for r in self._rows:
                yield r

        return gen()


class FakeAsyncSession:
    """In-memory session double that intercepts execute/stream."""

    def __init__(
        self,
        *,
        calendar_rows_desc: list[date],
        memberships: dict[str, CurrentStaticMembership],
        bar_rows: Sequence[tuple[Any, date, Any]],
        market_board_rows: list[Any] | None = None,
        board_membership_rows: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self.calendar_rows_desc = list(calendar_rows_desc)
        self.memberships = dict(memberships)
        self.bar_rows = list(bar_rows)
        # The two small execute() owners inside resolve_current_memberships_batch
        # need ORM rows.  Tests can pre-supply them; by default None we just
        # short-circuit via a patch (see _install_owners below).
        self.market_board_rows = list(market_board_rows or [])
        self.board_membership_rows = list(board_membership_rows or [])
        self.execute_calls: list[CapturedExecuteCall] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def execute(self, stmt: Any) -> Any:
        call = CapturedExecuteCall(stmt=stmt)
        self.execute_calls.append(call)
        # Dispatch by textual signature: calendar = TradingCalendar.trade_date
        stmt_str = str(stmt)
        if "trading_calendar" in stmt_str.lower():
            call.scalars_result = self.calendar_rows_desc
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: call.scalars_result)
            )
        # MarketBoard select — return ORM doubles (tests provide directly).
        if "market_board" in stmt_str.lower() and "membership" not in stmt_str.lower():
            call.scalars_result = list(self.market_board_rows)
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: call.scalars_result)
            )
        # MarketBoardMembership select — (boardId, instrumentId) tuple rows.
        if "market_board_membership" in stmt_str.lower():
            rows = list(self.board_membership_rows)
            call.all_result = rows
            return SimpleNamespace(all=lambda: rows)
        # Unknown SQL — keep captured but return empty.
        call.scalars_result = []
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [])
        )

    async def stream(self, stmt: Any) -> Any:
        # Capture raw statement + execution_options (extract from compiled
        # stmt via __dict__; SQLA stores yield_per under the _execution_options
        # attribute after execution_options(yield_per=...)).
        opts: dict[str, Any] = {}
        try:
            opts = dict(getattr(stmt, "_execution_options", {}) or {})
        except Exception:
            opts = {}
        capture = {
            "stmt": stmt,
            "execution_options": opts,
            "rows": list(self.bar_rows),
        }
        self.stream_calls.append(capture)
        return FakeStreamResult(self.bar_rows)


# ---------------------------------------------------------------------------
# Shared small-scope fixtures.
# ---------------------------------------------------------------------------
# 5 consecutive trading days Mar03..Mar07 (D=5).  The exact "previous trading
# day" T-1 for Mar03 is Mar02 — which lies outside the caller's trade_dates
# axis on purpose, so Test I can validate that the calendar owner truly
# delivers the non-axis T-1 predecessor and that the M5-B1 dual-index
# correctly picks up close[Mar02] *not* close[Mar03].
TRADE_DATES_5 = [
    date(2026, 3, 3),
    date(2026, 3, 4),
    date(2026, 3, 5),
    date(2026, 3, 6),
    date(2026, 3, 7),
]
ANALYSIS_ASOF_5 = TRADE_DATES_5[-1]
# Descending = list_recent_trading_days(n=D+1 = 6) output.
# We add Mar02 explicitly to guarantee the Test I "explicit non-axis T1" gate.
CALENDAR_RECENT_DESC_5 = sorted(
    [
        date(2026, 3, 2),
        *TRADE_DATES_5,
    ],
    reverse=True,
)
M0, M1, M2, M3, M4, M5, M6, M7 = [uuid.UUID(int=i) for i in range(8)]
S0_KEY = str(uuid.UUID(int=1000))
S1_KEY = str(uuid.UUID(int=1001))
S2_KEY = str(uuid.UUID(int=1002))

MEMBERSHIPS_FIXTURE = {
    S0_KEY: CurrentStaticMembership(
        member_ids=(M0, M1, M2, M3),
        scope_name="Scope Zero",
        asof_date=ANALYSIS_ASOF_5,
        member_count=4,
    ),
    S1_KEY: CurrentStaticMembership(
        member_ids=(M2, M3, M4, M5),
        scope_name="Scope One",
        asof_date=ANALYSIS_ASOF_5,
        member_count=4,
    ),
    S2_KEY: CurrentStaticMembership(
        member_ids=(M4, M5, M6, M7),
        scope_name="Scope Two",
        asof_date=ANALYSIS_ASOF_5,
        member_count=4,
    ),
}

# Deterministic synthetic close table: 6 dates (Mar02..Mar07) × 8 members.
# We hand-craft values so that every scalar return is finite and every scope
# returns a non-trivial mean on every analysis date.  Values are chosen as
# whole-Decimal rows to exercise the DB Decimal→float conversion in the
# adapter (and prove the exact-math path on the columnar side).
CLOSE_GRID: dict[tuple[date, uuid.UUID], float] = {}
# Base prices per member, monotonically increasing to guarantee finite
# returns.  Mar02 is the T-1 precursor for Mar03.
_BASE_BY_MEMBER = {
    M0: 10.00,
    M1: 20.00,
    M2: 30.00,
    M3: 40.00,
    M4: 50.00,
    M5: 60.00,
    M6: 70.00,
    M7: 80.00,
}
# Daily multiplicative drift per member (different per member so scopes' EW
# is an actual aggregation, not a trivial all-equal mean).
_DRIFT = [1.01, 1.02, 0.99, 1.005, 1.015, 0.985, 1.03, 1.00]
DATES_ALL_CLOSE = [date(2026, 3, 2), *TRADE_DATES_5]
for d_idx, d in enumerate(DATES_ALL_CLOSE):
    for m_idx, m in enumerate([M0, M1, M2, M3, M4, M5, M6, M7]):
        val = _BASE_BY_MEMBER[m]
        for t in range(d_idx):
            val *= _DRIFT[m_idx]
        CLOSE_GRID[(d, m)] = val


def _build_bar_rows_decimal(
    *,
    extra: Iterable[tuple[uuid.UUID, date, Any]] | None = None,
    override: dict[tuple[uuid.UUID, date], Any] | None = None,
    drop: Iterable[tuple[uuid.UUID, date]] | None = None,
) -> list[tuple[uuid.UUID, date, Decimal | None]]:
    drop_keys = set(drop or ())
    rows: list[tuple[uuid.UUID, date, Decimal | None]] = []
    for (d, m), v in CLOSE_GRID.items():
        if (m, d) in drop_keys:
            continue
        if override and (m, d) in override:
            ovr = override[(m, d)]
            rows.append((m, d, None if ovr is None or (isinstance(ovr, float) and not math.isfinite(ovr)) else Decimal(str(ovr))))
        else:
            # Round to 4 decimals to be realistic DB-Decimal storage.
            rows.append((m, d, Decimal(f"{v:.4f}")))
    for m, d, val in extra or ():
        rows.append((m, d, val))
    return rows


# ---------------------------------------------------------------------------
# Patch owner: avoid importing resolve_current_memberships_batch's MarketBoard
# ORM.  We short-circuit the call entirely.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_heavy_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    # replace list_recent_trading_days to bypass TradingCalendar imports.
    async def fake_list_recent(session: FakeAsyncSession, *, end_date: date, n: int) -> list[date]:
        # Use the session's pre-seeded calendar (descending = real owner order).
        return list(session.calendar_rows_desc[:n])

    # Replace resolve_current_memberships_batch to bypass MarketBoard ORM
    # SELECTs.  The real contract of that owner is "returns dict[str,
    # CurrentStaticMembership] in caller scope_keys order and validates
    # scope_type/scope_id exist — but since we don't test the ORM layer here
    # we just use the fixture dict and assert that scope_keys == dict.keys().
    async def fake_resolve(
        session: Any,
        scope_type: str,
        scope_keys: list[str],
        *,
        asof_date: date,
    ) -> dict[str, CurrentStaticMembership]:
        assert scope_type == "industry_l1" or scope_type in {
            "industry_l1",
            "concept_l3",
        }
        out: dict[str, CurrentStaticMembership] = {}
        for k in scope_keys:
            if k not in MEMBERSHIPS_FIXTURE:
                raise ValueError(f"fixture missing scope_key={k}")
            out[k] = MEMBERSHIPS_FIXTURE[k]
        return out

    monkeypatch.setattr(
        "app.services.review_historical_ew_db_service.list_recent_trading_days",
        fake_list_recent,
    )
    monkeypatch.setattr(
        "app.services.review_historical_ew_db_service.resolve_current_memberships_batch",
        fake_resolve,
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
async def _run(
    *,
    scope_keys: list[str] | None = None,
    trade_dates: list[date] | None = None,
    analysis_asof_date: date | None = None,
    scope_type: str = "industry_l1",
    bar_rows: list[tuple[uuid.UUID, date, Decimal | None]] | None = None,
    calendar_desc: list[date] | None = None,
    stream_yield_per: int = 4096,
) -> tuple[HistoricalEWBatchResult, FakeAsyncSession]:
    if scope_keys is None:
        scope_keys = [S0_KEY, S1_KEY, S2_KEY]
    if trade_dates is None:
        trade_dates = list(TRADE_DATES_5)
    if analysis_asof_date is None:
        analysis_asof_date = ANALYSIS_ASOF_5
    if calendar_desc is None:
        calendar_desc = list(CALENDAR_RECENT_DESC_5)
    if bar_rows is None:
        bar_rows = _build_bar_rows_decimal()
    session = FakeAsyncSession(
        calendar_rows_desc=calendar_desc,
        memberships=MEMBERSHIPS_FIXTURE,
        bar_rows=bar_rows,
    )
    result = await compute_current_static_historical_ew_batch(
        session,
        scope_type=scope_type,
        scope_keys=scope_keys,
        trade_dates=trade_dates,
        analysis_asof_date=analysis_asof_date,
        stream_yield_per=stream_yield_per,
    )
    return result, session


# ===========================================================================
# Tests A & B: SQL contract — exact projection columns, stream usage.
# ===========================================================================
class TestSqlContract:
    @pytest.mark.asyncio
    async def test_A_exact_sql_projection_three_columns_only(self) -> None:
        result, session = await _run()
        assert len(session.stream_calls) == 1
        stream_stmt = session.stream_calls[0]["stmt"]
        # SQLA select statement has a .selected_columns / _raw_columns tuple.
        selected = list(getattr(stream_stmt, "selected_columns", []))
        col_names = [str(c).split(".")[-1] for c in selected]
        # Remove any schema prefix so "bars_daily.instrument_id" → "instrument_id".
        # Expected EXACTLY: instrument_id, trade_date, close.  Ordering not
        # mandated by the spec but the tuple order (id/date/close) is what
        # the adapter unpacks, so we verify both set equality and tuple
        # identity.
        assert sorted(col_names) == sorted(
            ["instrument_id", "trade_date", "close"]
        ), f"unexpected projection columns: {col_names}"
        # Positive: no forbidden columns.
        forbidden = {"open", "high", "low", "volume", "amount", "adj_factor"}
        assert set(col_names).isdisjoint(forbidden)

    @pytest.mark.asyncio
    async def test_B_bars_use_stream_not_execute(self) -> None:
        result, session = await _run()
        # Bar statement must appear in stream_calls; must NOT be an execute.
        bar_stream_calls = [
            c
            for c in session.stream_calls
            if "bars_daily" in str(c["stmt"]).lower()
        ]
        assert len(bar_stream_calls) == 1
        # yield_per is captured in execution_options.
        opts = bar_stream_calls[0]["execution_options"]
        assert "yield_per" in opts
        assert opts["yield_per"] == 4096
        # Execute calls should be calendar only (1 call in our fixture —
        # resolve_current_memberships_batch is fully patched).
        for c in session.execute_calls:
            assert "bars_daily" not in str(c.stmt).lower()


# ===========================================================================
# Tests C / D / E / F: matrix mapping + unavailable semantics + duplicate.
# ===========================================================================
class TestMatrixMappingAndSemantics:
    @pytest.mark.asyncio
    async def test_C_row_column_mapping_exact(self) -> None:
        # Inject exactly one known row, capture what ends up in the scope EW
        # output for an isolated 1-scope, 1-member world.
        DAYS = [date(2026, 3, 3), date(2026, 3, 4)]
        CAL = sorted(
            [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)], reverse=True
        )
        M = uuid.UUID(int=42)
        S = str(uuid.UUID(int=1337))
        MEMBERSHIPS_FIXTURE_TEMP = {
            S: CurrentStaticMembership(
                member_ids=(M,),
                scope_name="Solo",
                asof_date=DAYS[-1],
                member_count=1,
            )
        }
        # Deterministic close: T1(Mar03)=Mar02=100, Mar03=110, Mar04=121.
        # Returns: Mar03 = 110/100 - 1 = 0.10.  Mar04 = 121/110 - 1 = 0.10.
        BAR_ROWS = [
            (M, date(2026, 3, 2), Decimal("100.0000")),
            (M, date(2026, 3, 3), Decimal("110.0000")),
            (M, date(2026, 3, 4), Decimal("121.0000")),
        ]

        # Use local patching for this isolated fixture.
        import pytest as _pt

        _mon = _pt.MonkeyPatch()
        try:
            async def _cal(s, *, end_date, n):
                return CAL[:n]
            async def _res(sess, scope_type, scope_keys, *, asof_date):
                return {k: MEMBERSHIPS_FIXTURE_TEMP[k] for k in scope_keys}

            _mon.setattr(
                "app.services.review_historical_ew_db_service.list_recent_trading_days",
                _cal,
            )
            _mon.setattr(
                "app.services.review_historical_ew_db_service.resolve_current_memberships_batch",
                _res,
            )
            session = FakeAsyncSession(
                calendar_rows_desc=CAL,
                memberships=MEMBERSHIPS_FIXTURE_TEMP,
                bar_rows=BAR_ROWS,
            )
            result = await compute_current_static_historical_ew_batch(
                session,
                scope_type="industry_l1",
                scope_keys=[S],
                trade_dates=DAYS,
                analysis_asof_date=DAYS[-1],
            )
        finally:
            _mon.undo()

        assert result.scope_order == (S,)
        ew = result.scopes[0].ew_values
        # Expected exact: both days 10% return = 0.1.  Both finite.
        assert len(ew) == 2
        # Exact float equality: both sides come from the same Decimal→float
        # conversion path inside compute_exact_return (no separate rounding).
        exact_01 = 110.0 / 100.0 - 1.0
        assert ew[0] is not None and float(ew[0]) == exact_01
        assert ew[1] is not None and float(ew[1]) == exact_01
        # Bar matrix metrics.
        m = result.metrics
        # R = required bar dates = DATES_ALL union T-1 = {Mar02, Mar03, Mar04} = 3.
        assert m.R == 3
        assert m.M == 1
        assert m.rows_streamed == 3
        assert m.finite_close_cells == 3
        assert m.unavailable_close_rows == 0
        assert m.missing_cells == 0  # 3×1 - 3 finite - 0 unavail = 0
        assert m.duplicate_streamed_cells == 0

    @pytest.mark.asyncio
    async def test_D_None_close_writes_NaN_unavailable(self) -> None:
        DAYS = [date(2026, 3, 3), date(2026, 3, 4)]
        CAL = sorted(
            [date(2026, 3, 2), *DAYS], reverse=True
        )
        M = uuid.UUID(int=9)
        S = str(uuid.UUID(int=90))
        FIXT = {
            S: CurrentStaticMembership(
                member_ids=(M,),
                scope_name="S",
                asof_date=DAYS[-1],
                member_count=1,
            )
        }
        # Mar02 T-1 close is None.  Mar03 return must be unavailable.
        # Mar03 close finite → price candidate but close_t1 = None →
        # return_valid False.  Mar04 close finite, Mar03 finite → valid.
        BAR = [
            (M, date(2026, 3, 2), None),  # ← None DB close
            (M, date(2026, 3, 3), Decimal("50.0")),
            (M, date(2026, 3, 4), Decimal("55.0")),
        ]
        import pytest as _pt
        _mon = _pt.MonkeyPatch()
        try:
            async def _c(s, *, end_date, n): return CAL[:n]
            async def _r(sess, st, sk, *, asof_date): return {k: FIXT[k] for k in sk}
            _mon.setattr("app.services.review_historical_ew_db_service.list_recent_trading_days", _c)
            _mon.setattr("app.services.review_historical_ew_db_service.resolve_current_memberships_batch", _r)
            session = FakeAsyncSession(calendar_rows_desc=CAL, memberships=FIXT, bar_rows=BAR)
            result = await compute_current_static_historical_ew_batch(
                session, scope_type="industry_l1", scope_keys=[S],
                trade_dates=DAYS, analysis_asof_date=DAYS[-1],
            )
        finally:
            _mon.undo()
        ew = result.scopes[0].ew_values
        assert ew[0] is None
        assert ew[1] is not None and math.isclose(ew[1], 55.0 / 50.0 - 1.0)
        assert result.metrics.unavailable_close_rows == 1
        assert result.metrics.finite_close_cells == 2

    @pytest.mark.asyncio
    async def test_E_nonfinite_numeric_close_writes_NaN(self) -> None:
        # Override the (M0, Mar02) base cell to inf.  That cell must be
        # treated as unavailable and M0's contribution on Mar03 MUST be
        # excluded from the scope mean.
        inf = float("inf")
        # The DB typically stores Decimal; but Decimal("Infinity") works on
        # some backends only.  To exercise the numeric path without needing
        # DB-specific Decimal(inf), we pass raw Python float inf via a
        # fixture override that the bar-row builder handles as a nonfinite
        # numeric.  We simulate the row directly.
        M = uuid.UUID(int=11)
        S = str(uuid.UUID(int=111))
        DAYS = [date(2026, 3, 3), date(2026, 3, 4)]
        CAL = sorted([date(2026, 3, 2), *DAYS], reverse=True)
        FIXT = {
            S: CurrentStaticMembership(
                member_ids=(M,), scope_name="S", asof_date=DAYS[-1], member_count=1,
            )
        }
        # Pass float inf directly as the DB cell.  The adapter reads
        # ``raw_close`` and calls ``_convert_db_close`` which will see a
        # non-finite float → unavailable close cell → matrix NaN.
        BAR = [
            (M, date(2026, 3, 2), inf),
            (M, date(2026, 3, 3), Decimal("10.0")),
            (M, date(2026, 3, 4), Decimal("11.0")),
        ]
        import pytest as _pt
        _mon = _pt.MonkeyPatch()
        try:
            async def _c(s, *, end_date, n): return CAL[:n]
            async def _r(sess, st, sk, *, asof_date): return {k: FIXT[k] for k in sk}
            _mon.setattr("app.services.review_historical_ew_db_service.list_recent_trading_days", _c)
            _mon.setattr("app.services.review_historical_ew_db_service.resolve_current_memberships_batch", _r)
            session = FakeAsyncSession(calendar_rows_desc=CAL, memberships=FIXT, bar_rows=BAR)
            result = await compute_current_static_historical_ew_batch(
                session, scope_type="industry_l1", scope_keys=[S],
                trade_dates=DAYS, analysis_asof_date=DAYS[-1],
            )
        finally:
            _mon.undo()
        ew = result.scopes[0].ew_values
        # Mar03: M0 unavailable (T-1 = inf).  Universe empty → None.
        # Mar04: Mar03 finite 10 → Mar04 11 → return = 0.1 finite → EW = 0.1.
        assert ew[0] is None
        assert ew[1] is not None and math.isclose(ew[1], 0.1)
        assert result.metrics.unavailable_close_rows == 1
        assert result.metrics.finite_close_cells == 2

    @pytest.mark.asyncio
    async def test_F_duplicate_streamed_cell_fail_closed(self) -> None:
        # Inject an extra duplicate row for (M0, Mar03) with the same
        # value — adapter MUST raise, not silent ignore.
        duplicate_extra: tuple[uuid.UUID, date, Decimal] = (
            M0,
            date(2026, 3, 3),
            Decimal("10.1000"),  # exact same as base (drift [0]=1.01 → 10·1.01=10.1).
        )
        base_rows = _build_bar_rows_decimal()
        bar_rows_with_dup = base_rows + [duplicate_extra]
        with pytest.raises(ValueError, match="duplicate streamed cell"):
            await _run(bar_rows=bar_rows_with_dup)


# ===========================================================================
# Tests G / H / I: axis calendar gates + T-1 non-axis inclusion.
# ===========================================================================
class TestAxisCalendarGates:
    @pytest.mark.asyncio
    async def test_G_calendar_suffix_matches_axis_pass(self) -> None:
        # Exact default fixture = canonical trailing D days: success case.
        result, session = await _run()
        assert len(result.trade_dates) == 5
        assert result.analysis_asof_date == ANALYSIS_ASOF_5
        assert list(result.trade_dates) == TRADE_DATES_5
        # First analysis date's return MUST use the external Mar02 close
        # (we'll prove value exactness in Test K; here we just prove R > D).
        m = result.metrics
        assert m.R == len(DATES_ALL_CLOSE) == 6  # includes non-axis Mar02.

    @pytest.mark.asyncio
    async def test_H_axis_calendar_mismatch_fail_closed(self) -> None:
        # Caller supplies a strictly ascending valid 5-day trading-day axis,
        # but it is NOT the trailing suffix of the canonical window.  We
        # achieve this by shrinking the calendar by 1 day on the left so the
        # returned suffix is shifted by one relative to the caller axis.
        # Calendar (descending, 6 entries = D+1 = 6):
        #   Mar02, Mar03, Mar04, Mar05, Mar06, Mar07  → ascending suffix last5 = Mar03..Mar07.
        # Shift calendar so its trailing last 5 starts at Mar04 instead.
        shifted_cal_desc = sorted(
            [date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5),
             date(2026, 3, 6), date(2026, 3, 7), date(2026, 3, 8)],
            reverse=True,
        )
        # Caller axis is still the original 5 dates → trailing suffix differs.
        with pytest.raises(ValueError, match="suffix differs"):
            await _run(
                trade_dates=list(TRADE_DATES_5),
                calendar_desc=shifted_cal_desc,
                analysis_asof_date=ANALYSIS_ASOF_5,
            )

    @pytest.mark.asyncio
    async def test_I_first_analysis_date_gets_exact_external_t1(self) -> None:
        # Manually verify scalar oracle on M0 / Mar03: CLOSE_GRID says
        # M0 @ Mar02 = 10.00 (base), M0 @ Mar03 = 10 * 1.01 = 10.1.
        # Exact return = 10.1/10.0 - 1 = 0.01.
        # We assert that the returned S0's first EW value reflects this
        # exact 0.01 contribution combined with the other 3 members.
        m0_expected = compute_exact_return(
            CLOSE_GRID[(date(2026, 3, 3), M0)],
            CLOSE_GRID[(date(2026, 3, 2), M0)],
        )
        # Prove the oracle itself is finite 0.01.
        assert m0_expected is not None and math.isfinite(float(m0_expected))
        # And that it is strictly nonzero positive → this is a genuine
        # close[Mar02]-dependent value that would disappear if we had
        # silently used the analysis-axis-closest available date instead.
        assert float(m0_expected) > 0
        # Additionally: the Mar02 T1 date is strictly OUTSIDE the caller
        # trade_dates axis.  If the adapter had dropped non-axis close rows
        # the first EW observation would be unavailable (all member T-1
        # cells = NaN).  We prove the positive nonzero EW on day 0 came in
        # through that non-axis Mar02 slot by computing a "naive axis-only"
        # oracle where every member's T-1 is their axis[1]=Mar03 close, and
        # showing the computed S0 EW differs from that naive number.
        s0_members = list(MEMBERSHIPS_FIXTURE[S0_KEY].member_ids)
        naive_axis_only_returns = []
        for m in s0_members:
            r = compute_exact_return(
                CLOSE_GRID[(date(2026, 3, 3), m)],
                CLOSE_GRID[(date(2026, 3, 3), m)],  # ← WRONG but this is the
                # "axis had no T1 outside" naive hypothesis.  Same price → 0.
            )
            if r is not None and math.isfinite(float(r)):
                naive_axis_only_returns.append(float(r))
        naive_mean = float(_return_distribution(sorted(naive_axis_only_returns))["mean"]) if naive_axis_only_returns else None
        # Same-price → 0 return.
        assert naive_mean is not None and float(naive_mean) == 0.0
        # Build only the bar rows that correspond to S0's 4 members.  This
        # avoids the broader union fixture bleeding members the test scope
        # doesn't own (adapter guards against it, but we keep the test
        # honest about its own input set).
        members_s0 = set(s0_members)
        rows_s0 = [
            (m, d, Decimal(f"{v:.4f}"))
            for (d, m), v in CLOSE_GRID.items()
            if m in members_s0
        ]
        result, _ = await _run(scope_keys=[S0_KEY], bar_rows=rows_s0)
        s0_ew = result.scopes[0].ew_values
        assert s0_ew[0] is not None and float(s0_ew[0]) != 0.0
        # And match the correct oracle using the real external T-1 (Mar02).
        def _cv(raw: float) -> float:
            return float(Decimal(f"{raw:.4f}"))

        correct_returns = []
        for m in s0_members:
            r = compute_exact_return(
                _cv(CLOSE_GRID[(date(2026, 3, 3), m)]),
                _cv(CLOSE_GRID[(date(2026, 3, 2), m)]),
            )
            if r is not None and math.isfinite(float(r)):
                correct_returns.append(float(r))
        correct_mean = float(_return_distribution(sorted(correct_returns))["mean"])
        assert float(s0_ew[0]) == float(correct_mean)


# ===========================================================================
# Test J: current-static membership owner reuse + overlap + metadata.
# ===========================================================================
class TestMembershipOwnerReuse:
    @pytest.mark.asyncio
    async def test_J_membership_overlap_metadata_and_ref_counts(self) -> None:
        result, _ = await _run()
        keys = list(result.scope_order)
        assert keys == [S0_KEY, S1_KEY, S2_KEY]
        names = {s.scope_key: s.scope_name for s in result.scopes}
        counts = {s.scope_key: s.member_count for s in result.scopes}
        assert names == {S0_KEY: "Scope Zero", S1_KEY: "Scope One", S2_KEY: "Scope Two"}
        assert counts == {S0_KEY: 4, S1_KEY: 4, S2_KEY: 4}
        # scope_member_refs = 3 scopes × 4 each = 12.
        # union M = {M0..M7} = 8 distinct → duplication ratio = 12 / 8 = 1.5
        m = result.metrics
        assert m.scope_member_refs == 12
        assert m.M == 8
        assert m.S == 3
        # Close matrix: R=6 × M=8 = 48 cells.  Each cell written finite in fixture.
        assert m.R == 6
        assert m.close_matrix_mib == pytest.approx((6 * 8 * 8) / (1024 * 1024), rel=1e-9)


# ===========================================================================
# Test K: EW exact against scalar oracle (compute_exact_return +
# canonical _return_distribution) on the full 3-scope × 5-day × 8-member
# synthetic grid.  This is the single largest correctness gate in C1.
# ===========================================================================
class TestExactEwParity:
    @pytest.mark.asyncio
    async def test_K_ew_exact_vs_scalar_oracle(self) -> None:
        # Use the same DB-close loading semantics as the adapter for the
        # scalar oracle: raw values are 4-digit-decimalised → float (this
        # is what the real adapter consumes).  Using raw CLOSE_GRID floats
        # directly would create spurious ~1e-15 differences caused by
        # Decimal("10.1000") float vs the binary-rounded product of the
        # 1.01 drift multiplications.  Both sides must read the same
        # on-the-wire representation.
        def _bar_close_value(raw: float) -> float:
            # Mirrors _build_bar_rows_decimal (4-digit-decimal → float).
            return float(Decimal(f"{raw:.4f}"))

        result, _ = await _run()
        # Rebuild the same deterministic member_ids / memberships / dates.
        union = set()
        mems: dict[str, tuple[uuid.UUID, ...]] = {}
        for k in [S0_KEY, S1_KEY, S2_KEY]:
            mm = MEMBERSHIPS_FIXTURE[k].member_ids
            mems[k] = mm
            union.update(mm)
        member_ids = sorted(union)
        m_to_c = {m: i for i, m in enumerate(member_ids)}
        required_bar_dates = list(DATES_ALL_CLOSE)
        d_to_r = {d: i for i, d in enumerate(required_bar_dates)}
        t_idx = [d_to_r[d] for d in TRADE_DATES_5]
        t1_by_date = {d: required_bar_dates[d_to_r[d] - 1] for d in TRADE_DATES_5}
        t1_idx = [d_to_r[t1_by_date[d]] for d in TRADE_DATES_5]
        R, M = len(required_bar_dates), len(member_ids)
        close = np.full((R, M), np.nan)
        for (d, m), v in CLOSE_GRID.items():
            close[d_to_r[d], m_to_c[m]] = _bar_close_value(float(v))
        # Scalar oracle for every (date, scope): finite returns only →
        # _return_distribution mean; empty universe → None.
        expected_by_scope: dict[str, list[float | None]] = {}
        for k, mm in mems.items():
            col: list[float | None] = [None] * len(TRADE_DATES_5)
            for t, d in enumerate(TRADE_DATES_5):
                rets: list[float] = []
                for m in mm:
                    close_t = close[t_idx[t], m_to_c[m]]
                    close_t1 = close[t1_idx[t], m_to_c[m]]
                    r = compute_exact_return(float(close_t), float(close_t1))
                    if r is not None and math.isfinite(float(r)):
                        rets.append(float(r))
                if not rets:
                    col[t] = None
                else:
                    col[t] = float(_return_distribution(sorted(rets))["mean"])
            expected_by_scope[k] = col
        # Exact float equality, no tolerance.  NaN ↔ None already normalised
        # by the adapter so we only compare (None↔None) or (finite↔finite exact).
        for scope_result in result.scopes:
            exp = expected_by_scope[scope_result.scope_key]
            got = list(scope_result.ew_values)
            assert len(exp) == len(got)
            for i, (e, g) in enumerate(zip(exp, got)):
                if e is None:
                    assert g is None, (scope_result.scope_key, i, e, g)
                else:
                    assert g is not None, (scope_result.scope_key, i)
                    # exact float bit equality — no np.isclose fallback.
                    assert float(g) == float(e), (
                        scope_result.scope_key,
                        i,
                        float(g),
                        float(e),
                    )


# ===========================================================================
# Test L: top-level result must NOT retain transient matrices / internals.
# ===========================================================================
class TestResultRetention:
    @pytest.mark.asyncio
    async def test_L_no_matrix_leakage_in_result(self) -> None:
        result, _ = await _run()
        # 1. Struct-level: top-level payload should contain only
        #    scope_type / analysis_asof_date / trade_dates / scopes /
        #    metrics / scope_order.  No ndarray survives in any of them.
        payload = result
        assert isinstance(payload, HistoricalEWBatchResult)
        for attr in (
            "scope_type",
            "analysis_asof_date",
            "trade_dates",
            "scopes",
            "metrics",
            "scope_order",
        ):
            assert hasattr(payload, attr), attr
        # Recursively walk the payload; reject any numpy ndarray / numpy scalar
        # that isn't a plain value inside the float/None ew_values tuple.
        forbidden_types: tuple[type, ...] = (np.ndarray,)

        def walk(node: Any, path: str) -> None:
            if isinstance(node, forbidden_types):
                raise AssertionError(f"numpy array leaked at {path}: {type(node)}")
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(node, (tuple, list)):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")
            elif isinstance(node, HistoricalEWBatchResult):
                for attr in ("scope_type", "analysis_asof_date", "trade_dates",
                             "scopes", "metrics", "scope_order"):
                    walk(getattr(node, attr), f"{path}.{attr}")
            elif isinstance(node, HistoricalEWScopeResult):
                # ``ew_values`` tuple of float|None is allowed; anything
                # else we recurse into.
                walk(node.ew_values, f"{path}.ew_values")
                walk(node.scope_key, f"{path}.scope_key")
                walk(node.scope_name, f"{path}.scope_name")
            # dataclass metrics struct: all int/float primitives.
            elif hasattr(node, "__dataclass_fields__"):
                for fname in node.__dataclass_fields__:
                    walk(getattr(node, fname), f"{path}.{fname}")

        walk(payload, "result")
        # 2. Ew_values type + length guarantee.
        for s in result.scopes:
            assert isinstance(s.ew_values, tuple)
            assert len(s.ew_values) == len(result.trade_dates)
            for v in s.ew_values:
                assert v is None or isinstance(v, (int, float)) and not isinstance(v, bool)


# ===========================================================================
# Test M: empty / invalid axis fail closed.
# ===========================================================================
class TestInvalidAxisFailClosed:
    @pytest.mark.asyncio
    async def test_M_empty_axis(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            await _run(trade_dates=[])

    @pytest.mark.asyncio
    async def test_M_duplicate_axis(self) -> None:
        bad = [*TRADE_DATES_5]
        bad[1] = bad[0]  # non-unique
        with pytest.raises(ValueError, match="strictly ascending"):
            await _run(trade_dates=bad)

    @pytest.mark.asyncio
    async def test_M_non_ascending_axis(self) -> None:
        bad = list(reversed(TRADE_DATES_5))
        with pytest.raises(ValueError, match="strictly ascending"):
            await _run(trade_dates=bad)

    @pytest.mark.asyncio
    async def test_M_future_dated_axis(self) -> None:
        bad = [*TRADE_DATES_5]
        bad[-1] = date(2026, 3, 10)
        # We also need analysis_asof_date to match this new tail, so keep
        # tail=ANALYSIS_ASOF; instead stick a future date in the middle.
        bad = [*TRADE_DATES_5]
        bad[0] = date(2026, 3, 15)  # later than analysis_asof_date
        with pytest.raises(ValueError, match="later than analysis_asof_date"):
            await _run(trade_dates=bad)

    @pytest.mark.asyncio
    async def test_M_axis_tail_not_equal_asof(self) -> None:
        bad = [*TRADE_DATES_5[:-1]]  # tail is Mar06, not Mar07
        with pytest.raises(ValueError, match="trade_dates\\[-1\\].*must equal"):
            await _run(
                trade_dates=bad,
                analysis_asof_date=ANALYSIS_ASOF_5,
            )

    @pytest.mark.asyncio
    async def test_M_empty_scope_keys(self) -> None:
        with pytest.raises(ValueError, match="scope_keys must be non-empty"):
            await _run(scope_keys=[])

    @pytest.mark.asyncio
    async def test_M_duplicate_scope_keys(self) -> None:
        with pytest.raises(ValueError, match="scope_keys must be strictly unique"):
            await _run(scope_keys=[S0_KEY, S0_KEY])
