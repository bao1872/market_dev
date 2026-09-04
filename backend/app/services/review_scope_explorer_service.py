"""[SLICE 5 / Explorer] Scope Explorer compare-first read model.

Narrow batch projection over the canonical owners. Hard constraints:

- **NO N+1**: one Fact LEFT OUTER JOIN Composition query projecting only the
  JSONB sub-objects the Explorer needs; cross-sectional peer percentiles are
  computed in-process from the same result set (canonical math owner), never by
  calling the single-scope ``get_cross_sectional`` per row.
- **No canonical recompute**: every displayed value is read from an existing
  owner (Observation scalars / persisted Composition / C1 cross-sectional).
  No score, no rank, no AW-EW, no Jaccard->migration inference.
- **Lineage**: the caller passes the *formally published* ``review_run_id``
  (resolved by the publication owner); this module never re-derives publication.
- Null / unavailable are preserved (never 0, never forward-filled).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Float, Select, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.analysis.cross_sectional import compute_cross_sectional
from app.models.market_review import (
    ReviewScopeCompositionSnapshot,
    ReviewScopeObservationFact,
)

# Formal composite scope identity. A scope is uniquely identified by
# (scope_type, scope_key); two families may legitimately share a scope_key, so
# every read-model map MUST be keyed by this tuple, never by scope_key alone.
ScopeIdentity = tuple[str, str]

# ---------------------------------------------------------------------------
# SMC display priority (§十) — pure list DISPLAY projection, NOT a score.
# ---------------------------------------------------------------------------

_SMC_ALLOWED_EVENT_TYPES = ("BOS", "CHoCH")
_SMC_ALLOWED_LEVELS = ("Swing", "Internal")

# fixed display priority: 1 Swing CHoCH, 2 Swing BOS, 3 Internal CHoCH, 4 Internal BOS
_SMC_LEVEL_RANK = {"Swing": 0, "Internal": 1}
_SMC_TYPE_RANK = {"CHoCH": 0, "BOS": 1}


def _smc_rank(event_type: str | None, structure_level: str | None) -> int | None:
    """Fixed display priority index; ``None`` when the event is not displayable."""
    if event_type not in _SMC_ALLOWED_EVENT_TYPES:
        return None
    if structure_level not in _SMC_ALLOWED_LEVELS:
        return None
    return _SMC_LEVEL_RANK[structure_level] * 2 + _SMC_TYPE_RANK[event_type]


def select_smc_display_event(events: Any) -> dict[str, Any]:
    """Pick the single most display-worthy BOS/CHoCH event (pure).

    Priority is a FIXED display order (Swing CHoCH > Swing BOS > Internal CHoCH >
    Internal BOS); ties break on ``member_ratio`` DESC then deterministic event
    key. This never mutates canonical event data and is not a structural score.

    Returns ``{"eventType","structureLevel","direction","memberRatio",
    "availability","reason"}``.
    """
    out_unavailable = {
        "eventType": None,
        "structureLevel": None,
        "direction": None,
        "memberRatio": None,
        "availability": "unavailable",
        "reason": "EVENTS_UNAVAILABLE",
    }
    if not isinstance(events, dict):
        return out_unavailable
    status = events.get("status")
    if status != "ready":
        out_unavailable["reason"] = (
            events.get("reason") if isinstance(events.get("reason"), str) else "EVENTS_UNAVAILABLE"
        )
        return out_unavailable

    cells = events.get("cells")
    if not isinstance(cells, dict):
        cells = {}

    candidates: list[tuple[int, float, str, dict[str, Any]]] = []
    for _bucket in ("leveled", "extreme"):
        bucket = cells.get(_bucket)
        if not isinstance(bucket, dict):
            continue
        for event_key, cell in bucket.items():
            if not isinstance(cell, dict):
                continue
            etype = cell.get("event_type")
            level = cell.get("structure_level")
            rank = _smc_rank(etype if isinstance(etype, str) else None, level if isinstance(level, str) else None)
            if rank is None:
                continue
            ratio = cell.get("member_ratio")
            ratio_num = float(ratio) if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) else -1.0
            candidates.append((rank, ratio_num, str(event_key), cell))

    if not candidates:
        # ready + denominator>0 + 无 BOS/CHoCH -> "无"，不是 unavailable
        return {
            "eventType": None,
            "structureLevel": None,
            "direction": None,
            "memberRatio": None,
            "availability": "ready",
            "reason": None,
        }

    # priority ASC, member_ratio DESC, deterministic tie-break on event key
    candidates.sort(key=lambda t: (t[0], -t[1], t[2]))
    _rank, ratio_num, _key, cell = candidates[0]
    direction = cell.get("direction")
    return {
        "eventType": cell.get("event_type") if isinstance(cell.get("event_type"), str) else None,
        "structureLevel": cell.get("structure_level") if isinstance(cell.get("structure_level"), str) else None,
        "direction": direction if isinstance(direction, str) else None,
        "memberRatio": ratio_num if ratio_num >= 0 else None,
        "availability": "ready",
        "reason": None,
    }


# ---------------------------------------------------------------------------
# Compare facts (pure) — canonical owners only
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def build_compare_facts(
    observation_payload: dict[str, Any] | None,
    composition_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the narrow Explorer compare-facts block (pure, no I/O).

    Every field is read VERBATIM from an existing canonical owner:
      - DSA: ``trend.continuous.{regime_strength,dsa_dir_bars,dsa_vwap_dev_pct}``
      - SMC: ``structure.events`` via the display-priority projection
      - Momentum: ``momentum.change`` (ratios from producer; denominator verbatim)
      - Volume: ``participation.volume.ratio20`` central value (p50)
      - Price: ``price.equal_weight_return`` / ``price.breadth.advance_ratio``
      - Composition: persisted ``capital_tilt.capital_tilt`` / ``leadership.migration``
    """
    obs = observation_payload if isinstance(observation_payload, dict) else {}
    comp = composition_payload if isinstance(composition_payload, dict) else {}

    trend = obs.get("trend")
    continuous = trend.get("continuous") if isinstance(trend, dict) else None
    if not isinstance(continuous, dict):
        continuous = {}

    structure = obs.get("structure")
    events = structure.get("events") if isinstance(structure, dict) else None
    # CURRENT SMC owner (REVIEW-CURRENT-SMC-OWNER): prefer the exact-T Core(T)
    # ``current`` block over the History immutable evidence (``events``).  For
    # historical dates ``current`` is absent -> fall back to ``events``.  There is
    # NO History fallback when ``current`` is present but unavailable (no frontend
    # synthesis, no History(T) re-gate for Current).
    current = structure.get("current") if isinstance(structure, dict) else None
    smc_source = current if isinstance(current, dict) else events

    momentum = obs.get("momentum")
    change = momentum.get("change") if isinstance(momentum, dict) else None
    if not isinstance(change, dict):
        change = {}

    participation = obs.get("participation")
    volume = participation.get("volume") if isinstance(participation, dict) else None
    ratio20 = volume.get("ratio20") if isinstance(volume, dict) else None
    if not isinstance(ratio20, dict):
        ratio20 = {}

    price = obs.get("price") if isinstance(obs.get("price"), dict) else {}
    breadth = price.get("breadth") if isinstance(price.get("breadth"), dict) else {}

    internal = comp.get("internal_structure_facts")
    capital_tilt_node = internal.get("capital_tilt") if isinstance(internal, dict) else None
    if not isinstance(capital_tilt_node, dict):
        capital_tilt_node = {}

    leadership = comp.get("leadership") if isinstance(comp.get("leadership"), dict) else {}

    return {
        "dsa": {
            "regimeStrength": _num(continuous.get("regime_strength")),
            "regimeStrengthPeerPercentile": None,  # filled by the percentile pass
            "durationBars": _num(continuous.get("dsa_dir_bars")),
            "vwapDevPct": _num(continuous.get("dsa_vwap_dev_pct")),
        },
        "smc": select_smc_display_event(smc_source),
        "momentum": {
            # Board parity: denominator is producer-owned; frontend must not redefine it.
            "enhancingRatio": _num(change.get("enhancing_ratio")),
            "weakeningRatio": _num(change.get("weakening_ratio")),
            "denominator": _num(change.get("denominator")),
        },
        "volume": {"ratio20": _num(ratio20.get("p50"))},
        "price": {
            "equalWeightReturn": _num(price.get("equal_weight_return")),
            "equalWeightReturnPeerPercentile": None,  # filled by the percentile pass
            "advanceRatio": _num(breadth.get("advance_ratio")),
        },
        "composition": {
            "capitalTilt": _num(capital_tilt_node.get("capital_tilt")),
            "migration": _num(leadership.get("migration")),
        },
    }


# ---------------------------------------------------------------------------
# Peer percentile — canonical cross-sectional math owner, batched
# ---------------------------------------------------------------------------

# canonical C1 field keys（OBSERVATION_PRIMITIVES registry key，非 l1_path）
_PEER_FIELDS = {
    "trend.continuous.regime_strength": "regimeStrengthPeerPercentile",
    "equal_weight_return": "equalWeightReturnPeerPercentile",
}


def _cohort_stub(trend_continuous: Any, price: Any) -> dict[str, Any]:
    """Minimal L1-shaped stub so the canonical C1 extractor can read the 2 fields."""
    return {
        "trend": {"continuous": trend_continuous if isinstance(trend_continuous, dict) else {}},
        "price": price if isinstance(price, dict) else {},
    }


def build_peer_percentiles(
    rows: list[tuple[str, Any, Any]],
) -> dict[str, dict[str, float | None]]:
    """Per-scope peer percentile for the Explorer's two C1 fields (pure).

    Uses the canonical ``compute_cross_sectional`` owner (no duplicated percentile
    formula). ``rows`` = ``[(scope_key, trend_continuous, price), ...]`` for
    **one family's** cohort.  Callers with mixed families MUST use
    ``build_peer_percentiles_by_family`` — this function treats every row as
    belonging to the same cohort.
    """
    peer_payloads: dict[str, dict[str, Any]] = {
        key: _cohort_stub(tc, pr) for key, tc, pr in rows
    }
    return _percentiles_for_cohort(peer_payloads)


def _percentiles_for_cohort(
    peer_payloads: dict[str, dict[str, Any]],
) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for scope_key in peer_payloads:
        entry: dict[str, float | None] = dict.fromkeys(_PEER_FIELDS.values())
        result = compute_cross_sectional(
            current_payload=peer_payloads[scope_key],
            peer_payloads=peer_payloads,
            current_scope_key=scope_key,
        )
        for f in result.get("fields", []):
            target = _PEER_FIELDS.get(f.get("field"))
            if target is None:
                continue
            # only a ready field exposes a real percentile
            if f.get("status") == "ready":
                entry[target] = _num(f.get("percentile"))
        out[scope_key] = entry
    return out


# ---------------------------------------------------------------------------
# Narrow batch read (ONE query)
# ---------------------------------------------------------------------------


def build_peer_percentiles_by_family(
    rows: list[tuple[str, str, Any, Any]],
) -> dict[ScopeIdentity, dict[str, float | None]]:
    """Family-scoped peer percentiles — the ONLY safe entry for mixed families.

    ``rows`` = ``[(scope_key, scope_type, trend_continuous, price), ...]``.

    [SLICE 5 finalization] ``/scopes`` has an OPTIONAL ``scope_type``: when it is
    omitted the query returns every family at once, so the cohort MUST be grouped
    by family before any percentile is computed — otherwise an industry_l1 scope
    would be ranked against concept scopes and the DTO's "同 family percentile"
    contract would be violated.

    Each family group is computed independently via the canonical owner; results
    are merged back under the formal composite identity ``(scope_type, scope_key)``
    — never by scope_key alone, because two families may legitimately share a
    scope_key (collision). Never writes family info into any score/rank.
    """
    groups: dict[str, list[tuple[str, Any, Any]]] = {}
    for scope_key, scope_type, tc, pr in rows:
        groups.setdefault(scope_type, []).append((scope_key, tc, pr))

    merged: dict[ScopeIdentity, dict[str, float | None]] = {}
    for family, family_rows in groups.items():
        cohort = {key: _cohort_stub(tc, pr) for key, tc, pr in family_rows}
        scope_key_map = _percentiles_for_cohort(cohort)
        for scope_key, entry in scope_key_map.items():
            merged[(family, scope_key)] = entry
    return merged


def _compare_stmt(
    review_run_id: UUID,
    trade_date: date,
    scope_type: str | None,
) -> Select:
    fact = ReviewScopeObservationFact
    comp = ReviewScopeCompositionSnapshot
    fact_payload = fact.observation_payload
    comp_payload = comp.composition_payload
    join_cond = (
        (comp.review_run_id == fact.review_run_id)
        & (comp.trade_date == fact.trade_date)
        & (comp.scope_type == fact.scope_type)
        & (comp.scope_key == fact.scope_key)
    )
    filters: list[Any] = [
        fact.review_run_id == review_run_id,
        fact.trade_date == trade_date,
    ]
    if scope_type is not None:
        filters.append(fact.scope_type == scope_type)
    return (
        select(
            fact.scope_type.label("scope_type"),
            fact.scope_key.label("scope_key"),
            fact_payload["trend"]["continuous"].label("trend_continuous"),
            fact_payload["structure"]["events"].label("structure_events"),
            fact_payload["momentum"]["change"].label("momentum_change"),
            fact_payload["participation"]["volume"]["ratio20"].label("vol_ratio20"),
            fact_payload["price"].label("price"),
            cast(
                comp_payload["internal_structure_facts"]["capital_tilt"]["capital_tilt"].astext,
                Float,
            ).label("capital_tilt"),
            cast(comp_payload["leadership"]["migration"].astext, Float).label("migration"),
        )
        .select_from(fact)
        .join(comp, join_cond, isouter=True)
        .where(*filters)
    )


async def list_review_scope_compare(
    db: AsyncSession,
    *,
    review_run_id: UUID,
    trade_date: date,
    scope_type: str | None,
    scope_keys: set[ScopeIdentity],
) -> dict[ScopeIdentity, dict[str, Any]]:
    """Explorer compare facts for the requested page keys — **ONE query**.

    The cohort for peer percentiles is the whole result set of this same query
    (already scoped to the published run + family), so no second round-trip and
    no per-scope cross-sectional call. Only the requested ``scope_keys`` (formal
    composite identity ``(scope_type, scope_key)``) get a full compare-facts
    block built.
    """
    rows = list((await db.execute(_compare_stmt(review_run_id, trade_date, scope_type))).all())
    if not rows:
        return {}

    # 按 family 分组算 percentile（scope_type 可省略 → 结果集可能含多个 family）
    cohort: list[tuple[str, str, Any, Any]] = [
        (r.scope_key, r.scope_type, r.trend_continuous, r.price) for r in rows
    ]
    percentile_map = build_peer_percentiles_by_family(cohort)

    out: dict[ScopeIdentity, dict[str, Any]] = {}
    for r in rows:
        identity = (r.scope_type, r.scope_key)
        if identity not in scope_keys:
            continue
        obs_stub = {
            "trend": {"continuous": r.trend_continuous if isinstance(r.trend_continuous, dict) else {}},
            "structure": {"events": r.structure_events if isinstance(r.structure_events, dict) else None},
            "momentum": {"change": r.momentum_change if isinstance(r.momentum_change, dict) else {}},
            "participation": {"volume": {"ratio20": r.vol_ratio20 if isinstance(r.vol_ratio20, dict) else {}}},
            "price": r.price if isinstance(r.price, dict) else {},
        }
        comp_stub: dict[str, Any] = {}
        if r.capital_tilt is not None:
            comp_stub["internal_structure_facts"] = {"capital_tilt": {"capital_tilt": r.capital_tilt}}
        if r.migration is not None:
            comp_stub["leadership"] = {"migration": r.migration}

        facts = build_compare_facts(obs_stub, comp_stub if comp_stub else None)
        pct = percentile_map.get(identity) or {}
        facts["dsa"]["regimeStrengthPeerPercentile"] = pct.get("regimeStrengthPeerPercentile")
        facts["price"]["equalWeightReturnPeerPercentile"] = pct.get("equalWeightReturnPeerPercentile")
        out[identity] = facts
    return out
