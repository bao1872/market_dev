"""API contract tests for Auction V3.2 (no PostgreSQL).

These pin the contracts the endpoints must satisfy:

- §三   the read chain is publication -> scan_run -> scope results;
- §四   publication pointer: a newer UNPUBLISHED run stays invisible;
- §五   the COMPLETE family snapshot is returned (25 industries -> 25 rows);
- §六   industry and concept never mix;
- §七   meta/dates lists only formally published dates;
- §八   null stays null (insufficient history never becomes 0);
- §九   detail exposes all five canonical groups;
- §十   mapping only reads the payload — no business metric is recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.auction.publication_read import (
    V32_ALGORITHM_VERSION,
    published_dates,
    read_published_scope_results,
    select_published_run,
    to_scope_detail,
    to_scope_list_items,
)
from app.domain.auction.scope_payload import build_scope_payload

_T = date(2026, 8, 14)


@dataclass
class FakePublication:
    trade_date: date
    algorithm_version: str
    scan_run_id: UUID
    published_at: datetime


@dataclass
class FakeScopeResult:
    scan_run_id: UUID
    trade_date: date
    scope_type: str
    scope_id: UUID | None
    scope_name: str | None
    payload: dict[str, Any] = field(default_factory=dict)


def _pub(trade_date: date, run_id: UUID, when: datetime) -> FakePublication:
    return FakePublication(trade_date, V32_ALGORITHM_VERSION, run_id, when)


def _at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 8, day, hour, 30, tzinfo=UTC)


def _payload(
    *,
    ew: float | None = 0.01,
    position: float | None = 60.0,
    velocity: float | None = 0.2,
    acceleration: float | None = 0.05,
    amount_position: float | None = 55.0,
    amount_multiple: float | None = 1.4,
) -> dict:
    groups = (
        {
            "equal_weight_gap": ew,
            "amount_weighted_gap": 0.012,
            "capital_tilt": 0.002,
            "positive_gap_breadth": 0.5,
            "negative_gap_breadth": 0.3,
            "unchanged_gap_breadth": 0.2,
            "gap_dispersion": 0.01,
            "price_normalized_hhi": 0.3,
            "price_valid_count": 10,
        },
        {
            "position": position,
            "ema_fast": 60.0,
            "ema_slow": 58.0,
            "velocity": velocity,
            "signal": 0.15,
            "acceleration": acceleration,
        },
        {
            "total_auction_amount": 1_000_000.0,
            "amount_position": amount_position,
            "amount_multiple": amount_multiple,
            "amount_abnormal_breadth": 0.25,
            "top1_amount_share": 0.2,
            "top3_amount_share": 0.45,
            "amount_normalized_hhi": 0.4,
        },
        {
            "repricing": {"equal_weight_gap": 70.0},
            "breadth": {"positive_gap_breadth": 65.0},
            "participation": {"amount_position": 55.0},
            "concentration": {"amount_normalized_hhi": 40.0},
        },
        {
            "members": [],
            "leadership_migration": 0.3,
            "retained": [],
            "entrants": [],
            "exits": [],
            "jaccard": 0.7,
        },
    )
    return build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": "IND_BANK", "scope_name": "银行"},
        repricing=groups[0],
        historical_dynamics=groups[1],
        participation=groups[2],
        cross_sectional=groups[3],
        member_attribution=groups[4],
    )


def _row(
    run_id: UUID,
    scope_type: str,
    name: str,
    trade_date: date = _T,
    **payload_kwargs: Any,
) -> FakeScopeResult:
    return FakeScopeResult(
        scan_run_id=run_id,
        trade_date=trade_date,
        scope_type=scope_type,
        scope_id=uuid4(),
        # scope_name is only a display label; the product key lives in payload
        scope_name=f"显示名-{name}",
        payload=_payload(**payload_kwargs),
    )


# ---------------------------------------------------------------------------
# §四 publication pointer — the most important contract
# ---------------------------------------------------------------------------
def test_scenario1_published_run_is_read() -> None:
    run_a = uuid4()
    pubs = [_pub(_T, run_a, _at(14, 9))]
    rows = [_row(run_a, "industry", "IND_BANK")]
    selected = select_published_run(pubs, trade_date=_T)
    assert selected is not None and selected.scan_run_id == run_a
    assert len(read_published_scope_results(pubs, rows, trade_date=_T, family="industry")) == 1


def test_scenario2_newer_unpublished_run_stays_invisible() -> None:
    """Run B is newer and succeeded, but has NO publication -> must stay hidden."""
    run_a, run_b = uuid4(), uuid4()
    pubs = [_pub(_T, run_a, _at(14, 9))]  # only A is published
    rows = [
        _row(run_a, "industry", "IND_FROM_A", ew=0.010),
        _row(run_b, "industry", "IND_FROM_B", ew=0.999),  # newer, unpublished
    ]
    result = read_published_scope_results(pubs, rows, trade_date=_T, family="industry")
    assert len(result) == 1
    assert result[0].scope_name == "显示名-IND_FROM_A"


def test_scenario3_unpublished_scope_results_are_invisible() -> None:
    """Scope results without a publication must never reach the user API."""
    run_a = uuid4()
    rows = [_row(run_a, "industry", "IND_ORPHAN")]
    assert read_published_scope_results([], rows, trade_date=_T, family="industry") == []
    assert select_published_run([], trade_date=_T) is None


def test_publication_on_another_date_does_not_leak() -> None:
    run_a = uuid4()
    other = date(2026, 8, 13)
    pubs = [_pub(other, run_a, _at(13, 9))]
    rows = [_row(run_a, "industry", "IND_BANK", trade_date=other)]
    assert read_published_scope_results(pubs, rows, trade_date=_T, family="industry") == []


def test_legacy_algorithm_publication_is_not_v32() -> None:
    run_legacy = uuid4()
    pubs = [FakePublication(_T, "auction-legacy", run_legacy, _at(14, 9))]
    assert select_published_run(pubs, trade_date=_T) is None


# ---------------------------------------------------------------------------
# §五 complete family snapshot (no Top-N)
# ---------------------------------------------------------------------------
def test_25_industries_all_returned() -> None:
    run_a = uuid4()
    pubs = [_pub(_T, run_a, _at(14, 9))]
    rows = [_row(run_a, "industry", f"IND_{i:02d}") for i in range(25)]
    result = read_published_scope_results(pubs, rows, trade_date=_T, family="industry")
    assert len(result) == 25
    assert len(to_scope_list_items(result)) == 25


def test_40_concepts_all_returned() -> None:
    run_a = uuid4()
    pubs = [_pub(_T, run_a, _at(14, 9))]
    rows = [_row(run_a, "concept", f"CPT_{i:02d}") for i in range(40)]
    result = read_published_scope_results(pubs, rows, trade_date=_T, family="concept")
    assert len(result) == 40


# ---------------------------------------------------------------------------
# §六 family isolation
# ---------------------------------------------------------------------------
def test_industry_request_never_returns_concept() -> None:
    run_a = uuid4()
    pubs = [_pub(_T, run_a, _at(14, 9))]
    rows = [
        _row(run_a, "industry", "IND_BANK"),
        _row(run_a, "concept", "CPT_ROBOT"),
        _row(run_a, "concept", "CPT_AI"),
    ]
    got = read_published_scope_results(pubs, rows, trade_date=_T, family="industry")
    assert [r.scope_name for r in got] == ["显示名-IND_BANK"]

    got_concept = read_published_scope_results(pubs, rows, trade_date=_T, family="concept")
    assert sorted(r.scope_name for r in got_concept) == [
        "显示名-CPT_AI",
        "显示名-CPT_ROBOT",
    ]


# ---------------------------------------------------------------------------
# §七 meta/dates = formally published dates only
# ---------------------------------------------------------------------------
def test_published_dates_only_include_v32_publications() -> None:
    pubs = [
        _pub(date(2026, 8, 14), uuid4(), _at(14, 9)),
        _pub(date(2026, 8, 13), uuid4(), _at(13, 9)),
        FakePublication(date(2026, 8, 12), "auction-legacy", uuid4(), _at(12, 9)),
    ]
    got = published_dates(pubs)
    assert got == [date(2026, 8, 14), date(2026, 8, 13)]


def test_published_dates_empty_when_nothing_published() -> None:
    assert published_dates([]) == []


# ---------------------------------------------------------------------------
# §八 null fidelity — insufficient history must NOT become 0
# ---------------------------------------------------------------------------
def test_insufficient_history_stays_null() -> None:
    run_a = uuid4()
    pubs = [_pub(_T, run_a, _at(14, 9))]
    rows = [
        _row(
            run_a,
            "industry",
            "IND_THIN",
            position=None,
            velocity=None,
            acceleration=None,
            amount_position=None,
            amount_multiple=None,
        )
    ]
    result = read_published_scope_results(pubs, rows, trade_date=_T, family="industry")
    items = to_scope_list_items(result)
    item = items[0]
    assert item.ew_position is None
    assert item.ew_velocity is None
    assert item.ew_acceleration is None
    assert item.amount_historical_position is None
    assert item.amount_multiple is None
    # and definitely not coerced to zero / empty string / False
    assert item.ew_position is not False
    assert item.ew_position != 0


def test_zero_gap_is_preserved_and_distinct_from_null() -> None:
    run_a = uuid4()
    pubs = [_pub(_T, run_a, _at(14, 9))]
    rows = [_row(run_a, "industry", "IND_FLAT", ew=0.0)]
    item = to_scope_list_items(
        read_published_scope_results(pubs, rows, trade_date=_T, family="industry")
    )[0]
    assert item.equal_weight_gap == 0.0  # a real zero, not unavailable


# ---------------------------------------------------------------------------
# §九 detail exposes all five groups
# ---------------------------------------------------------------------------
def test_detail_contains_five_canonical_groups() -> None:
    run_a = uuid4()
    row = _row(run_a, "industry", "IND_BANK")
    detail = to_scope_detail(row)
    for group in (
        "repricing",
        "historical_dynamics",
        "participation",
        "cross_sectional",
        "member_attribution",
        "diagnostics",
    ):
        assert group in detail


def test_detail_repricing_has_gap_breadth_dispersion_hhi() -> None:
    detail = to_scope_detail(_row(uuid4(), "industry", "IND_BANK"))
    re_ = detail["repricing"]
    for key in (
        "equal_weight_gap",
        "amount_weighted_gap",
        "capital_tilt",
        "positive_gap_breadth",
        "negative_gap_breadth",
        "unchanged_gap_breadth",
        "gap_dispersion",
        "price_normalized_hhi",
    ):
        assert key in re_


def test_detail_dynamics_has_ema_velocity_signal_acceleration() -> None:
    dyn = to_scope_detail(_row(uuid4(), "industry", "IND_BANK"))["historical_dynamics"]
    for key in ("position", "ema_fast", "ema_slow", "velocity", "signal", "acceleration"):
        assert key in dyn


def test_detail_participation_has_amount_position_multiple_top() -> None:
    part = to_scope_detail(_row(uuid4(), "industry", "IND_BANK"))["participation"]
    for key in (
        "total_auction_amount",
        "amount_position",
        "amount_multiple",
        "amount_abnormal_breadth",
        "top1_amount_share",
        "top3_amount_share",
        "amount_normalized_hhi",
    ):
        assert key in part


def test_detail_cross_sectional_has_four_axes_without_total_score() -> None:
    cross = to_scope_detail(_row(uuid4(), "industry", "IND_BANK"))["cross_sectional"]
    assert {"repricing", "breadth", "participation", "concentration"} <= set(cross)
    assert "score" not in cross and "total_score" not in cross


def test_detail_attribution_has_contributions_and_leadership() -> None:
    attr = to_scope_detail(_row(uuid4(), "industry", "IND_BANK"))["member_attribution"]
    for key in (
        "members",
        "leadership_migration",
        "retained",
        "entrants",
        "exits",
        "jaccard",
    ):
        assert key in attr


def test_technical_ids_live_only_in_diagnostics() -> None:
    row = _row(uuid4(), "industry", "IND_BANK")
    detail = to_scope_detail(row)
    assert "scan_run_id" in detail["diagnostics"]
    assert "scope_id" in detail["diagnostics"]
    # and not in the business groups
    assert "scan_run_id" not in detail["repricing"]


# ---------------------------------------------------------------------------
# §十 mapping only reads — never recomputes
# ---------------------------------------------------------------------------
def test_list_item_values_come_straight_from_the_payload() -> None:
    run_a = uuid4()
    payload = _payload(ew=0.077, position=88.0, velocity=1.5, amount_multiple=9.25)
    row = FakeScopeResult(run_a, _T, "industry", uuid4(), "IND_X", payload)
    item = to_scope_list_items([row])[0]
    assert item.equal_weight_gap == 0.077
    assert item.ew_position == 88.0
    assert item.ew_velocity == 1.5
    assert item.amount_multiple == 9.25


def test_malformed_payload_is_rejected_not_silently_defaulted() -> None:
    bad = _payload()
    bad["schema_version"] = "not-v32"
    row = FakeScopeResult(uuid4(), _T, "industry", uuid4(), "IND_BAD", bad)
    with pytest.raises(ValueError, match="schema_version"):
        to_scope_list_items([row])
