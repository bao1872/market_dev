"""REVIEW-CURRENT-SMC-OWNER (MIXED_CONTRACT_BUG) — narrow contract tests.

Pure (no DB).  Proves the resolved ownership split:

    CURRENT STATE  = Core(T)        (structure.current)
    HISTORY/EVIDENCE = History      (structure.events)

History(T) coverage must NOT suppress Current SMC availability.

A. Current Core ready / History(T) coverage missing  -> Current ready, Explorer SMC available.
B. Legal no-current-event (Core snapshot present, fp_structure_event_type null)
   -> Current source ready, event = null/no-event, NOT unavailable.
C. History(T) unavailable -> immutable event evidence unavailable, but Current SMC ready.
D. Historical date -> uses canonical History event evidence; no Core(T) backfill.
E. Explorer prefers Core(T) ``current`` and does NOT fall back to History when unavailable.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.first_pyramid_semantics import Direction
from app.domain.review.scope_observation import (
    MemberObservation,
    StructureEvent,
    compute_scope_observation,
)
from app.services.review_scope_explorer_service import (
    build_compare_facts,
    select_smc_display_event,
)


def _member(mid: str, **kw: object) -> MemberObservation:
    # MemberObservation requires the categorical/return fields (no dataclass
    # default); None is a valid "absent" value consumed by compute_scope_observation.
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=None,
        amount=None,
        trend=None,
        swing=None,
        internal=None,
        momentum=None,
        **kw,
    )


def _compute(members, *, trade_date=date(2026, 9, 3), events=None, coverage=None):
    return compute_scope_observation(
        scope_type="industry",
        scope_key="tech",
        trade_date=trade_date,
        pit_member_ids=[m.member_id for m in members],
        members=members,
        events=events if events is not None else [],
        event_coverage_member_ids=coverage,
    )


def test_A_current_core_ready_history_missing():
    members = [
        _member("a", latest_bos_direction=Direction.UP, latest_bos_level="Swing",
                current_only_present=True),
        _member("b", latest_choch_direction=Direction.DOWN, latest_choch_level="Internal",
                current_only_present=True),
    ]
    obs = _compute(members, events=[], coverage=None)
    struct = obs["structure"]
    # Current SMC sourced from Core(T), independent of History(T) coverage.
    assert struct["current"]["status"] == "ready"
    assert struct["current"]["denominator"] == 2
    # Immutable evidence gated by History(T) coverage -> unavailable (expected).
    assert struct["events"]["status"] == "unavailable"
    # Explorer SMC Event available from Current.
    smc = select_smc_display_event(struct["current"])
    assert smc["availability"] == "ready"
    assert smc["eventType"] in ("BOS", "CHoCH")


def test_B_legal_no_current_event():
    # Core(T) snapshot present (current_only_present True) but no SMC event facts.
    members = [_member("a", current_only_present=True)]
    obs = _compute(members, events=[], coverage=None)
    struct = obs["structure"]
    # Source (Core snapshot) present -> ready, NOT unavailable.
    assert struct["current"]["status"] == "ready"
    assert struct["current"]["denominator"] == 1
    smc = select_smc_display_event(struct["current"])
    # Legal zero-event day -> ready + no-event, never "unavailable".
    assert smc["availability"] == "ready"
    assert smc["eventType"] is None


def test_C_history_evidence_independent():
    members = [
        _member("a", latest_bos_direction=Direction.UP, latest_bos_level="Swing",
                current_only_present=True),
    ]
    obs = _compute(members, events=[], coverage=None)
    struct = obs["structure"]
    assert struct["current"]["status"] == "ready"
    assert struct["events"]["status"] == "unavailable"
    # Current must NOT carry a CURRENT_SOURCE_UNAVAILABLE reason.
    assert "CURRENT_SOURCE_UNAVAILABLE" not in (struct["current"].get("reason") or "")


def test_D_historical_uses_history_evidence():
    # Historical date: members have NO Core(T) current facts (current_only_present
    # defaults False) -> no Core(T) backfill into historical.
    members = [_member("a")]
    events = [StructureEvent(member_id="a", event_type="BOS", direction="bullish", internal=False)]
    obs = _compute(members, trade_date=date(2026, 9, 1), events=events, coverage={"a"})
    struct = obs["structure"]
    assert struct["current"]["status"] == "unavailable"
    assert struct["events"]["status"] == "ready"
    smc = select_smc_display_event(struct["events"])
    assert smc["availability"] == "ready"


def test_E_explorer_no_history_fallback_when_current_unavailable():
    obs = {
        "structure": {
            "current": {
                "status": "unavailable",
                "reason": "CURRENT_SOURCE_UNAVAILABLE",
                "denominator": 0,
                "cells": {"leveled": {}, "extreme": {}},
            },
            # History evidence IS available, but Explorer must NOT synthesize from it
            # for the Current display.
            "events": {
                "status": "ready",
                "cells": {
                    "leveled": {
                        "BOS_bullish_Swing": {
                            "event_type": "BOS",
                            "direction": "bullish",
                            "structure_level": "Swing",
                            "event_count": 1,
                            "member_count": 1,
                            "member_ratio": 1.0,
                        }
                    },
                    "extreme": {},
                },
            },
        },
        "trend": {"continuous": {}},
        "momentum": {"change": {}},
        "participation": {"volume": {"ratio20": {}}},
        "price": {"breadth": {}},
    }
    result = build_compare_facts(obs, {})
    # Explorer passes ``current`` (unavailable), never falls back to ``events``.
    assert result["smc"]["availability"] == "unavailable"


def test_explorer_current_ready_shows_smc():
    obs = {
        "structure": {
            "current": {
                "status": "ready",
                "denominator": 1,
                "cells": {
                    "leveled": {
                        "CHoCH_bearish_Internal": {
                            "event_type": "CHoCH",
                            "direction": "bearish",
                            "structure_level": "Internal",
                            "event_count": 1,
                            "member_count": 1,
                            "member_ratio": 1.0,
                        }
                    },
                    "extreme": {},
                },
            },
            "events": {
                "status": "unavailable",
                "denominator": None,
                "cells": {"leveled": {}, "extreme": {}},
            },
        },
        "trend": {"continuous": {}},
        "momentum": {"change": {}},
        "participation": {"volume": {"ratio20": {}}},
        "price": {"breadth": {}},
    }
    result = build_compare_facts(obs, {})
    assert result["smc"]["availability"] == "ready"
    assert result["smc"]["eventType"] == "CHoCH"


def test_current_structure_event_type_cell():
    # Exact-T fp_structure_event_type is surfaced as a Current SMC cell.
    members = [
        _member(
            "a",
            current_only_present=True,
            current_structure_event_type="CHoCH",
            current_structure_event_direction="bearish",
            current_structure_event_level="Internal",
        )
    ]
    obs = _compute(members, events=[], coverage=None)
    cells = obs["structure"]["current"]["cells"]["leveled"]
    assert "CHoCH_bearish_Internal" in cells


def test_current_smc_cells_match_structure_events_shape():
    # structure.current mirrors structure.events cell shape so the Explorer
    # selection (select_smc_display_event) is reused verbatim.
    members = [
        _member("a", latest_bos_direction=Direction.UP, latest_bos_level="Swing",
                current_only_present=True),
    ]
    obs = _compute(members, events=[], coverage=None)
    cur = obs["structure"]["current"]
    assert set(cur.keys()) == {"status", "cells", "denominator"}
    assert set(cur["cells"].keys()) == {"leveled", "extreme"}
    cell = cur["cells"]["leveled"]["BOS_bullish_Swing"]
    assert cell["event_type"] == "BOS"
    assert cell["direction"] == "bullish"
    assert cell["structure_level"] == "Swing"
    assert cell["member_ratio"] == 1.0
