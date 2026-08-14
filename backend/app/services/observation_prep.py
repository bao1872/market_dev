"""Canonical Observation Data Preparation — pure mapping (Round 1B).

Maps already-resolved canonical facts (exact-T1 resolved by the caller) into
``MemberObservation`` inputs consumed by the Canonical Scope Observation Core
(``app.domain.review.scope_observation``).  This module is pure: it never touches
the database, never resolves membership, never loads bars, and never guesses the
canonical previous trading day.

Reuse / no second mapping
-------------------------
- The T and exact T-1 semantic contract is the SAME canonical path:
  ``FirstPyramidSemanticAdapter`` normalizes the flat keys (produced by
  ``member_fact.previous_state_to_flat`` from a canonical daily_state payload)
  into ``Direction`` / ``MomentumDirection`` enums.  No second trend/BOS/CHoCH
  re-interpretation happens here.
- ``vol_ratio20`` / ``amt_ratio20`` reuse the shared SSOT formula
  ``member_fact.compute_ratio`` (current / prior-window mean), identical to the
  existing Review pipeline.

Exact-T1 boundary (HARD)
------------------------
``close_t1`` / ``flat_t1`` are the EXACT canonical T-1 (resolved upstream from
the trading calendar).  A member whose exact T-1 is missing simply carries
``None`` here; this module never falls back to an earlier bar (T-2/T-3).

``price_candidate`` = close(T) available, independently of ``return_1d``:
candidate vs valid are kept as the two-layer semantics of the Core.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.member_fact import compute_ratio
from app.domain.review.scope_observation import MemberObservation
from app.services.first_pyramid_semantic_adapter import FirstPyramidSemanticAdapter
from app.services.volume_context import compute_volume_context_from_series


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compute_exact_return(close_t: float | None, close_t1: float | None) -> float | None:
    """close(T) / close(T-1) - 1 via the exact canonical T-1.

    Returns ``None`` when either close is missing/non-finite (never searches an
    earlier bar).  This matches the Core's ratio semantics (0.01 == +1%).
    """
    current = _finite(close_t)
    previous = _finite(close_t1)
    if current is None or previous is None or abs(previous) <= 1e-12:
        return None
    return current / previous - 1.0


def _flat_semantics(
    flat: Mapping[str, Any] | None,
) -> tuple[Direction | None, Direction | None, Direction | None, MomentumDirection | None]:
    """Canonical T/T-1 semantic contract via the shared boundary adapter."""
    if not flat:
        return (None, None, None, None)
    adapter = FirstPyramidSemanticAdapter(flat)
    return (adapter.trend, adapter.swing, adapter.internal, adapter.momentum_direction)


@dataclass(frozen=True)
class RawMemberFacts:
    """Resolved canonical raw facts for one member on trade date T.

    ``close_t1`` / ``flat_t1`` are the EXACT canonical T-1 facts; when the exact
    T-1 is missing they are ``None`` (never an earlier bar).  ``volume_history``
    / ``amount_history`` are ascending bar histories (including the current bar)
    used only for the shared ``vol/amt_ratio20`` SSOT.
    """

    member_id: str
    flat_t: Mapping[str, Any]
    close_t: float | None
    amount_t: float | None
    volume_t: float | None = None
    volume_history: tuple[float, ...] = ()
    amount_history: tuple[float, ...] = ()
    flat_t1: Mapping[str, Any] | None = None
    close_t1: float | None = None
    # Continuous member facts from the history FirstPyramidHistoryDailyState
    # state_payload (PRD §7.3-§7.6).  Additive: defaults empty; missing keys yield
    # None (never 0) at the observation boundary.
    continuous: Mapping[str, Any] = field(default_factory=dict)


def build_member_observation(raw: RawMemberFacts) -> MemberObservation:
    """Build a ``MemberObservation`` from already-resolved canonical facts."""
    trend, swing, internal, momentum = _flat_semantics(raw.flat_t)
    t1_trend, t1_swing, t1_internal, t1_momentum = _flat_semantics(raw.flat_t1)
    close_t = _finite(raw.close_t)
    cont = raw.continuous or {}
    vc = compute_volume_context_from_series(_finite(raw.volume_t), list(raw.volume_history))
    ratio200 = vc.volume_ratio_200 if vc else None
    pct20 = vc.volume_percentile_20 if vc else None
    pct200 = vc.volume_percentile_200 if vc else None
    z20 = vc.volume_zscore_20 if vc else None
    z200 = vc.volume_zscore_200 if vc else None
    return MemberObservation(
        member_id=raw.member_id,
        # candidate = close(T) available, independent of return availability.
        price_candidate=close_t is not None,
        return_1d=compute_exact_return(close_t, _finite(raw.close_t1)),
        amount=_finite(raw.amount_t),
        trend=trend,
        swing=swing,
        internal=internal,
        momentum=momentum,
        t1_trend=t1_trend,
        t1_swing=t1_swing,
        t1_internal=t1_internal,
        t1_momentum=t1_momentum,
        vol_ratio20=compute_ratio(_finite(raw.volume_t), list(raw.volume_history), 20),
        amt_ratio20=compute_ratio(_finite(raw.amount_t), list(raw.amount_history), 20),
        # TOTAL VOLUME (PRD §7.2) — raw bar volume, summed at scope level.
        volume_t=_finite(raw.volume_t),
        # VOLUME 20D/200D six-fact vector (PRD §7.5) — single canonical owner.
        vol_ratio200=ratio200,
        vol_pct20=pct20,
        vol_pct200=pct200,
        vol_zscore20=z20,
        vol_zscore200=z200,
        # TREND continuous facts (PRD §7.3).
        regime_strength=cont.get("regime_strength"),
        dsa_dir_bars=cont.get("dsa_dir_bars"),
        dsa_vwap_dev_pct=cont.get("dsa_vwap_dev_pct"),
        segment_id=cont.get("segment_id"),
        segment_direction=cont.get("segment_direction"),
        segment_bars=cont.get("segment_bars"),
        segment_change_pct=cont.get("segment_change_pct"),
        segment_slope=cont.get("segment_slope"),
        seg_vol_ratio=cont.get("current_vs_prev_volume_mean_ratio"),
        seg_amt_ratio=cont.get("current_vs_prev_amount_mean_ratio"),
        seg_vol_mean=cont.get("current_segment_volume_mean"),
        seg_amt_mean_prev=cont.get("prev_segment_volume_mean"),
        # STRUCTURE continuous facts (PRD §7.4 B).
        structure_alignment=cont.get("structure_alignment"),
        active_internal_ob_count=cont.get("active_internal_ob_count"),
        active_swing_ob_count=cont.get("active_swing_ob_count"),
        # MOMENTUM continuous facts (PRD §7.5).
        volatility_phase=cont.get("volatility_phase"),
        momentum_direction_raw=cont.get("momentum_direction"),
        momentum_change=cont.get("momentum_change"),
        sqzmom_delta=cont.get("sqzmom_delta"),
        sqzmom_val=cont.get("sqzmom_val"),
    )


def _is_finite(value: Any) -> bool:
    if value is None:
        return True
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _ratio_sum(*ratios: Any) -> Any:
    """Sum ratios, treating ``None`` (empty denominator) as absent.

    When every ratio is ``None`` (denominator == 0) returns ``None``; otherwise
    returns the numeric sum of the non-None ratios.
    """
    present = [r for r in ratios if r is not None]
    if not present:
        return None
    return sum(present)


def check_observation_invariants(obs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pure sanity checks over a Scope Observation result (prompt §11).

    Returns a list of ``{"name", "ok", "detail"}`` records.  Every check is
    invariant over the Core output; no DB access.
    """
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    scope = obs["scope"]
    pit_count = scope["pit_member_count"]
    provided = scope["provided_member_count"]
    add("provided_le_pit", provided <= pit_count, f"{provided} <= {pit_count}")

    price = obs["price"]
    add("price_valid_le_candidate", price["valid_count"] <= price["candidate_count"],
        f"{price['valid_count']} <= {price['candidate_count']}")
    add("missing_exact_t1_ge_0", price["missing_exact_t1_count"] >= 0,
        price["missing_exact_t1_count"])

    breadth = price["breadth"]
    bsum = breadth["advance_count"] + breadth["decline_count"] + breadth["unchanged_count"]
    add("price_breadth_sums_denominator", bsum == breadth["denominator"],
        f"{bsum} == {breadth['denominator']}")

    for axis in ("trend",):
        state = obs[axis]["state"]
        den = state["denominator"]
        total = sum(state[k] for k in ("up_count", "neutral_count", "down_count"))
        add(f"{axis}_categorical_sums", total == den, f"{total} == {den}")
        ratio_sum = _ratio_sum(state["up_ratio"], state["neutral_ratio"], state["down_ratio"])
        add(f"{axis}_categorical_ratios_1",
            (ratio_sum is None) or abs((ratio_sum or 0.0) - 1.0) < 1e-9, ratio_sum)

    for axis in ("swing", "internal"):
        state = obs["structure"][axis]["state"]
        den = state["denominator"]
        total = sum(state[k] for k in ("up_count", "neutral_count", "down_count"))
        add(f"{axis}_categorical_sums", total == den, f"{total} == {den}")
        ratio_sum = _ratio_sum(state["up_ratio"], state["neutral_ratio"], state["down_ratio"])
        add(f"{axis}_categorical_ratios_1",
            (ratio_sum is None) or abs((ratio_sum or 0.0) - 1.0) < 1e-9, ratio_sum)

    mom_state = obs["momentum"]["state"]
    mden = mom_state["denominator"]
    mtotal = sum(mom_state[k] for k in ("expanding_count", "flat_count", "contracting_count"))
    add("momentum_categorical_sums", mtotal == mden, f"{mtotal} == {mden}")
    mratio = _ratio_sum(
        mom_state["expanding_ratio"], mom_state["flat_ratio"], mom_state["contracting_ratio"],
    )
    add("momentum_categorical_ratios_1",
        (mratio is None) or abs((mratio or 0.0) - 1.0) < 1e-9, mratio)

    common_pit = min(
        scope.get("pit_member_count_t1", 0) or 0,
        pit_count,
    )
    for axis, obj in (
        ("trend", obs["trend"]),
        ("swing", obs["structure"]["swing"]),
        ("internal", obs["structure"]["internal"]),
        ("momentum", obs["momentum"]),
    ):
        tden = obj["transition"]["denominator"]
        add(f"{axis}_transition_den_le_common", tden <= common_pit,
            f"{tden} <= {common_pit}")

    for kind, conc in (
        ("price", price["concentration"]),
        ("amount", price["amount"]["concentration"]),
    ):
        if conc.get("status") == "ready":
            hhi = conc.get("raw_hhi")
            add(f"{kind}_hhi_in_range", hhi is not None and 0.0 < hhi <= 1.0 + 1e-9, hhi)
        else:
            add(f"{kind}_hhi_in_range", True, conc.get("status"))

    # All numeric outputs finite (deep, cheap scan over scalars).
    bad: list[str] = []
    for section in ("price", "trend", "structure", "momentum", "participation"):
        _scan_finite(obs[section], section, bad)
    add("all_numeric_finite", not bad, bad[:10])

    return checks


def _scan_finite(node: Any, path: str, bad: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _scan_finite(value, f"{path}.{key}", bad)
    elif isinstance(node, (list, tuple)):
        for idx, value in enumerate(node):
            _scan_finite(value, f"{path}[{idx}]", bad)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        if not _is_finite(node):
            bad.append(path)
