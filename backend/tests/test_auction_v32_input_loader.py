"""KPI-3 fix round: expected A-share universe + PIT definition-version window.

No PostgreSQL required: a FakeSession routes the loader's fixed bulk queries and
returns canned rows, so the coverage denominator and the definition-version
interval intersection are proven in pure unit space.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.domain.auction.member_observation import (
    AuctionMemberObservation,
    build_member_observation,
)
from app.domain.auction.membership_pit import (
    FAMILY_INDUSTRY,
    resolve_scope_members_bulk,
)
from app.domain.auction.scope_history import build_scope_history_series
from app.services.auction_v32_input_loader import (
    _MISSING_QUOTE,
    _load_membership_edges,
    expected_active_ashare_stmt,
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
        universe=(),
        quotes=(),
        slots=(_T,),
        edges=(),
        history=(),
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
# P0-1: active A-share口径 — single production predicate owner
# ---------------------------------------------------------------------------
def test_expected_universe_owner_uses_active_six_digit_not_market() -> None:
    """Test consumes the SINGLE production predicate owner
    (``expected_active_ashare_stmt``); it does not re-write the SQL."""
    sql = str(expected_active_ashare_stmt())
    assert "market" not in sql, "Instrument.market is SH/SZ/BJ, never a single 'A'"
    assert "symbol" in sql
    assert "~" in sql  # POSIX regex operator for the 6-digit symbol


async def test_load_expected_universe_returns_the_canned_active_set() -> None:
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
# P1: BoardDefinitionVersion PIT window — interval intersection
# ---------------------------------------------------------------------------
async def test_loader_drops_edges_whose_definition_version_expired_before_window() -> None:
    i_ok, i_stale = uuid4(), uuid4()
    slots = (_T, _WIN_START)

    ok_row = (
        "IND_OK", "OK", FAMILY_INDUSTRY, i_ok,
        _WIN_START, None,
        _WIN_START, None,
    )
    stale_row = (
        "IND_STALE", "STALE", FAMILY_INDUSTRY, i_stale,
        _WIN_START, None,
        date(2026, 1, 1), date(2026, 3, 31),
    )
    session = _LoaderSession(edges=(ok_row, stale_row), slots=slots, universe=())
    inputs = await load_v32_inputs(
        session, trade_date=_T, capture_run_id=_CAP, window=120
    )
    keys = {e.scope_key for e in inputs.edges}
    assert "IND_OK" in keys
    assert "IND_STALE" not in keys


def _obs(iid: UUID, d: date) -> AuctionMemberObservation:
    return build_member_observation(
        instrument_id=iid,
        trade_date=d,
        final_price=10.0,
        prev_close=9.0,
        amount=1000.0,
        quality_status="ok",
        source="verified_consensus",
    )


async def test_pit_edge_intersection_when_definition_starts_mid_window() -> None:
    """Definition version begins AFTER the membership row, inside the window.

    The historical Scope builder must NOT see the member before the definition
    version existed.
    """
    i = uuid4()
    rows = [(
        "IND", "Name", FAMILY_INDUSTRY, i,
        date(2026, 8, 1), None,
        date(2026, 8, 10), None,
    )]
    edges = await _load_membership_edges(_LoaderSession(edges=rows), _T, date(2026, 8, 1))
    assert len(edges) == 1
    assert edges[0].effective_from == date(2026, 8, 10)
    assert edges[0].effective_to is None

    by_date = resolve_scope_members_bulk(
        edges, [date(2026, 8, 9), date(2026, 8, 10)], family=FAMILY_INDUSTRY
    )
    assert by_date[date(2026, 8, 9)].get("IND") is None
    assert "IND" in by_date[date(2026, 8, 10)]


async def test_pit_edge_intersection_when_definition_ends_mid_window() -> None:
    """Definition version ends BEFORE the membership row, inside the window.

    The historical Scope builder must NOT keep using a stale definition after it
    ended.
    """
    i = uuid4()
    rows = [(
        "IND", "Name", FAMILY_INDUSTRY, i,
        date(2026, 8, 1), None,
        date(2026, 8, 1), date(2026, 8, 10),
    )]
    edges = await _load_membership_edges(_LoaderSession(edges=rows), _T, date(2026, 8, 1))
    assert len(edges) == 1
    assert edges[0].effective_from == date(2026, 8, 1)
    assert edges[0].effective_to == date(2026, 8, 10)

    by_date = resolve_scope_members_bulk(
        edges,
        [date(2026, 8, 9), date(2026, 8, 10), date(2026, 8, 11)],
        family=FAMILY_INDUSTRY,
    )
    assert "IND" in by_date[date(2026, 8, 9)]
    assert by_date[date(2026, 8, 10)].get("IND") is None
    assert by_date[date(2026, 8, 11)].get("IND") is None


async def test_pit_definition_version_respected_in_history_series() -> None:
    """Full path: DB-row adapter -> MembershipEdge -> build_scope_history_series.

    The Definition Version ends mid-window, so the member must disappear from the
    historical Scope series exactly when the definition version ended.
    """
    i = uuid4()
    rows = [(
        "IND", "Name", FAMILY_INDUSTRY, i,
        date(2026, 8, 1), None,
        date(2026, 8, 1), date(2026, 8, 10),
    )]
    edges = await _load_membership_edges(_LoaderSession(edges=rows), _T, date(2026, 8, 1))
    dates = [date(2026, 8, 9), date(2026, 8, 10), date(2026, 8, 11)]
    obs = {d: (_obs(i, d),) for d in dates}
    series = build_scope_history_series(
        trade_dates=dates,
        observations_by_date=obs,
        edges=edges,
        config=AuctionMemberFactConfig(
            positive_gap_percentile_threshold=90.0,
            negative_gap_percentile_threshold=10.0,
            volume_abnormal_percentile_threshold=90.0,
            amount_abnormal_percentile_threshold=90.0,
        ),
    )
    assert "IND" in series.industry[date(2026, 8, 9)]
    assert "IND" not in series.industry[date(2026, 8, 10)]
    assert "IND" not in series.industry[date(2026, 8, 11)]
