"""REVIEW-PRODUCT-CLOSURE-01 Phase D + Phase C — typed history mapping + member directory.

Phase D: ``state_to_continuous`` must preserve canonical CATEGORICAL strings
(volatility_phase / momentum_direction / momentum_change / structure_alignment)
verbatim while numeric-coercing the NUMERIC keys, so ``FirstPyramidSemanticAdapter``
consumers (squeeze_state / alignment) get real values whenever the history state
source is present.  Verified against the real 2026-08-21 history state payload.

Phase C: ``collect_composition_member_ids`` gathers every member/instrument UUID
referenced by a Composition payload (leadership id arrays + attribution member_id),
and ``is_uuid`` gates malformed ids.  Purely additive display metadata — never
rewrites the persisted Composition.
"""
from __future__ import annotations

import uuid

import pytest

from app.domain.review.member_fact import (
    CATEGORICAL_STATE_KEYS,
    NUMERIC_STATE_KEYS,
    collect_composition_member_ids,
    is_uuid,
    state_to_continuous,
)

# ---------------------------------------------------------------------------
# Phase D — typed categorical/numeric mapping
# ---------------------------------------------------------------------------


def test_categorical_ownership_sets_split_every_state_key() -> None:
    """The two ownership sets must be disjoint and together cover every key."""
    assert not (set(NUMERIC_STATE_KEYS) & set(CATEGORICAL_STATE_KEYS))
    assert set(NUMERIC_STATE_KEYS) | set(CATEGORICAL_STATE_KEYS) == {
        "regime_strength",
        "dsa_dir_bars",
        "dsa_vwap_dev_pct",
        "segment_id",
        "segment_direction",
        "segment_bars",
        "segment_change_pct",
        "segment_slope",
        "current_vs_prev_volume_mean_ratio",
        "current_vs_prev_amount_mean_ratio",
        "current_segment_volume_mean",
        "prev_segment_volume_mean",
        "structure_alignment",
        "active_internal_ob_count",
        "active_swing_ob_count",
        "volatility_phase",
        "momentum_direction",
        "momentum_change",
        "sqzmom_delta",
        "sqzmom_val",
        "volume_ratio_20",
        "volume_percentile_20",
        "volume_zscore_20",
        "available_bars",
    }


def test_categorical_strings_survive_verbatim() -> None:
    """Real 2026-08-21 shape: the 4 categorical keys are JSONB STRINGS and must
    be carried verbatim (never numeric-coerced to None)."""
    state = {
        "volatility_phase": "squeeze_on",
        "momentum_direction": "contracting",
        "momentum_change": "weakening",
        "structure_alignment": "背离",
        # mixed numeric
        "regime_strength": 0.01945797279946948,
        "segment_direction": -1,
        "sqzmom_val": -0.33768211773303614,
    }
    out = state_to_continuous(state)
    assert out["volatility_phase"] == "squeeze_on"
    assert out["momentum_direction"] == "contracting"
    assert out["momentum_change"] == "weakening"
    assert out["structure_alignment"] == "背离"


def test_numeric_keys_coerce_finite_only() -> None:
    state = {
        "regime_strength": "0.5",  # numeric-string -> finite float
        "segment_direction": -1,
        "dsa_dir_bars": 33,
        "available_bars": None,  # JSON null -> None
    }
    out = state_to_continuous(state)
    assert out["regime_strength"] == 0.5
    assert out["segment_direction"] == -1.0
    assert out["dsa_dir_bars"] == 33.0
    assert out["available_bars"] is None


def test_absent_numeric_key_maps_to_none_never_zero() -> None:
    out = state_to_continuous({"regime_strength": 1.0})
    assert out["dsa_dir_bars"] is None
    assert out["sqzmom_val"] is None
    assert not isinstance(out["dsa_dir_bars"], int)


def test_empty_state_yields_all_none() -> None:
    out = state_to_continuous(None)
    assert len(out) == len(NUMERIC_STATE_KEYS) + len(CATEGORICAL_STATE_KEYS)
    assert all(v is None for v in out.values())


def test_categorical_absent_key_maps_to_none() -> None:
    out = state_to_continuous({})
    assert out["volatility_phase"] is None
    assert out["momentum_direction"] is None


def test_both_canonical_squeeze_values_survive() -> None:
    on = state_to_continuous({"volatility_phase": "squeeze_on"})
    off = state_to_continuous({"volatility_phase": "squeeze_off"})
    assert on["volatility_phase"] == "squeeze_on"
    assert off["volatility_phase"] == "squeeze_off"


# ---------------------------------------------------------------------------
# Phase C — member identity directory collection
# ---------------------------------------------------------------------------


def _comp_payload() -> dict:
    return {
        "scope": {"scope_type": "concept", "scope_key": "X"},
        "trade_date": "2026-08-24",
        "leadership": {
            "status": "ready",
            "current_leader_ids": ["01060b6b-82cb-4704-88c7-34c67c5ea82c"],
            "previous_leader_ids": ["0473e2f3-91b4-4526-abcc-a5e12cfb9fc1"],
            "entrant_ids": ["33ff5303-5f2f-45c1-b6c6-7809ff402723"],
            "exit_ids": ["202859aa-7b27-4d91-8b0e-ed50034d3c7a"],
        },
        "member_attribution": {
            "direction": {
                "positive": [
                    {"member_id": "01060b6b-82cb-4704-88c7-34c67c5ea82c",
                     "member_name": "01060b6b-82cb-4704-88c7-34c67c5ea82c"},
                ]
            },
            "breadth": {
                "advance": [{"member_id": "0473e2f3-91b4-4526-abcc-a5e12cfb9fc1"}],
            },
            "concentration": {
                "price": {"members": [{"member_id": "6b51182b-152b-424a-b8c3-fd3be97c8155"}]},
            },
        },
    }


def test_collect_composition_member_ids_leadership_and_attribution() -> None:
    ids = collect_composition_member_ids(_comp_payload())
    assert ids == {
        "01060b6b-82cb-4704-88c7-34c67c5ea82c",
        "0473e2f3-91b4-4526-abcc-a5e12cfb9fc1",
        "33ff5303-5f2f-45c1-b6c6-7809ff402723",
        "202859aa-7b27-4d91-8b0e-ed50034d3c7a",
        "6b51182b-152b-424a-b8c3-fd3be97c8155",
    }


def test_collect_dedupes_duplicate_ids() -> None:
    """Duplicates collapse to a set; the call-side ``is_uuid`` gate (applied in
    api/review.py before the bulk Instrument query) drops non-UUID strings — the
    collector itself is a generic string collector, the gate lives at the call
    site so both are independently testable."""
    payload = _comp_payload()
    payload["member_attribution"]["direction"]["positive"].append(
        {"member_id": "01060b6b-82cb-4704-88c7-34c67c5ea82c"}  # duplicate
    )
    payload["leadership"]["current_leader_ids"].append("not-a-uuid")
    ids = collect_composition_member_ids(payload)
    assert len(ids) == 6  # 5 unique uuids + the raw non-uuid string
    assert "not-a-uuid" in ids
    # call-site gate: only well-formed UUIDs survive to the bulk query
    assert {i for i in ids if is_uuid(i)} == {
        "01060b6b-82cb-4704-88c7-34c67c5ea82c",
        "0473e2f3-91b4-4526-abcc-a5e12cfb9fc1",
        "33ff5303-5f2f-45c1-b6c6-7809ff402723",
        "202859aa-7b27-4d91-8b0e-ed50034d3c7a",
        "6b51182b-152b-424a-b8c3-fd3be97c8155",
    }


def test_collect_empty_and_non_dict() -> None:
    assert collect_composition_member_ids(None) == set()
    assert collect_composition_member_ids({}) == set()
    assert collect_composition_member_ids("nope") == set()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("01060b6b-82cb-4704-88c7-34c67c5ea82c", True),
        ("not-a-uuid", False),
        (123, False),
        (None, False),
        ("", False),
        ("01060b6b-82cb-4704-88c7-34c67c5ea82cx", False),
    ],
)
def test_is_uuid_gate(value, expected) -> None:
    assert is_uuid(value) is expected


def test_collect_does_not_touch_unrelated_fields() -> None:
    payload = {
        "scope": {"scope_key": "0c98f409-bcfc-4f93-b1f0-dc5bde6684de"},
        "capability": {"persistence_activated": True},
        "historical_dynamics": {"position": 32.77},
        "trade_date": "2026-08-24",
    }
    ids = collect_composition_member_ids(payload)
    assert ids == set()


def test_collect_member_id_string_field() -> None:
    """A top-level string valued member_id (unusual but possible) is collected."""
    payload = {"member_attribution": {"some": {"member_id": str(uuid.uuid4())}}}
    ids = collect_composition_member_ids(payload)
    assert len(ids) == 1
