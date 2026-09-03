"""Modified-scope pure/unit tests for the Slice 3 Momentum+Volume history projection.

Covers ``_build_momentum_volume_projection`` in ``review_scope_diagnostics_service``.
No DB, no network — the projection is a pure direct-projection over a synthetic
published-run-safe ``canonical`` payload + formal date axis.

Locked contracts (from the Slice 3 spec §九):
- date slot alignment (gap date -> None, slot preserved)
- missing fact -> null
- open categorical relation verbatim preservation (no fixed enum, unknown kept)
- percentile20/200 central projection (direct, not recomputed)
- release ratio projection (member-first median, not event-weighted)
- SQZ_RELEASE event stream is NEVER used as release_volume_ratio source
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.review_scope_diagnostics_service import _build_momentum_volume_projection


def _full_payload() -> dict[str, Any]:
    """A canonical Observation payload with full momentum + participation.volume facts."""
    return {
        "momentum": {
            "state": {
                "expanding_count": 6,
                "expanding_ratio": 0.3,
                "flat_count": 8,
                "flat_ratio": 0.4,
                "contracting_count": 6,
                "contracting_ratio": 0.3,
                "denominator": 20,
            },
            "change": {
                "enhancing_count": 5,
                "weakening_count": 3,
                "flat_count": 12,
                "denominator": 20,
            },
            "squeeze_state": {
                "squeeze_count": 2,
                "squeeze_ratio": 0.1,
                "squeeze_release_count": 1,
                "squeeze_release_ratio": 0.05,
                "non_squeeze_count": 17,
                "non_squeeze_ratio": 0.85,
                "denominator": 20,
            },
            "bb_position": {"median": 0.5, "p25": 0.2, "p75": 0.8, "valid_count": 20, "denominator": 20},
            "bb_width": {"median": 0.08, "p25": 0.05, "p75": 0.12, "valid_count": 20, "denominator": 20},
            "release_volume_ratio": {"median": 1.42, "p25": 1.1, "p75": 1.9, "valid_count": 20, "denominator": 20},
            # OPEN categorical: producer emits arbitrary category vocabulary.
            "momentum_volume_relation": {
                "共振_count": 9,
                "共振_ratio": 0.45,
                "背离_count": 4,
                "背离_ratio": 0.2,
                "缩量挤压_count": 2,
                "缩量挤压_ratio": 0.1,
                "denominator": 20,
            },
            "sqzmom": {"mean": 0.37, "valid_count": 18},
        },
        "participation": {
            "volume": {
                "ratio20": {"p25": 0.9, "p50": 1.25, "p75": 1.6, "valid_count": 18},
                "ratio200": {"p25": 0.8, "p50": 1.1, "p75": 1.4, "valid_count": 18},
                "percentile20": {"p25": 60, "p50": 72.5, "p75": 85, "valid_count": 18},
                "percentile200": {"p25": 55, "p50": 68.0, "p75": 80, "valid_count": 18},
                "zscore20": {"p25": -2.0, "p50": -1.35, "p75": -0.2, "valid_count": 18},
                "zscore200": {"p25": -1.5, "p50": -0.9, "p75": 0.1, "valid_count": 18},
                "badge": {"high_count": 4, "low_count": 3, "normal_count": 10, "unknown_count": 1},
                "ratio20_mean": 1.27,
                "ratio200_mean": 1.12,
                "percentile20_histogram": {"lt20": 1, "20_40": 3, "40_60": 6, "60_80": 5, "gte80": 3},
                "percentile200_histogram": {"lt20": 2, "20_40": 4, "40_60": 5, "60_80": 4, "gte80": 3},
            }
        },
        # SQZ_RELEASE lives in structure.events — must NOT leak into release_volume_ratio.
        "structure": {
            "events": {
                "status": "ready",
                "cells": {
                    "leveled": {
                        "SQZ_RELEASE_Up_Swing": {
                            "event_type": "SQZ_RELEASE",
                            "direction": "Up",
                            "structure_level": "Swing",
                            "event_count": 1,
                            "member_count": 1,
                            "member_ratio": 0.3,
                        }
                    },
                    "extreme": {},
                },
            }
        },
    }


def test_mv1_date_slot_alignment_preserves_gap_as_null():
    d1 = date(2024, 1, 2)
    d2 = date(2024, 1, 3)  # gap: no fact persisted
    d3 = date(2024, 1, 4)
    canonical = {
        d1: _full_payload(),
        d3: _full_payload(),
    }
    out = _build_momentum_volume_projection(canonical, [d1, d2, d3])
    assert out["dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    # gap slot -> null (preserved, never forward-filled / compressed)
    assert out["momentum_state"][1] is None
    assert out["release_volume_ratio"][1] is None
    assert out["sqzmom_mean"][1] is None
    # present slots -> projected
    assert out["momentum_state"][0] is not None
    assert out["momentum_state"][2] is not None
    # every array aligned to the same date axis length
    for key in (
        "momentum_state",
        "momentum_change",
        "squeeze_state",
        "release_volume_ratio",
        "momentum_volume_relation",
        "volume_percentile20",
        "volume_percentile200",
        "sqzmom_mean",
    ):
        assert len(out[key]) == 3, key


def test_mv2_missing_fact_per_date_null():
    d = date(2024, 1, 2)
    # payload present but momentum missing entirely
    canonical = {d: {"participation": {"volume": {"ratio20": {"p25": 0.9, "p50": 1.25, "p75": 1.6, "valid_count": 5}}}}}
    out = _build_momentum_volume_projection(canonical, [d])
    assert out["momentum_state"][0] is None
    assert out["squeeze_state"][0] is None
    assert out["release_volume_ratio"][0] is None
    assert out["momentum_volume_relation"][0] is None
    assert out["sqzmom_mean"][0] is None
    # volume percentile20 still absent (participation.volume has no percentile20)
    assert out["volume_percentile20"][0] is None


def test_mv3_open_categorical_relation_verbatim_unknown_preserved():
    d = date(2024, 1, 2)
    canonical = {d: _full_payload()}
    out = _build_momentum_volume_projection(canonical, [d])
    rel = out["momentum_volume_relation"][0]
    assert rel is not None
    # unknown / arbitrary categories preserved verbatim (no fixed enum; keys suffixed _count/_ratio)
    assert "共振_count" in rel and "背离_count" in rel and "缩量挤压_count" in rel
    assert rel["共振_count"] == 9
    assert rel["共振_ratio"] == 0.45
    assert rel["denominator"] == 20


def test_mv4_percentile20_200_central_projection_direct():
    d = date(2024, 1, 2)
    canonical = {d: _full_payload()}
    out = _build_momentum_volume_projection(canonical, [d])
    p20 = out["volume_percentile20"][0]
    p200 = out["volume_percentile200"][0]
    # direct projection of the persisted distribution (no recompute)
    assert p20["p50"] == 72.5
    assert p20["p25"] == 60
    assert p20["p75"] == 85
    assert p200["p50"] == 68.0


def test_mv5_release_ratio_projection_member_first_median():
    d = date(2024, 1, 2)
    canonical = {d: _full_payload()}
    out = _build_momentum_volume_projection(canonical, [d])
    rv = out["release_volume_ratio"][0]
    assert rv is not None
    # member-first median, NOT event-weighted
    assert rv["median"] == 1.42


def test_mv6_sqz_release_event_stream_not_release_volume_ratio_source():
    d = date(2024, 1, 2)
    # payload has SQZ_RELEASE in structure.events but NO momentum.release_volume_ratio
    payload = _full_payload()
    del payload["momentum"]["release_volume_ratio"]
    canonical = {d: payload}
    out = _build_momentum_volume_projection(canonical, [d])
    # release_volume_ratio must be null — SQZ_RELEASE event stream is not its source
    assert out["release_volume_ratio"][0] is None
