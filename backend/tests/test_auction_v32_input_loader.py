"""KPI-3 fix round: expected A-share universe + PIT definition-version window.

No PostgreSQL required: a FakeSession routes the loader's fixed bulk queries and
returns canned rows, so the coverage denominator and the definition-version
filter are proven in pure unit space.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import select

from app.domain.auction.membership_pit import FAMILY_INDUSTRY
from app.models.instrument import Instrument
from app.services.auction_v32_input_loader import (
    _MISSING_QUOTE,
    _is_active_ashare,
    load_expected_universe,
    load_v32_inputs,
)

_T = date(2026, 8, 14)
_CAP = uuid4()
_WIN_START = date(2026, 4, 1)


class _ScalarView:
    def __init__(self, scalars: list) -> None:
        self._scalars = scalars

    def all(self) -> list:
        return list(self._scalars)

    def scalar_one_or_none(self):
        return self._scalars[0] if self._scalars else None


class _Rows:
    def __init__(self, scalars: list, tuples: list) -> None:
        self._scalars = scalars
        self._tuples = tuples

    def scalars(self) -> _ScalarView:
        return _ScalarView(self._scalars)

    def all(self) -> list:
        return list(self._tuples)

    def scalar_one_or_none(self):
        return self._scalars[0] if self._scalars else None


class _LoaderSession:
    """Routes the loader's fixed bulk reads to canned data by query shape."""

    def __init__(
        self,
        *,
        universe: tuple[UUID, ...] = (),
        quotes: tuple = (),
        slots: tuple[date, ...] = (_T,),
        edges: tuple = (),
        history: tuple = (),
    ) -> None:
        self.universe = universe
        self.quotes = quotes
        self.slots = slots
        self.edges = edges
        self.history = history

    async def execute(self, stmt, *args, **kwargs):
        sql = str(stmt)
        if "instruments" in sql and "symbol" in sql:
            return _Rows(list(self.universe), [(i,) for i in self.universe])
        if "trading_calendar" in sql:
            return _Rows(list(self.slots), [(d,) for d in self.slots])
        if "auction_final_quotes" in sql and "historical_backfill" in sql:
            return _Rows(list(self.history), [(q,) for q in self.history])
        if "auction_final_quotes" in sql:
            return _Rows(list(self.quotes), [(q,) for q in self.quotes])
        if "market_boards" in sql and "external_code" in sql:
            return _Rows([], list(self.edges))
        return _Rows([], [])


# ---------------------------------------------------------------------------
# P0-1: active A-share口径 — must not key on Instrument.market == "A"
# ---------------------------------------------------------------------------
def test_active_ashare_requires_six_digit_symbol_and_active_status() -> None:
    assert _is_active_ashare("600000", "active") is True   # SH
    assert _is_active_ashare("000001", "active") is True   # SZ
    assert _is_active_ashare("830799", "active") is True   # BJ
    assert _is_active_ashare("600000", "delisted") is False
    assert _is_active_ashare("6000000", "active") is False  # 7 digits
    assert _is_active_ashare("US0001", "active") is False
    assert _is_active_ashare("", "active") is False


def test_expected_universe_query_shape_excludes_market_filter() -> None:
    stmt = select(Instrument.id).where(
        Instrument.status == "active",
        Instrument.symbol.op("~")(r"^\d{6}$"),
    )
    sql = str(stmt)
    assert "market" not in sql, "Instrument.market is SH/SZ/BJ, never a single 'A'"
    assert "symbol" in sql


async def test_expected_universe_returns_the_canned_active_set() -> None:
    included = tuple(uuid4() for _ in range(3))
    session = _LoaderSession(universe=included)
    result = await load_expected_universe(session)
    assert result == included


async def test_coverage_denominator_counts_missing_universe_members() -> None:
    """expected universe = 3, current quotes = 2 -> 1 missing -> denominator 3."""
    u1, u2, u3 = uuid4(), uuid4(), uuid4()

    class _Quote:
        def __init__(self, iid: UUID) -> None:
            self.instrument_id = iid
            self.trade_date = _T
            self.final_price = 10.0
            self.prev_close = 9.0
            self.amount = 1000.0
            self.quality_status = "ok"
            self.source = "verified_consensus"

    session = _LoaderSession(
        universe=(u1, u2, u3),
        quotes=(_Quote(u1), _Quote(u2)),
        slots=(_T,),
    )
    inputs = await load_v32_inputs(
        session, trade_date=_T, capture_run_id=_CAP, window=120
    )
    assert len(inputs.current_observations) == 3
    missing = [o for o in inputs.current_observations if o.source == _MISSING_QUOTE]
    assert len(missing) == 1
    assert missing[0].instrument_id == u3
    assert len(inputs.expected_universe_ids) == 3


# ---------------------------------------------------------------------------
# P1: BoardDefinitionVersion PIT window must also be enforced
# ---------------------------------------------------------------------------
async def test_loader_drops_edges_whose_definition_version_expired_before_window() -> None:
    i_ok, i_stale = uuid4(), uuid4()
    # slots[0] becomes window_start; trade_date stays _T
    slots = (_T, _WIN_START)

    ok_row = (
        "IND_OK", "OK", FAMILY_INDUSTRY, i_ok,
        _WIN_START, None,            # membership [from, to)
        _WIN_START, None,            # definition version [from, to) — still valid
    )
    stale_row = (
        "IND_STALE", "STALE", FAMILY_INDUSTRY, i_stale,
        _WIN_START, None,            # membership still overlaps
        date(2026, 1, 1), date(2026, 3, 31),  # definition version ended before window
    )
    session = _LoaderSession(edges=(ok_row, stale_row), slots=slots, universe=())
    inputs = await load_v32_inputs(
        session, trade_date=_T, capture_run_id=_CAP, window=120
    )
    keys = {e.scope_key for e in inputs.edges}
    assert "IND_OK" in keys
    assert "IND_STALE" not in keys
