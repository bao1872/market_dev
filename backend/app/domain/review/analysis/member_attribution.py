"""Member Attribution — deterministic member-level explanation of canonical Scope facts.

``REVIEW-MEMBER-ATTRIBUTION-CLOSURE``.  This stage is NOT a threshold/research
experiment.  There is no score / classification / variant / percentile search.
The only job is:

    canonical Scope aggregates
        -> deterministic, lossless member-level decomposition
        -> deterministic sorting
        -> reconciliation (prove the member sum reproduces the published fact)

It only answers "which members caused the current scope state".  It never
re-judges the state: no Type, no Core-led, no structure label, no composite
score, no interpretation.

Contract (M1) — five Attribution groups, ONE unified member evidence schema:

1. ``direction``      - Canonical fact: ``price.amount_weighted_return``.
   ``contribution_i = aw_weight_i x return_1d_i`` where ``aw_weight`` is the
   amount-weight renormalized inside the canonical AW joint universe.  Sum of
   ``contribution`` == canonical AW.  positive: contribution DESC; negative: ASC.

2. ``capital_tilt``   - Canonical fact: AW - EW.  ``tilt_contribution_i =
   (aw_weight_i - ew_weight_i) x return_1d_i``, with ``aw_weight`` the canonical
   AW joint weight (0 outside U_aw) and ``ew_weight`` the canonical equal weight
   (0 outside U_price).  Because missing weights are exactly 0, the sum over ALL
   members is exactly ``AW - EW`` regardless of universe overlap:
   ``sum(aw_w_i x r_i) = AW`` and ``sum(ew_w_i x r_i) = EW`` by definition.
   positive: tilt DESC; negative: tilt ASC.

3. ``breadth``        - Canonical fact: ``price.breadth`` over the price-valid
   return universe.  Member *sets* (advance / decline / unchanged) — no new
   breadth score.  advance: return DESC; decline: return ASC; unchanged:
   member_id ASC.

4. ``concentration``  - Canonical fact: raw HHI.  ``hhi_contribution_i =
   weight_i^2``.  Price HHI uses abs-return shares over U_price; amount HHI uses
   the canonical amount-share owner over the amount-valid universe.  Sum of
   ``weight_i^2`` == raw HHI (price / amount).  Sorting: hhi DESC, member_id ASC.

5. ``leadership``     - Canonical fact: Leadership Migration.  Consumes
   ``migration.entrant_ids`` / ``migration.exit_ids`` VERBATIM (single owner,
   no recompute).  ``retained = previous ∩ current`` is derived ONLY because the
   canonical contract exposes no ``retained_ids``, and it is validated against
   ``migration.retained_count``.  Sorting: aligned contribution DESC (fallback
   canonical contribution, member_id ASC).

Ownership boundary (single-owner + NO-MIGRATION):

- It does NOT create a second canonical aggregate owner.  EW / AW / HHI /
  Breadth / Leadership Migration stay owned by their single canonical producers.
- It constructs deterministic *member-level decomposition components* inside
  Attribution (aw_weight / ew_weight / hhi weight / breadth membership) and
  reconciles the member sum back to the single canonical aggregate — a
  projection of the canonical aggregate, not a new aggregate formula.
- ``amount_share`` consumed from the single canonical owner
  ``compute_member_amount_contributions``; leadership ``contribution`` consumed
  from ``compute_member_leadership_contributions``.
- Leadership ``entrant_ids`` / ``exit_ids`` are consumed verbatim from the
  canonical ``LeadershipMigrationFacts``; only ``retained`` is derived
  (previous ∩ current) because canonical exposes no ``retained_ids``, and it is
  validated against ``migration.retained_count``.

Raw vs display precision (A1-1):
- Every decomposition component is kept in FULL precision internally
  (``raw_contribution`` / ``raw_tilt`` / ``raw_price_hhi`` / ``raw_amount_hhi``).
- Reconciliation sums RAW values with ``math.fsum`` over a member_id-sorted
  sequence; ranking sorts RAW values.
- ``_quant()`` (6 decimals) is applied ONLY at serialization/display time, never
  before summation or ranking.  The audit sequence is
  ``raw -> {sum -> reconcile, sort -> rank, round -> display}`` — never
  ``raw -> round -> sum -> reconcile``.

Availability semantics (canonical, reused verbatim):

- ``return_1d`` missing / NaN / inf -> not in any return universe ->
  ``contribution`` / ``tilt_contribution`` are ``None`` (never 0).
- ``return_1d == 0`` -> valid member; breadth unchanged; zero contribution.
- amount missing / non-finite / negative -> ``amount_share``/``aw_weight`` None
  -> contribution None (excluded from AW / direction); amount HHI excludes.
- ``amount == 0`` (total > 0) -> legal zero share.
- Leadership ``None != []``.  A legitimately empty leader set is NOT a snapshot
  problem: canonical migration still reports ``status=unavailable`` with
  ``reason=empty_leader_set`` (only jaccard / retention / migration are
  unavailable) while ``retained`` / ``entrant`` / ``exit`` set facts stay valid.
  Attribution mirrors that: group ``status=unavailable`` but the member sets are
  populated and their reconciliation still RUNS.
- ``unavailable_snapshot`` (one side's leader ids unknown) -> transition
  reconciliation is skipped (cannot be verified), never a false PASS.

Determinism (M2): every sort uses ``(primary_key, member_id ASC)``.  Database /
set iteration ordering never affects output.  Same input -> same output always;
verified by ``determinism_checksum`` over the full result.

Pure + deterministic + non-mutating.  No DB, no I/O, no mutation of inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.review.analysis.leadership_contribution import (
    compute_member_leadership_contributions,
)
from app.domain.review.analysis.leadership_migration import LeadershipMigrationFacts
from app.domain.review.scope_observation import MemberObservation

_EPSILON = 1e-12

# Frozen deterministic float tolerance for member-sum vs canonical reconciliation.
# Absolute-bound.  1e-5 comfortably absorbs subtractive-cancellation float residue
# (e.g. a capital tilt whose member sum lands at ~1e-6 when AW == EW), while a real
# decomposition bug (wrong universe / wrong weight / dropped member) produces errors
# of order 1/N ~ 1e-3+ that far exceed it.
RECONCILIATION_TOLERANCE = 1e-5


def _finite(value: Any) -> float | None:
    """Return ``value`` iff it is a finite float, else None (unavailable)."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _deep_get(payload: Mapping[str, Any], *keys: str) -> Any:
    """Read ``payload`` by dotted path; missing -> None (never raises)."""
    node: Any = payload
    for key in keys:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _quant(value: Any) -> Any:
    """Round a finite float to a stable comparison resolution; pass others through."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    return round(float(value), 6)


def checksum(*encodable: Any) -> str:
    """Deterministic SHA-256 checksum over arbitrary JSON-serializable content.

    Used to prove determinism / reproducibility: re-running the same input must
    produce the same checksum.
    """
    digest = hashlib.sha256()
    for item in encodable:
        raw = json.dumps(item, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        digest.update(raw)
        digest.update(b"\x00")
    return digest.hexdigest()


def _evidence(
    member: MemberObservation,
    *,
    member_name_by_id: Mapping[str, str],
    amount_share: float | None,
    aw_weight: float | None,
    ew_weight: float | None,
    canonical_contribution: float | None,
    direction_contribution: float | None,
    tilt_contribution: float | None,
    in_price_universe: bool,
    in_aw_universe: bool,
) -> dict[str, Any]:
    """One row of the unified member evidence schema (M1).

    Every attribution group reuses this exact shape; absent facts are ``None``
    (never 0).  No per-attribution member DTO exists.
    """
    return {
        "member_id": member.member_id,
        "member_name": member_name_by_id.get(member.member_id, member.member_id),
        "return_1d": _quant(_finite(member.return_1d)),
        "amount": _quant(_finite(member.amount)),
        "amount_share": _quant(amount_share),
        "aw_weight": _quant(aw_weight),
        "ew_weight": _quant(ew_weight),
        "contribution": _quant(direction_contribution),
        "canonical_contribution": _quant(canonical_contribution),
        "tilt_contribution": _quant(tilt_contribution),
        "in_price_universe": in_price_universe,
        "in_aw_universe": in_aw_universe,
    }


def compute_member_attribution(
    *,
    members: Sequence[MemberObservation],
    observation: Mapping[str, Any],
    leadership_migration: LeadershipMigrationFacts | None = None,
    member_name_by_id: Mapping[str, str] | None = None,
    tolerance: float = RECONCILIATION_TOLERANCE,
) -> dict[str, Any]:
    """Compute the five Member Attribution groups + reconciliation (M2/M3).

    Args:
        members: canonical prepared ``MemberObservation`` facts (see
            ``scope_observation``), all belonging to PIT(T).
        observation: the canonical L1 payload from
            ``scope_observation.compute_scope_observation`` — the *published*
            facts that member decomposition must reproduce.
        leadership_migration: ``LeadershipMigrationFacts`` from
            ``leadership_migration.compute_leadership_migration``.  Optional: when
            omitted, the ``leadership`` group is ``unavailable``
            (``not_provided``) and its reconciliation is ``skipped`` (never a
            false PASS).
        member_name_by_id: optional display-name map (member_id -> name).  Falls
            back to member_id when absent.

    Returns (fixed contract):
        ``{"scope", "direction", "capital_tilt", "breadth", "concentration",
        "leadership", "reconciliation", "determinism_checksum"}``.
    """
    names = dict(member_name_by_id) if member_name_by_id else {}
    member_list = list(members)

    # ---- canonical published facts (the numbers all decompositions reproduce) --
    canonical_aw = _finite(_deep_get(observation, "price", "amount_weighted_return"))
    canonical_ew = _finite(_deep_get(observation, "price", "equal_weight_return"))
    canonical_breadth = _deep_get(observation, "price", "breadth") or {}
    canonical_price_hhi = _deep_get(observation, "price", "concentration", "raw_hhi")
    canonical_price_hhi_norm = _deep_get(observation, "price", "concentration", "normalized_hhi")
    canonical_amt_hhi = _deep_get(observation, "price", "amount", "concentration", "raw_hhi")
    canonical_amt_hhi_norm = _deep_get(
        observation, "price", "amount", "concentration", "normalized_hhi"
    )

    # ---- canonical member-level facts (single owners, no recompute) -----------
    contribution_facts = compute_member_leadership_contributions(member_list)
    amount_share_by: dict[str, float | None] = {}
    canon_contrib_by: dict[str, float | None] = {}
    for c in contribution_facts.members:
        amount_share_by[c.member_id] = c.amount_share
        canon_contrib_by[c.member_id] = c.contribution

    # ---- universes (mirror the canonical definitions exactly) ------------------
    # U_aw (AW joint universe): return finite AND amount finite >= 0, weights
    # renormalized inside U_aw.  Every internal sum iterates a member_id-sorted
    # sequence so float non-associativity can never depend on supplier order.
    aw_valid_raw = []
    for m in member_list:
        r = _finite(m.return_1d)
        a = _finite(m.amount)
        if r is not None and a is not None and a >= 0.0:
            aw_valid_raw.append((m.member_id, r, a))
    aw_valid: list[tuple[str, float]] = sorted(
        (mid, a) for mid, _, a in aw_valid_raw
    )
    aw_total = sum(a for _, a in aw_valid)
    aw_weights: dict[str, float] = {}
    if aw_total > _EPSILON:
        for mid, a in aw_valid:
            aw_weights[mid] = a / aw_total
    aw_weight_of: dict[str, float | None] = {mid: aw_weights.get(mid) for mid, _ in aw_valid}

    # U_price (price-valid return universe): price_candidate AND return finite.
    price_set = {
        m.member_id
        for m in member_list
        if m.price_candidate and _finite(m.return_1d) is not None
    }
    n_price = len(price_set)
    ew_weight = (1.0 / n_price) if n_price > 0 else None

    # ---- breadth member sets (price universe) ----------------------------------
    advance: list[MemberObservation] = []
    decline: list[MemberObservation] = []
    unchanged: list[MemberObservation] = []
    breadth_unavailable: list[MemberObservation] = []
    for m in member_list:
        r = _finite(m.return_1d)
        if not (m.price_candidate and r is not None):
            breadth_unavailable.append(m)
        elif r > 0:
            advance.append(m)
        elif r < 0:
            decline.append(m)
        else:
            unchanged.append(m)

    # ---- per-member unified evidence + raw components --------------------------
    # RAW vs DISPLAY (A1-1): full-precision component values are kept in separate
    # raw maps for reconciliation + ranking; ``_evidence`` emits only the
    # quantized display row.  Never sum/rank from a rounded value.
    raw_contribution: dict[str, float] = {}
    raw_tilt: dict[str, float] = {}
    evidence_by: dict[str, dict[str, Any]] = {}
    for m in member_list:
        r = _finite(m.return_1d)
        a = _finite(m.amount)
        in_aw = m.member_id in aw_weight_of
        in_price = m.member_id in price_set
        aw_w = aw_weight_of.get(m.member_id)
        ew_w = ew_weight if in_price else None
        # direction contribution: only inside U_aw (mirrors canonical AW).
        dir_contrib = (aw_w * r) if (in_aw and r is not None and aw_w is not None) else None
        # tilt contribution: over ALL members; missing weight == exact 0, so the
        # total is exactly AW - EW (no universe-overlap fragility).
        if r is not None and (in_aw or in_price):
            tilt = ((aw_w if aw_w is not None else 0.0) - (ew_w if ew_w is not None else 0.0)) * r
        else:
            tilt = None
        if dir_contrib is not None:
            raw_contribution[m.member_id] = dir_contrib
        if tilt is not None:
            raw_tilt[m.member_id] = tilt
        evidence_by[m.member_id] = _evidence(
            m,
            member_name_by_id=names,
            amount_share=amount_share_by.get(m.member_id),
            aw_weight=aw_w,
            ew_weight=ew_w,
            canonical_contribution=canon_contrib_by.get(m.member_id),
            direction_contribution=dir_contrib,
            tilt_contribution=tilt,
            in_price_universe=in_price,
            in_aw_universe=in_aw,
        )

    # ---- Direction group -------------------------------------------------------
    direction_rankable = sorted(raw_contribution)
    direction_positive = [
        evidence_by[mid]
        for mid in sorted(
            (mid for mid, c in raw_contribution.items() if c > 0),
            key=lambda mid: (-raw_contribution[mid], mid),
        )
    ]
    direction_negative = [
        evidence_by[mid]
        for mid in sorted(
            (mid for mid, c in raw_contribution.items() if c < 0),
            key=lambda mid: (raw_contribution[mid], mid),
        )
    ]
    # RAW sum (A1-1): full precision + member_id-sorted input to math.fsum.
    direction_sum = (
        math.fsum(raw_contribution[mid] for mid in direction_rankable)
        if direction_rankable
        else None
    )

    # ---- Capital Tilt group ----------------------------------------------------
    # Capital-tilt is a scope fact only when BOTH canonical AW and EW are
    # available (canonical semantics).  Otherwise the tilt group is unavailable
    # and its reconciliation is both_unavailable (never a mismatch).
    tilt_available = canonical_aw is not None and canonical_ew is not None
    tilt_rankable = sorted(raw_tilt)
    tilt_positive = [
        evidence_by[mid]
        for mid in sorted(
            (mid for mid, t in raw_tilt.items() if t > 0),
            key=lambda mid: (-raw_tilt[mid], mid),
        )
    ]
    tilt_negative = [
        evidence_by[mid]
        for mid in sorted(
            (mid for mid, t in raw_tilt.items() if t < 0),
            key=lambda mid: (raw_tilt[mid], mid),
        )
    ]
    tilt_sum = (
        math.fsum(raw_tilt[mid] for mid in tilt_rankable)
        if (tilt_rankable and tilt_available)
        else None
    )

    # ---- Concentration group ----------------------------------------------------
    price_hhi_members: list[dict[str, Any]] = []
    raw_price_hhi: dict[str, float] = {}
    # Deterministic abs-return total: iterate member_id-sorted sequence so the
    # shares and their squares are supplier-order independent.
    price_abs: list[tuple[str, float]] = []
    for m in member_list:
        r = _finite(m.return_1d)
        if m.price_candidate and r is not None:
            price_abs.append((m.member_id, abs(r)))
    abs_total = math.fsum(v for _, v in sorted(price_abs, key=lambda x: x[0]))
    if abs_total > _EPSILON:
        for mid, r_abs in sorted(price_abs, key=lambda x: x[0]):
            share = r_abs / abs_total
            raw_price_hhi[mid] = share * share
            row = dict(evidence_by[mid])
            row["concentration_weight"] = _quant(share)
            row["hhi_contribution"] = _quant(share * share)
            price_hhi_members.append(row)
    # Rank by RAW hhi (never the rounded display value); sum = raw fsum.
    price_hhi_members.sort(key=lambda e: (-raw_price_hhi[e["member_id"]], e["member_id"]))
    sum_price_hhi = (
        math.fsum(raw_price_hhi[mid] for mid in sorted(raw_price_hhi))
        if raw_price_hhi
        else None
    )

    amount_hhi_members: list[dict[str, Any]] = []
    raw_amount_hhi: dict[str, float] = {}
    for mid in sorted(amount_share_by):
        sh = amount_share_by[mid]
        if sh is not None:
            raw_amount_hhi[mid] = sh * sh
            row = dict(evidence_by[mid])
            row["concentration_weight"] = _quant(sh)
            row["hhi_contribution"] = _quant(sh * sh)
            amount_hhi_members.append(row)
    amount_hhi_members.sort(key=lambda e: (-raw_amount_hhi[e["member_id"]], e["member_id"]))
    sum_amount_hhi = (
        math.fsum(raw_amount_hhi[mid] for mid in sorted(raw_amount_hhi))
        if raw_amount_hhi
        else None
    )

    # ---- Leadership group (consume canonical migration verbatim) ----------------
    # A1-4 availability split:
    #   - unavailable_snapshot (one side's leader ids unknown) -> no transition
    #     evidence; transition reconciliation is skipped (cannot be verified).
    #   - empty_leader_set (both snapshots ready, either set legitimately empty)
    #     -> migration metric unavailable BUT retained/entrant/exit set facts stay
    #     valid and their reconciliation still RUNS.
    leadership: dict[str, Any]
    prev_ids: tuple[str, ...] | None = None
    curr_ids: tuple[str, ...] | None = None
    retained_ids: list[str] = []
    entrant_ids: list[str] = []
    exit_ids: list[str] = []
    leadership_transition_available = False
    if leadership_migration is None:
        leadership = {
            "status": "unavailable",
            "reason": "not_provided",
            "retained": [],
            "entrants": [],
            "exits": [],
        }
    else:
        mig = leadership_migration
        prev_ids = mig.previous_leader_ids
        curr_ids = mig.current_leader_ids
        # A1-3: entrant/exit consumed DIRECTLY from canonical migration ids;
        # only retained is derived (previous ∩ current) because canonical exposes
        # no retained_ids, and it is validated against migration.retained_count.
        if prev_ids is None or curr_ids is None:
            # unavailable_snapshot: one side unknown -> no transition evidence.
            retained_ids = []
            entrant_ids = list(mig.entrant_ids) if mig.entrant_ids is not None else []
            exit_ids = list(mig.exit_ids) if mig.exit_ids is not None else []
        else:
            prev_set, curr_set = set(prev_ids), set(curr_ids)
            retained_ids = sorted(prev_set & curr_set)
            entrant_ids = list(mig.entrant_ids) if mig.entrant_ids is not None else []
            exit_ids = list(mig.exit_ids) if mig.exit_ids is not None else []
            leadership_transition_available = True

        dir_sign = mig.current_direction

        def _aligned_raw(mid: str) -> float:
            """Raw aligned contribution for ranking (never the rounded value)."""
            canon = canon_contrib_by.get(mid)
            if canon is not None and dir_sign is not None:
                return canon * dir_sign
            if canon is not None:
                return canon
            return 0.0

        def _leader_rows(ids: Sequence[str]) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for mid in ids:
                base = dict(evidence_by.get(mid) or {})
                base.setdefault("member_id", mid)
                base.setdefault("member_name", names.get(mid, mid))
                base.setdefault("return_1d", None)
                base.setdefault("amount", None)
                base.setdefault("amount_share", _quant(amount_share_by.get(mid)))
                base.setdefault("aw_weight", None)
                base.setdefault("ew_weight", None)
                base.setdefault("contribution", None)
                base.setdefault("canonical_contribution", _quant(canon_contrib_by.get(mid)))
                base.setdefault("tilt_contribution", None)
                base.setdefault("in_price_universe", False)
                base.setdefault("in_aw_universe", False)
                canon = canon_contrib_by.get(mid)
                base["aligned_contribution"] = _quant(
                    (canon * dir_sign)
                    if (canon is not None and dir_sign is not None)
                    else (canon if canon is not None else None)
                )
                rows.append(base)
            # rank by RAW aligned contribution DESC; member_id ASC tie-break.
            rows.sort(key=lambda e: (-_aligned_raw(e["member_id"]), e["member_id"]))
            return rows

        leadership = {
            "status": mig.status,
            "reason": mig.reason,
            "previous_direction": mig.previous_direction,
            "current_direction": mig.current_direction,
            "retained": _leader_rows(retained_ids),
            "entrants": _leader_rows(entrant_ids),
            "exits": _leader_rows(exit_ids),
        }

    # ---- Reconciliation (M3) ---------------------------------------------------
    checks: dict[str, Any] = {}

    def _recon(key: str, sum_member: Any, canonical: Any, extra: Any = None) -> None:
        chk = {"kind": "sum", "sum_member": _quant(sum_member), "canonical": _quant(canonical)}
        if extra is not None:
            chk["extra"] = extra
        dsum = _finite(sum_member)
        dcan = _finite(canonical)
        if dsum is None and dcan is None:
            chk["pass"], chk["resolved"] = True, "both_unavailable"
        elif dsum is None or dcan is None:
            chk["pass"], chk["resolved"] = False, "mismatch"
        else:
            # PASS/FAIL is decided on the RAW difference (full precision); the
            # displayed abs_diff is quantized like every other display field.
            # This keeps the output order-stable even when the canonical
            # amount_share owner sums in input order (residue ~1e-16, far below
            # the 6-decimal display resolution).
            chk["abs_diff"] = _quant(abs(dsum - dcan))
            chk["pass"] = abs(dsum - dcan) <= tolerance
            chk["resolved"] = "matched" if chk["pass"] else "mismatch"
        checks[key] = chk

    canonical_tilt = (canonical_aw - canonical_ew) if (
        canonical_aw is not None and canonical_ew is not None
    ) else None
    _recon("direction", direction_sum, canonical_aw, extra={"rankable_count": len(direction_rankable)})
    _recon("capital_tilt", tilt_sum, canonical_tilt,
           extra={"aw_universe_count": len(aw_valid), "price_universe_count": n_price})
    _recon("concentration_price", sum_price_hhi, canonical_price_hhi,
           extra={"normalized_hhi": canonical_price_hhi_norm})
    _recon("concentration_amount", sum_amount_hhi, canonical_amt_hhi,
           extra={"normalized_hhi": canonical_amt_hhi_norm})

    adv_c = canonical_breadth.get("advance_count")
    dec_c = canonical_breadth.get("decline_count")
    unc_c = canonical_breadth.get("unchanged_count")
    counts_match = (
        len(advance) == adv_c and len(decline) == dec_c and len(unchanged) == unc_c
    )
    ratio_ok = (
        _quant(len(advance) / n_price if n_price > 0 else None)
        == _quant(canonical_breadth.get("advance_ratio"))
        and _quant(len(decline) / n_price if n_price > 0 else None)
        == _quant(canonical_breadth.get("decline_ratio"))
        and _quant(len(unchanged) / n_price if n_price > 0 else None)
        == _quant(canonical_breadth.get("unchanged_ratio"))
    )
    checks["breadth"] = {
        "kind": "counts_and_ratios",
        "sum_member": {"advance": len(advance), "decline": len(decline), "unchanged": len(unchanged)},
        "canonical": {"advance": adv_c, "decline": dec_c, "unchanged": unc_c},
        "ratios_match": ratio_ok,
        "pass": counts_match and ratio_ok,
        "resolved": "matched" if (counts_match and ratio_ok) else "mismatch",
    }

    if leadership_migration is None:
        checks["leadership"] = {
            "kind": "set", "resolved": "skipped", "note": "leadership not provided", "pass": None,
        }
    elif not leadership_transition_available:
        # unavailable_snapshot (or partial ids): transition cannot be verified.
        checks["leadership"] = {
            "kind": "set", "resolved": "skipped",
            "note": "leadership snapshot unavailable; transition cannot be verified",
            "pass": None,
        }
    else:
        # A1-3: entrant/exit ids ARE the canonical migration ids (no self-derived
        # algebra to re-prove); only retained is derived and is validated against
        # the canonical retained_count.  Set algebra is checked against the
        # CANONICAL leader sets, not against self-recomputed sets.
        can_entrants = set(leadership_migration.entrant_ids or ())
        can_exits = set(leadership_migration.exit_ids or ())
        prev_set = set(prev_ids or ())
        curr_set = set(curr_ids or ())
        retained = set(retained_ids)
        entrants = set(entrant_ids)
        exits = set(exit_ids)
        entrant_ids_ok = entrants == can_entrants
        exit_ids_ok = exits == can_exits
        retained_count_ok = len(retained) == leadership_migration.retained_count
        entrant_count_ok = len(entrants) == leadership_migration.entrant_count
        exit_count_ok = len(exits) == leadership_migration.exit_count
        union_current_ok = (retained | entrants) == curr_set
        union_prev_ok = (retained | exits) == prev_set
        pairwise_disjoint = (
            retained.isdisjoint(entrants)
            and retained.isdisjoint(exits)
            and entrants.isdisjoint(exits)
        )
        passed = (
            entrant_ids_ok and exit_ids_ok and retained_count_ok
            and entrant_count_ok and exit_count_ok
            and union_current_ok and union_prev_ok and pairwise_disjoint
        )
        checks["leadership"] = {
            "kind": "set",
            "entrant_ids_match_canonical": entrant_ids_ok,
            "exit_ids_match_canonical": exit_ids_ok,
            "retained_count_matches_canonical": retained_count_ok,
            "entrant_count_matches_canonical": entrant_count_ok,
            "exit_count_matches_canonical": exit_count_ok,
            "retained_union_entrant_eq_current": union_current_ok,
            "retained_union_exit_eq_previous": union_prev_ok,
            "pairwise_disjoint": pairwise_disjoint,
            "pass": passed,
            "resolved": "matched" if passed else "mismatch",
        }

    violation_count = sum(1 for chk in checks.values() if chk.get("pass") is False)
    skipped = sorted(k for k, chk in checks.items() if chk.get("resolved") == "skipped")

    scope_meta = {
        "scope_type": _deep_get(observation, "scope", "scope_type"),
        "scope_key": _deep_get(observation, "scope", "scope_key"),
        "trade_date": _deep_get(observation, "scope", "trade_date"),
        "member_count": len(member_list),
        "price_universe_count": n_price,
        "aw_universe_count": len(aw_valid),
    }

    # Determinism: checksum covers the whole output (self-excluding the checksum key).
    result: dict[str, Any] = {
        "scope": scope_meta,
        "direction": {
            "status": "ready" if canonical_aw is not None else "unavailable",
            "aw_universe_count": len(aw_valid),
            "positive": direction_positive,
            "negative": direction_negative,
            "sum_contribution": _quant(direction_sum),
            "canonical_aw_return": _quant(canonical_aw),
        },
        "capital_tilt": {
            "status": "ready" if canonical_tilt is not None else "unavailable",
            "price_universe_count": n_price,
            "aw_universe_count": len(aw_valid),
            "positive": tilt_positive,
            "negative": tilt_negative,
            "sum_tilt_contribution": _quant(tilt_sum),
            "canonical_aw_return": _quant(canonical_aw),
            "canonical_ew_return": _quant(canonical_ew),
        },
        "breadth": {
            "status": "ready",
            "denominator": n_price,
            "advance": _breadth_sort(advance, names, "desc"),
            "decline": _breadth_sort(decline, names, "asc"),
            "unchanged": _breadth_sort(unchanged, names, "member"),
            "unavailable": [
                dict(evidence_by[m.member_id])
                for m in sorted(breadth_unavailable, key=lambda m: m.member_id)
                if m.member_id in evidence_by
            ],
        },
        "concentration": {
            "price": {
                "members": price_hhi_members,
                "sum_hhi": _quant(sum_price_hhi),
                "canonical_raw_hhi": _quant(canonical_price_hhi),
                "canonical_normalized_hhi": _quant(canonical_price_hhi_norm),
            },
            "amount": {
                "members": amount_hhi_members,
                "sum_hhi": _quant(sum_amount_hhi),
                "canonical_raw_hhi": _quant(canonical_amt_hhi),
                "canonical_normalized_hhi": _quant(canonical_amt_hhi_norm),
            },
        },
        "leadership": leadership,
        "reconciliation": {
            "violation_count": violation_count,
            "skipped": skipped,
            "tolerance": tolerance,
            "checks": checks,
        },
    }
    result["determinism_checksum"] = checksum(result)
    return result


def _breadth_sort(
    group: Sequence[MemberObservation], names: Mapping[str, str], mode: str
) -> list[dict[str, Any]]:
    if mode == "desc":
        ordered = sorted(
            group, key=lambda m: (-(m.return_1d if m.return_1d is not None else 0.0), m.member_id)
        )
    elif mode == "asc":
        ordered = sorted(
            group, key=lambda m: ((m.return_1d if m.return_1d is not None else 0.0), m.member_id)
        )
    else:
        ordered = sorted(group, key=lambda m: m.member_id)
    return [
        {
            "member_id": m.member_id,
            "member_name": names.get(m.member_id, m.member_id),
            "return_1d": _quant(_finite(m.return_1d)),
        }
        for m in ordered
    ]


__all__ = [
    "RECONCILIATION_TOLERANCE",
    "checksum",
    "compute_member_attribution",
]
