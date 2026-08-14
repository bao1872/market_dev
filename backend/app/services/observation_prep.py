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

import pandas as pd

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import MemberObservation
from app.services.first_pyramid_semantic_adapter import FirstPyramidSemanticAdapter
from app.services.volume_context import (
    compute_volume_context_series,
    extract_last_volume_context,
)


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


def _categorical(value: Any) -> str | None:
    """Normalize a canonical categorical fact to a non-empty string or ``None``.

    Used for Current-only categorical facts (e.g. Momentum/Volume Relation) that
    Review consumes verbatim from the canonical producer.  Blank/absent -> None so
    the fact reports unavailable instead of surfacing an empty category.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    T-1 is missing they are ``None`` (never an earlier bar).

    ``volume_history`` / ``amount_history`` are ascending **STRICT-PRIOR** bar
    histories: they EXCLUDE the current bar T.  T is carried separately by
    ``volume_t`` / ``amount_t`` and is appended exactly once by the canonical
    volume owner.  Including T in the history as well would double-count it.
    Both series MUST be built from the same prior bars so that index ``i`` refers
    to the same trade_date in both (bar-aligned); they are used only for the
    shared ``vol/amt_ratio20`` SSOT.
    """

    member_id: str
    flat_t: Mapping[str, Any]
    close_t: float | None
    amount_t: float | None
    volume_t: float | None = None
    volume_history: tuple[float | None, ...] = ()
    amount_history: tuple[float | None, ...] = ()
    flat_t1: Mapping[str, Any] | None = None
    close_t1: float | None = None
    # Continuous member facts from the history FirstPyramidHistoryDailyState
    # state_payload (PRD §7.3-§7.6).  Additive: defaults empty; missing keys yield
    # None (never 0) at the observation boundary.
    continuous: Mapping[str, Any] = field(default_factory=dict)
    # Current-only canonical facts resolved from the exact-T StockFeatureSnapshot
    # (``summary_payload.first_pyramid_flat``).  These facts have no member-day
    # history series, so Current is served while Historical Dynamics stays
    # unavailable (PRD v2.3).  ``None``/absent -> the fact is unavailable; there is
    # never a fallback to a "latest"/T+1 snapshot.
    current_only: Mapping[str, Any] | None = None


def _compute_volume_context_canonical(raw: RawMemberFacts):
    """Hand the prepared bars (strict-prior history + T) to the canonical First
    Pyramid VolumeContext owner and return the T row.

    This is the ONLY volume-math owner for the Review scope-prep path.  Review
    never re-implements rolling MA / percentile / z-score; it reuses
    ``compute_volume_context_series`` verbatim so that Review T facts are
    bit-identical to the canonical series T row (no 19/20 or 199/200 one-bar
    drift, no divergent 0/negative-volume handling).
    """
    vol_hist = [_finite(v) for v in raw.volume_history]
    amt_hist = [_finite(v) for v in raw.amount_history]
    vol_t = _finite(raw.volume_t)
    amt_t = _finite(raw.amount_t)
    # Build a T-inclusive bar series.  Volume is the mandatory dimension: a bar is
    # included only if its VOLUME is finite.  A missing/invalid amount for an
    # included bar becomes NaN (NOT dropped), so volume is never truncated by a
    # shorter amount history.  We pair volume/amount bar-by-bar with zip_longest so a
    # length-mismatched amount history cannot discard valid volume bars.
    # compute_volume_context_series uses a rolling window that EXCLUDES the current
    # bar (vals[max(0, i-w):i]); the last row is T, so its window = the strict-prior
    # history, exactly matching the prior semantics.
    from itertools import zip_longest

    vols: list[float] = []
    amts: list[float] = []
    for v, a in zip_longest(vol_hist, amt_hist):
        if v is None:
            continue
        vols.append(float(v))
        amts.append(float(a) if a is not None else float("nan"))
    if vol_t is not None:
        vols.append(float(vol_t))
        amts.append(float(amt_t) if amt_t is not None else float("nan"))
    if not vols:
        return None
    df = pd.DataFrame({"volume": vols, "amount": amts})
    series = compute_volume_context_series(df)
    return extract_last_volume_context(series)


def build_member_observation(raw: RawMemberFacts) -> MemberObservation:
    """Build a ``MemberObservation`` from already-resolved canonical facts."""
    trend, swing, internal, momentum = _flat_semantics(raw.flat_t)
    t1_trend, t1_swing, t1_internal, t1_momentum = _flat_semantics(raw.flat_t1)
    close_t = _finite(raw.close_t)
    cont = raw.continuous or {}
    current_only = raw.current_only or {}
    # VOLUME SSOT (REVIEW-V23-A-CORRECTION-2): Review does NOT own a second rolling
    # formula.  The prepared bars through T are handed to the canonical First Pyramid
    # VolumeContext owner; we extract the last (T) row.  This guarantees identical
    # MA20/MA200 window semantics, 0/negative-volume handling, percentile gate and
    # readiness to compute_volume_context_series (no 19/20 or 199/200 one-bar drift).
    vc = _compute_volume_context_canonical(raw)
    # 2026-08-13 CORRECTION: 200D facts 仅在 readiness_200 满足（完整 >=200 根
    # history）时产出；25D history 不得产生 200D fact。  Readiness is taken from the
    # canonical owner's produced 200D fields (None => window not satisfied).
    vc_ready_200 = vc.readiness_200 if (vc and vc.readiness_200) else False
    ratio200 = vc.volume_ratio_200 if (vc and vc_ready_200) else None
    pct20 = vc.volume_percentile_20 if vc else None
    pct200 = vc.volume_percentile_200 if (vc and vc_ready_200) else None
    z20 = vc.volume_zscore_20 if vc else None
    z200 = vc.volume_zscore_200 if (vc and vc_ready_200) else None
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
        # VOLUME SSOT (REVIEW-V23-A-CORRECTION-2): 20D/200D facts all come from the
        # single canonical VolumeContext owner (vc).  No Review-local compute_ratio.
        vol_ratio20=vc.volume_ratio_20 if vc else None,
        amt_ratio20=vc.amount_ratio_20 if (vc and vc.amount_ratio_20 is not None) else None,
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
        seg_amt_mean_prev=cont.get("prev_segment_amount_mean"),
        # STRUCTURE categorical fact (PRD §7.4 B) — canonical Structure Alignment
        # value verbatim from the raw state payload ("aligned" / "divergent" / None).
        # NOT the numeric continuous cast.
        structure_alignment_categorical=raw.flat_t.get("structure_alignment")
        if raw.flat_t
        else None,
        active_internal_ob_count=cont.get("active_internal_ob_count"),
        active_swing_ob_count=cont.get("active_swing_ob_count"),
        # MOMENTUM canonical facts (PRD §7.5) — inherited from First Pyramid.
        # ``volatility_phase`` / ``momentum_direction`` are the canonical stored
        # values; Review maps them through FirstPyramidSemanticAdapter (no re-derive).
        volatility_phase=cont.get("volatility_phase"),
        momentum_direction_raw=cont.get("momentum_direction"),
        momentum_change=cont.get("momentum_change"),
        sqzmom_delta=cont.get("sqzmom_delta"),
        sqzmom_val=cont.get("sqzmom_val"),
        # CURRENT-ONLY canonical facts from the exact-T StockFeatureSnapshot
        # (REVIEW-V23-A-CORRECTION-3).  Numeric facts are finite-guarded; the
        # Momentum/Volume Relation is a canonical categorical string consumed
        # verbatim (Review does NOT own that algorithm and must not re-derive it).
        # Absent snapshot / absent field -> None -> the fact reports unavailable.
        release_volume_ratio=_finite(current_only.get("release_volume_ratio")),
        momentum_volume_relation=_categorical(
            current_only.get("momentum_volume_relation")
        ),
        bb_position=_finite(current_only.get("bb_position")),
        bb_width=_finite(current_only.get("bb_width")),
        vwap_ret_total=_finite(current_only.get("vwap_ret_total")),
        trailing_top_pct=_finite(current_only.get("trailing_top_pct")),
        trailing_bottom_pct=_finite(current_only.get("trailing_bottom_pct")),
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
