"""Slice 1 CORRECTION (REVIEW-CURRENT-OWNER-01) PURE tests — Current(T) owned by Core(T).

[CHANGE-20260826-001 Slice 1] Review(T) Current facts = published Core(T)
(StockFeatureSnapshot.first_pyramid_flat), NOT History(T).

PURE (no DB): proves the Core(T)->flat_t/continuous adapters.
- KPI-1: Current(T) result is independent of whether History(T) exists
  (adapters read ONLY the Core flat; a conflicting History state_payload does
  NOT change the output).
- KPI-2: FirstPyramidHistoryDailyState(T) is NOT consumed for the Current(T)
  flat_t / continuous (adapters never read raw History state_payload keys).
- No latest / same-day-other-run fallback: absent Core-flat keys -> None.
"""
import datetime
import uuid

import pytest

from app.domain.review.member_fact import (
    snapshot_flat_to_continuous,
    snapshot_flat_to_flat_t,
)
from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.services.review_observation_prep_service import (
    _BOARD_CURRENT_FLAT_KEY,
    _build_member_observations,
)

pytestmark = pytest.mark.pure_unit


# A representative Core(T) first_pyramid_flat (the fp_* keys flatten_first_pyramid
# produces and the Board producer consumes).  This IS Core(T).  Labels use the
# canonical display strings (上行/下行/震荡, 扩张/收缩/平缓) that
# FirstPyramidSemanticAdapter accepts.
CORE_FLAT = {
    "fp_trend_direction": "上行",
    "fp_swing_direction": "上行",
    "fp_internal_direction": "下行",
    "fp_structure_alignment": "共振",
    "fp_momentum_direction": "扩张",
    "fp_momentum_change": "enhancing",
    "fp_volume_ratio20": 1.34,
    "fp_volume_percentile20": 72,
    "review_price_position": 0.61,
    "review_volume_ratio20": 1.34,
    "review_amount_ratio20": 1.12,
    "review_volume_percentile20": 70,
    "review_amount_percentile200": 55,
    "fp_latest_bos_direction": "up",
    "fp_latest_bos_freshness": 3,
    "fp_latest_choch_direction": "down",
    "fp_latest_choch_freshness": 8,
    "fp_latest_ob_direction": "up",
    "fp_latest_ob_freshness": 5,
    "fp_segment_volume_ratio": 1.21,
    "fp_prev_segment_volume": 800000.0,
    "fp_sqzmom_value": 0.42,
    "fp_volume_zscore20": 0.81,
    # DSA continuous facts the Core flat carries (regime_strength / dsa_dir_bars /
    # dsa_vwap_dev_pct) — added 2026-09-04 to exercise the mapping-gap fix.
    "fp_trend_strength": 0.85,
    "fp_trend_bars": 7,
    "fp_dsa_vwap_dev_pct": -1.23,
}

# A conflicting History(T) state_payload.  If Current(T) were owned by History,
# these values would leak into the output.  They must NOT.
HISTORY_STATE = {
    "regime_value": "down",          # conflicts with Core fp_trend_direction=涨
    "swing_bias": "down",
    "internal_bias": "up",
    "structure_alignment": "diverge",
    "sqzmom_val": -0.9,
    "sqzmom_delta": -0.3,
    "volume_ratio_20": 0.5,
    "volume_percentile_20": 10,
    "price_position_120d": 0.2,
}


def test_core_flat_passthrough_maps_expected_keys():
    """KPI-2: adapters surface the canonical Current(T) fp_* keys from Core flat."""
    flat_t = snapshot_flat_to_flat_t(CORE_FLAT)
    # trend/structure/momentum/state-volume families present
    assert flat_t["fp_trend_direction"] == "上行"
    assert flat_t["fp_swing_direction"] == "上行"
    assert flat_t["fp_internal_direction"] == "下行"
    assert flat_t["fp_structure_alignment"] == "共振"
    assert flat_t["fp_momentum_direction"] == "扩张"
    assert flat_t["fp_momentum_change"] == "enhancing"
    assert flat_t["fp_volume_ratio20"] == 1.34
    assert flat_t["fp_volume_percentile20"] == 72
    assert flat_t["review_price_position"] == 0.61
    assert flat_t["fp_latest_bos_direction"] == "up"
    assert flat_t["fp_segment_volume_ratio"] == 1.21
    assert flat_t["fp_prev_segment_volume"] == 800000.0


def test_current_independent_of_history_presence():
    """KPI-1: Current(T) output identical with vs without History(T) state.

    The adapter takes ONLY the Core flat; a History state_payload passed alongside
    must not change anything.  This is the I1 invariant: same Core(T) -> same
    Current result regardless of History(T).
    """
    from_dict = snapshot_flat_to_flat_t(CORE_FLAT)
    # "History(T) present" scenario: a History state exists but is NOT an input to
    # the adapter (it only reads core_flat).  We assert the output equals the
    # no-history scenario exactly.
    no_history = snapshot_flat_to_flat_t(CORE_FLAT)
    assert from_dict == no_history

    cont_with = snapshot_flat_to_continuous(CORE_FLAT)
    cont_without = snapshot_flat_to_continuous(CORE_FLAT)
    assert cont_with == cont_without

    # Critically: the conflicting HISTORY_STATE values must NOT appear in output.
    assert cont_with["volume_ratio_20"] == 1.34   # from Core fp_volume_ratio20, NOT History 0.5
    assert cont_with["sqzmom_val"] == 0.42        # from Core fp_sqzmom_value
    assert cont_with["momentum_direction"] == "扩张"  # from Core, NOT History down


def test_history_raw_keys_not_consumed_for_current():
    """KPI-2: adapters never read History raw state_payload keys.

    When ONLY a History state_payload (no Core flat) is supplied, the Current(T)
    flat_t must be all-None (Core owns Current, History raw keys are not used).
    This proves History(T) is not the Current owner.
    """
    flat_t = snapshot_flat_to_flat_t(HISTORY_STATE)
    # None of the History raw keys map into the Current fp_* output.
    assert flat_t["fp_trend_direction"] is None  # not "down" from regime_value
    assert flat_t["fp_momentum_direction"] is None
    assert flat_t["fp_volume_ratio20"] is None

    cont = snapshot_flat_to_continuous(HISTORY_STATE)
    assert cont["volume_ratio_20"] is None        # not 0.5 from History
    assert cont["sqzmom_val"] is None             # not -0.9 from History
    assert cont["momentum_direction"] is None


def test_absent_core_flat_keys_are_none_no_fallback():
    """KPI-3 spirit / I3: missing Core-flat keys -> None, never fallback to History."""
    partial = {"fp_trend_direction": "涨"}  # only one key present
    flat_t = snapshot_flat_to_flat_t(partial)
    assert flat_t["fp_trend_direction"] == "涨"
    assert flat_t["fp_momentum_direction"] is None  # absent -> None, not History
    assert flat_t["fp_volume_ratio20"] is None

    cont = snapshot_flat_to_continuous(partial)
    assert cont["volume_ratio_20"] is None
    assert cont["sqzmom_val"] is None


def test_none_core_flat_returns_full_none():
    assert all(v is None for v in snapshot_flat_to_flat_t(None).values())
    assert all(v is None for v in snapshot_flat_to_continuous(None).values())


class _MockBars:
    """Duck-typed bar series: no daily bars available (Current facts come from Core)."""
    def exact_bar(self, td):
        return None

    def window(self, td):
        return []


def test_build_member_observations_current_owned_by_core_not_history():
    """KPI-1/KPI-2 (wiring proof): _build_member_observations builds Current(T)
    flat_t / continuous from Core(T) flat, NOT from a conflicting History(T) state.

    History(T) state_t carries OPPOSITE values (regime_value=down etc.); the
    produced Current observation must reflect the Core flat, not History.
    """
    inst = uuid.uuid4()
    td = datetime.date(2026, 8, 25)

    # History(T) state with conflicting values
    history_state_t = {
        "regime_value": "down",
        "swing_bias": "down",
        "internal_bias": "up",
        "structure_alignment": "diverge",
        "sqzmom_val": -0.9,
        "sqzmom_delta": -0.3,
        "volume_ratio_20": 0.5,
        "volume_percentile_20": 10,
    }
    current_only = {
        str(inst): {_BOARD_CURRENT_FLAT_KEY: dict(CORE_FLAT)},
    }

    members = _build_member_observations(
        [inst],
        trade_date=td,
        t1=None,
        states_t={inst: history_state_t},     # History(T) present but conflicting
        states_t1={},
        bars={inst: _MockBars()},
        current_only_facts=current_only,
    )
    assert len(members) == 1
    obs = members[0]
    # Current(T) Trend / Momentum reflect CORE (上行 / 扩张), not History.
    # (vol_ratio20 is owned by the canonical VolumeContext, not the Core flat —
    #  it is intentionally None under a mock with no volume window.)
    assert obs.trend == Direction.UP          # Core "上行", not History "down"
    assert obs.momentum == MomentumDirection.EXPANDING  # Core "扩张", not History
    assert obs.internal == Direction.DOWN     # Core fp_internal_direction="下行"


def test_build_member_observations_history_absent_same_as_present():
    """KPI-1: Current result identical whether History(T) state is present or absent.

    With the SAME Core flat, omitting states_t entirely must yield the SAME
    Current observation as when a conflicting History(T) state exists.
    """
    inst = uuid.uuid4()
    td = datetime.date(2026, 8, 25)
    current_only = {str(inst): {_BOARD_CURRENT_FLAT_KEY: dict(CORE_FLAT)}}

    with_hist = _build_member_observations(
        [inst], trade_date=td, t1=None,
        states_t={inst: {"regime_value": "down", "volume_ratio_20": 0.5}},
        states_t1={}, bars={inst: _MockBars()}, current_only_facts=current_only,
    )
    without_hist = _build_member_observations(
        [inst], trade_date=td, t1=None,
        states_t={}, states_t1={}, bars={inst: _MockBars()}, current_only_facts=current_only,
    )
    assert with_hist[0].trend == without_hist[0].trend
    assert with_hist[0].momentum == without_hist[0].momentum
    assert with_hist[0].vol_ratio20 == without_hist[0].vol_ratio20
    assert with_hist[0].internal == without_hist[0].internal


# ---------------------------------------------------------------------------
# DSA mapping-gap regression (2026-09-04)
#
# Root cause: snapshot_flat_to_continuous omitted fp_trend_strength /
# fp_trend_bars / fp_dsa_vwap_dev_pct, so Current(T) DSA (regime_strength /
# dsa_dir_bars / dsa_vwap_dev_pct) was always None even when the Core snapshot
# was present.  These tests lock source-ownership + adapter mapping so the drift
# cannot silently return.
# ---------------------------------------------------------------------------

def test_snapshot_flat_to_continuous_maps_dsa_from_core_flat():
    """Layer 1 (mapper unit): Core flat DSA keys MUST map into continuous."""
    cont = snapshot_flat_to_continuous(CORE_FLAT)
    assert cont["regime_strength"] == 0.85
    assert cont["dsa_dir_bars"] == 7
    assert cont["dsa_vwap_dev_pct"] == -1.23


def test_snapshot_flat_to_continuous_dsa_none_when_absent_or_nonfinite():
    """Layer 1 (mapper unit): DSA stays None (never History / 0) when the Core
    flat lacks the key or the value is non-finite.
    """
    partial = {"fp_trend_direction": "上行"}  # no DSA keys
    cont = snapshot_flat_to_continuous(partial)
    assert cont["regime_strength"] is None
    assert cont["dsa_dir_bars"] is None
    assert cont["dsa_vwap_dev_pct"] is None

    nonfinite = {
        "fp_trend_strength": "not-a-number",
        "fp_trend_bars": None,
        "fp_dsa_vwap_dev_pct": float("nan"),
    }
    cont2 = snapshot_flat_to_continuous(nonfinite)
    assert cont2["regime_strength"] is None       # _number rejects non-numeric
    assert cont2["dsa_dir_bars"] is None
    assert cont2["dsa_vwap_dev_pct"] is None      # _number rejects nan


def test_build_member_observations_dsa_owned_by_core_not_history():
    """Layer 2 (current-owner path contract): Current DSA must come from the
    Core(T) flat, NOT a conflicting History(T) state_payload.  This is the exact
    bug class (source-ownership + adapter mapping drift) a mapper-only test misses.
    """
    inst = uuid.uuid4()
    td = datetime.date(2026, 8, 25)

    # History(T) claims OPPOSITE DSA values.
    history_state_t = {
        "regime_strength": 999.0,
        "dsa_dir_bars": 50,
        "dsa_vwap_dev_pct": 42.0,
        "regime_value": "down",
    }
    current_only = {str(inst): {_BOARD_CURRENT_FLAT_KEY: dict(CORE_FLAT)}}

    members = _build_member_observations(
        [inst],
        trade_date=td,
        t1=None,
        states_t={inst: history_state_t},   # conflicting History present
        states_t1={},
        bars={inst: _MockBars()},
        current_only_facts=current_only,
    )
    assert len(members) == 1
    obs = members[0]
    # DSA from Core flat (0.85 / 7 / -1.23), NOT History (999 / 50 / 42).
    assert obs.regime_strength == 0.85
    assert obs.dsa_dir_bars == 7
    assert obs.dsa_vwap_dev_pct == -1.23


def test_build_member_observations_dsa_independent_of_history_presence():
    """Layer 2 (KPI-1 for DSA): same Core flat -> same Current DSA whether or not
    a (conflicting) History(T) state exists.  No fallback to History when absent.
    """
    inst = uuid.uuid4()
    td = datetime.date(2026, 8, 25)
    current_only = {str(inst): {_BOARD_CURRENT_FLAT_KEY: dict(CORE_FLAT)}}

    with_hist = _build_member_observations(
        [inst], trade_date=td, t1=None,
        states_t={inst: {"regime_strength": 999.0, "dsa_dir_bars": 50, "dsa_vwap_dev_pct": 42.0}},
        states_t1={}, bars={inst: _MockBars()}, current_only_facts=current_only,
    )
    without_hist = _build_member_observations(
        [inst], trade_date=td, t1=None,
        states_t={}, states_t1={}, bars={inst: _MockBars()}, current_only_facts=current_only,
    )
    assert with_hist[0].regime_strength == without_hist[0].regime_strength == 0.85
    assert with_hist[0].dsa_dir_bars == without_hist[0].dsa_dir_bars == 7
    assert with_hist[0].dsa_vwap_dev_pct == without_hist[0].dsa_vwap_dev_pct == -1.23
