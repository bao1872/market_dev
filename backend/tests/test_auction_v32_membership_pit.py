"""Tests for the Auction V3.2 PIT membership owner.

The interval convention is the highest-risk detail: the repo uses a HALF-OPEN
``[effective_from, effective_to)`` range (see review_scope_service.py:441-444),
so a membership ending exactly on T is NOT valid at T.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    MembershipEdge,
    definition_version_effective_in_window,
    is_effective_at,
    resolve_scope_members,
    resolve_scope_members_bulk,
)

# Deterministic, ascending UUIDs: resolve_scope_members() returns sorted()
# members, so random uuid4() values would make ordering assertions flaky
# (they pass or fail depending on the random draw).
_A = UUID("00000000-0000-0000-0000-000000000001")
_B = UUID("00000000-0000-0000-0000-000000000002")
_C = UUID("00000000-0000-0000-0000-000000000003")
_D0 = date(2026, 8, 1)
_D1 = date(2026, 8, 10)
_D2 = date(2026, 8, 14)


def _edge(iid, key="IND_BANK", family=FAMILY_INDUSTRY, frm=_D0, to=None):
    return MembershipEdge(
        instrument_id=iid,
        scope_key=key,
        scope_name=key,
        family=family,
        effective_from=frm,
        effective_to=to,
    )


# ---------------------------------------------------------------------------
# half-open interval semantics
# ---------------------------------------------------------------------------
def test_effective_from_equal_to_t_is_effective() -> None:
    e = _edge(_A, frm=_D1)
    assert is_effective_at(e, _D1) is True


def test_effective_to_equal_to_t_is_not_effective() -> None:
    """Right-exclusive: the membership's last valid day is effective_to - 1."""
    e = _edge(_A, frm=_D0, to=_D1)
    assert is_effective_at(e, _D1) is False
    assert is_effective_at(e, date(2026, 8, 9)) is True


def test_open_interval_is_effective_in_the_future() -> None:
    e = _edge(_A, frm=_D0, to=None)
    assert is_effective_at(e, date(2030, 1, 1)) is True


def test_before_effective_from_is_not_effective() -> None:
    e = _edge(_A, frm=_D1)
    assert is_effective_at(e, _D0) is False


# ---------------------------------------------------------------------------
# PIT: no back-fill with today's list, no stale forward
# ---------------------------------------------------------------------------
def test_member_joining_later_is_absent_on_earlier_date() -> None:
    """A stock that joined on D1 must NOT appear in the D0 membership."""
    edges = [_edge(_A, frm=_D0), _edge(_B, frm=_D1)]
    at_d0 = resolve_scope_members(edges, _D0)
    at_d1 = resolve_scope_members(edges, _D1)
    assert at_d0["IND_BANK"] == (_A,)
    assert at_d1["IND_BANK"] == (_A, _B)


def test_member_leaving_is_absent_after_end() -> None:
    edges = [_edge(_A, frm=_D0, to=_D1)]
    assert resolve_scope_members(edges, date(2026, 8, 9))["IND_BANK"] == (_A,)
    assert "IND_BANK" not in resolve_scope_members(edges, _D1)


def test_same_stock_in_multiple_concepts_is_counted_in_each() -> None:
    """Concept overlap: one instrument may belong to several scopes."""
    edges = [
        _edge(_A, key="CPT_ROBOT", family=FAMILY_CONCEPT),
        _edge(_A, key="CPT_AI", family=FAMILY_CONCEPT),
    ]
    res = resolve_scope_members(edges, _D2)
    assert set(res) == {"CPT_ROBOT", "CPT_AI"}
    assert res["CPT_ROBOT"] == (_A,)
    assert res["CPT_AI"] == (_A,)


def test_result_is_sorted_and_deduplicated() -> None:
    edges = [_edge(_C), _edge(_A), _edge(_B), _edge(_A)]
    assert resolve_scope_members(edges, _D2)["IND_BANK"] == tuple(sorted([_A, _B, _C]))


# ---------------------------------------------------------------------------
# family isolation
# ---------------------------------------------------------------------------
def test_family_filter_prevents_mixing_industry_and_concept() -> None:
    edges = [
        _edge(_A, key="IND_BANK", family=FAMILY_INDUSTRY),
        _edge(_B, key="CPT_ROBOT", family=FAMILY_CONCEPT),
    ]
    ind = resolve_scope_members(edges, _D2, family=FAMILY_INDUSTRY)
    con = resolve_scope_members(edges, _D2, family=FAMILY_CONCEPT)
    assert set(ind) == {"IND_BANK"}
    assert set(con) == {"CPT_ROBOT"}


# ---------------------------------------------------------------------------
# bulk == single date (one-pass consistency)
# ---------------------------------------------------------------------------
def test_bulk_matches_single_date_resolution() -> None:
    edges = [
        _edge(_A, key="IND_BANK", frm=_D0),
        _edge(_B, key="IND_BANK", frm=_D1),
        _edge(_C, key="IND_BANK", frm=_D0, to=_D1),
        _edge(_A, key="CPT_AI", family=FAMILY_CONCEPT, frm=_D0),
    ]
    dates = [_D0, _D1, _D2]
    bulk = resolve_scope_members_bulk(edges, dates)
    for d in dates:
        assert bulk[d] == resolve_scope_members(edges, d)


def test_bulk_returns_a_bucket_for_every_requested_date() -> None:
    edges = [_edge(_A, frm=_D0)]
    bulk = resolve_scope_members_bulk(edges, [_D0, _D1, _D2])
    assert set(bulk) == {_D0, _D1, _D2}


def test_empty_edges_yield_no_scopes() -> None:
    assert resolve_scope_members([], _D2) == {}
    assert resolve_scope_members_bulk([], [_D2]) == {_D2: {}}


# ---------------------------------------------------------------------------
# P1: BoardDefinitionVersion PIT window (not just membership row)
# ---------------------------------------------------------------------------
def test_definition_version_overlapping_window_is_effective() -> None:
    assert definition_version_effective_in_window(
        date(2026, 1, 1), None, date(2026, 8, 14), _D0
    ) is True
    # boundary: definition version begins exactly at window_start
    assert definition_version_effective_in_window(
        _D0, None, date(2026, 8, 14), _D0
    ) is True
    # boundary: definition version still open at window_start
    assert definition_version_effective_in_window(
        date(2026, 1, 1), date(2026, 12, 31), date(2026, 8, 14), _D0
    ) is True


def test_definition_version_ended_before_window_is_not_effective() -> None:
    """Counterexample: membership row overlaps, but its board definition ended
    before the window — the scope must not be built from a stale board."""
    assert definition_version_effective_in_window(
        date(2026, 1, 1), date(2026, 3, 31),
        date(2026, 8, 14), _D0,
    ) is False


def test_definition_version_starting_after_trade_date_is_not_effective() -> None:
    assert definition_version_effective_in_window(
        date(2026, 9, 1), None,
        date(2026, 8, 14), _D0,
    ) is False
