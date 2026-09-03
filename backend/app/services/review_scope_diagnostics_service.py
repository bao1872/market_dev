"""Review Scope Diagnostics read-model (R3 History / Cross-sectional completion).

Narrow owner (task spec §4): published daily facts + published ReviewRun ->
align trading dates -> extract canonical fields -> compute approved 20D rolling
diagnostics -> build UI DTO.

Hard contract (task spec / PRD §7.6 / §7.7.5 / §7.8):
- History is composed at QUERY TIME from persisted ``ReviewScopeObservationFact``
  rows, each resolved through its *published* ``ReviewRun`` (review_run_id).
  Never a global ``WHERE trade_date=?`` scan; never ``latest row`` as canonical.
- ``baseline(T)`` excludes T (lagged). ``null != 0`` (missing excluded from baseline).
- No rolling-20D result persisted to DB.
- Cross-sectional uses the SAME published-run lineage (see
  ``review_cross_sectional_service``), not a same-day global list.

This module owns ONLY read + extract + rolling math. It does NOT recompute any
canonical L1/L2 fact, percentile peer semantics, or velocity/acceleration.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.analysis.observation_stats import (
    empirical_percentile,
    safe_mean,
    safe_std,
    safe_variance,
    zscore,
)
from app.models.market_review import ReviewScopeObservationFact
from app.services.review_observation_persistence_service import (
    ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES,
    list_scope_composition_snapshots_for_dates,
    list_scope_observation_facts,
)
from app.services.review_publication_service import (
    get_published_review_run_id,
    list_formally_published_review_dates,
)

HISTORY_DISPLAY_WINDOW = 20
HISTORY_WARMUP_TOTAL = 40
ROLLING_WINDOW = 20
# calendar slack so a ~60-trading-day warmup is always covered
_HISTORY_CALENDAR_SLACK = 30


# ---------------------------------------------------------------------------
# extraction helpers (pure)
# ---------------------------------------------------------------------------


def _deep_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _scalar_direct(node: Any) -> float | None:
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        v = float(node)
        return v if math.isfinite(v) else None
    return None


def _scalar_central(node: Any) -> float | None:
    """Central tendency of an L1 distribution dict (p50 first, fall back median)."""
    if not isinstance(node, dict):
        return None
    v = _scalar_direct(node.get("p50"))
    if v is None:
        v = _scalar_direct(node.get("median"))
    return v


# (key, payload path, extractor, label, unit)
_HISTORY_FIELD_SPECS: tuple[tuple[str, tuple[str, ...], Any, str, str | None], ...] = (
    ("regime_strength", ("trend", "continuous", "regime_strength"), _scalar_direct,
     "趋势 Regime Strength", None),
    ("dsa_vwap_dev_pct", ("trend", "continuous", "dsa_vwap_dev_pct"), _scalar_direct,
     "DSA VWAP 偏离 %", "pct"),
    ("equal_weight_return", ("price", "equal_weight_return"), _scalar_direct,
     "等权收益", "pct"),
    ("amount_weighted_return", ("price", "amount_weighted_return"), _scalar_direct,
     "金额加权收益", "pct"),
    ("advance_ratio", ("price", "breadth", "advance_ratio"), _scalar_direct,
     "上涨比率", "ratio"),
    ("decline_ratio", ("price", "breadth", "decline_ratio"), _scalar_direct,
     "下跌比率", "ratio"),
    ("unchanged_ratio", ("price", "breadth", "unchanged_ratio"), _scalar_direct,
     "走平比率", "ratio"),
    ("return_dispersion", ("price", "return_dispersion"), _scalar_direct,
     "收益离散度", None),
    ("price_hhi", ("price", "concentration", "normalized_hhi"), _scalar_direct,
     "价格集中度 HHI", None),
    ("bb_position", ("momentum", "bb_position"), _scalar_central, "BB 位置", "pct"),
    ("bb_width", ("momentum", "bb_width"), _scalar_central, "BB 宽度", None),
    ("volume_ratio20", ("participation", "volume", "ratio20"), _scalar_central,
     "量比 20D", None),
    ("volume_ratio200", ("participation", "volume", "ratio200"), _scalar_central,
     "量比 200D", None),
    ("volume_zscore20", ("participation", "volume", "zscore20"), _scalar_central,
     "量能 Z 20D", None),
    ("volume_zscore200", ("participation", "volume", "zscore200"), _scalar_central,
     "量能 Z 200D", None),
    ("trend_up_ratio", ("trend", "state", "up_ratio"), _scalar_direct,
     "上涨成员比", "ratio"),
    ("trend_down_ratio", ("trend", "state", "down_ratio"), _scalar_direct,
     "下跌成员比", "ratio"),
    # Sideways 映射成 Neutral；canonical producer 产出 neutral_ratio（非 range_ratio）。
    ("trend_range_ratio", ("trend", "state", "neutral_ratio"), _scalar_direct,
     "横盘成员比", "ratio"),
)


def _select_published_facts(
    rows: list[ReviewScopeObservationFact],
    published_by_date: dict[date, Any],
) -> dict[date, dict[str, Any]]:
    """Lineage-safe selection: keep only the fact whose ``review_run_id`` equals the
    *published* run for its trade_date.

    A later same-day run (review_run_id != published) is dropped; a date with no
    published pointer yields no fact. Pure + unit-testable (no DB needed).
    """
    canonical: dict[date, dict[str, Any]] = {}
    for row in rows:
        pub = published_by_date.get(row.trade_date)
        if pub is None:
            continue
        if row.review_run_id != pub:
            continue
        payload = row.observation_payload
        if isinstance(payload, dict):
            # unique grain (review_run_id, trade_date, scope) -> single row per date
            canonical[row.trade_date] = payload
    return canonical


def build_canonical_by_date(
    formal_dates: list[date],
    run_id_by_date: dict[date, Any],
    fact_by_run: dict[Any, dict[str, Any]],
) -> dict[date, dict[str, Any] | None]:
    """Lineage-safe canonical payload per *formal published* date (P1-2 fix).

    The history date axis is the set of formally published Review trading dates,
    NOT the set of dates that happen to have a persisted Scope Fact. Each date
    keeps its slot; the value is:

    - ``None`` when no fact was persisted for the published run of that date
      (e.g. scope not ready that day). The slot is preserved — never dropped,
      never forward-filled, never compressed.
    - the fact payload when present.

    Pure + unit-testable (no DB needed). The ``run_id_by_date`` must already be
    resolved through the FORMAL REVIEW READ OWNER (``list_formally_published_
    review_dates``), so a broken pointer (pointer exists but run not formally
    published) never reaches this function — it is excluded upstream by the date
    axis itself.
    """
    canonical: dict[date, dict[str, Any] | None] = {}
    for d in formal_dates:
        run_id = run_id_by_date.get(d)
        if run_id is None:
            canonical[d] = None
            continue
        canonical[d] = fact_by_run.get(run_id)  # None when fact missing for run
    return canonical


def _select_published_compositions(
    rows: list[Any],
    published_by_date: dict[date, Any],
) -> dict[date, dict[str, Any]]:
    """[SLICE 4 / Price] Lineage-safe Composition selection (mirrors
    ``_select_published_facts``).

    Keeps only the Composition row whose ``review_run_id`` equals the *formally
    published* run of its trade_date. A later same-day run (unpublished) is
    DROPPED; a date with no published pointer yields no Composition. Pure +
    unit-testable (no DB needed) — this function IS the guarantee that an
    unpublished same-day run cannot pollute the Price history.
    """
    canonical: dict[date, dict[str, Any]] = {}
    for row in rows:
        pub = published_by_date.get(row.trade_date)
        if pub is None:
            continue
        if row.review_run_id != pub:
            continue
        payload = row.composition_payload
        if isinstance(payload, dict):
            canonical[row.trade_date] = payload
    return canonical


def _build_price_projection(
    compositions: dict[date, dict[str, Any] | None],
    dates: list[date],
) -> dict[str, Any]:
    """[SLICE 4 / Price] Narrow Composition history projection (pure).

    Every date slot is preserved; a date whose published Composition is missing
    yields ``None`` (never forward-filled, never back-derived from the current
    Composition). Values are read VERBATIM from the persisted Composition:

    - ``capital_tilt`` = ``internal_structure_facts.capital_tilt.capital_tilt``
      (persisted fact — the frontend must NOT compute AW - EW)
    - leadership: ``status`` / ``reason`` / ``jaccard_stability`` / ``migration`` /
      ``current_leader_count`` / ``current_leader_ids`` (verbatim; ``[]`` vs ``None``
      for the leader-id list must be preserved).
    """
    capital_tilt: list[float | None] = []
    leadership: list[dict[str, Any] | None] = []
    for d in dates:
        comp = compositions.get(d)
        if not isinstance(comp, dict):
            capital_tilt.append(None)
            leadership.append(None)
            continue
        tilt = _deep_get(comp, ("internal_structure_facts", "capital_tilt", "capital_tilt"))
        capital_tilt.append(tilt if isinstance(tilt, (int, float)) and not isinstance(tilt, bool) else None)
        lead = comp.get("leadership")
        if not isinstance(lead, dict):
            leadership.append(None)
            continue
        ids = lead.get("current_leader_ids")
        leadership.append(
            {
                "status": lead.get("status") if isinstance(lead.get("status"), str) else None,
                "reason": lead.get("reason") if isinstance(lead.get("reason"), str) else None,
                "jaccard_stability": _num_or_none(lead.get("jaccard_stability")),
                "migration": _num_or_none(lead.get("migration")),
                "current_leader_count": (
                    int(lead.get("current_leader_count"))
                    if isinstance(lead.get("current_leader_count"), int)
                    and not isinstance(lead.get("current_leader_count"), bool)
                    else None
                ),
                # [] (empty leader set) is a REAL fact and must not collapse to null.
                "current_leader_ids": ids if isinstance(ids, list) else None,
            }
        )
    return {
        "dates": [d.isoformat() for d in dates],
        "capital_tilt": capital_tilt,
        "leadership": leadership,
    }


def _num_or_none(v: Any) -> float | None:
    """Numeric passthrough; non-finite / non-numeric -> None (never 0)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _compute_field_rolling(
    full_series: list[float | None], window: int = ROLLING_WINDOW
) -> dict[str, list[float | None | int]]:
    """Compute lagged-baseline 20D rolling diagnostics for one aligned series.

    ``full_series`` is the value aligned to the FULL warmup window (ascending).
    ``baseline(i)`` = finite values in ``full_series[max(0, i-window) : i]``
    (strictly before i -> excludes T). Returns arrays aligned to ``full_series``.

    [SLICE 4 / Price] ``variance20`` is a first-class backend fact (population
    variance of the same lagged baseline). It shares the owner + definition with
    ``safe_std`` (``std == sqrt(variance)``); ``None`` when the baseline holds
    fewer than 2 finite values. The frontend must never derive it as ``std ** 2``.
    """
    n = len(full_series)
    mean20: list[float | None] = []
    variance20: list[float | None] = []
    std20: list[float | None] = []
    z20: list[float | None] = []
    p20: list[float | None] = []
    bcount: list[int | None] = []
    for i in range(n):
        baseline = [
            full_series[j]
            for j in range(max(0, i - window), i)
            if full_series[j] is not None
        ]
        m = safe_mean(baseline)
        var = safe_variance(baseline)
        s = safe_std(baseline)
        v = full_series[i]
        mean20.append(m)
        variance20.append(var)
        std20.append(s)
        z20.append(zscore(v, m, s))
        window_samples = baseline + ([v] if v is not None else [])
        p20.append(empirical_percentile(v, window_samples))
        bcount.append(len(baseline))
    return {
        "mean20": mean20,
        "variance20": variance20,
        "std20": std20,
        "zscore20": z20,
        "percentile20": p20,
        "baselineCount": bcount,
    }


def _build_smc_projection(
    canonical: dict[date, dict[str, Any] | None],
    dates: list[date],
) -> dict[str, Any]:
    """Narrow SMC history projection (Slice 2 SMC).

    复用同一 published-run 安全日序列（``canonical``）+ 已解析的正式日期轴 ``dates``，
    直接从每个日期的 persisted Observation 投影结构事实；不重新查询 / 不重算 canonical
    SMC。每个日期槽保留（None = 该日正式 run 无 fact，显示 gap）。
    """
    swing_state: list[Any] = []
    internal_state: list[Any] = []
    event_tape: list[Any] = []
    for d in dates:
        payload = canonical.get(d)
        if not isinstance(payload, dict):
            swing_state.append(None)
            internal_state.append(None)
            event_tape.append(None)
            continue
        structure = payload.get("structure")
        if not isinstance(structure, dict):
            swing_state.append(None)
            internal_state.append(None)
            event_tape.append(None)
            continue
        swing = structure.get("swing")
        internal = structure.get("internal")
        swing_state.append(swing.get("state") if isinstance(swing, dict) else None)
        internal_state.append(internal.get("state") if isinstance(internal, dict) else None)
        event_tape.append(structure.get("events"))
    return {
        "dates": [d.isoformat() for d in dates],
        "swing_state": swing_state,
        "internal_state": internal_state,
        "event_tape": event_tape,
    }


def _build_momentum_volume_projection(
    canonical: dict[date, dict[str, Any] | None],
    dates: list[date],
) -> dict[str, Any]:
    """Narrow Momentum+Volume history projection (Slice 3).

    复用同一 published-run 安全日序列（``canonical``）+ 已解析的正式日期轴 ``dates``，
    直接从每个日期的 persisted Observation 投影动量+量能事实；不重新查询 / 不重算
    canonical。每个日期槽保留（None = 该日正式 run 无 fact，显示 gap）。

    OPEN categorical ``momentum_volume_relation`` 原样保留（不建立固定 enum、不丢未知
    category）。Release Volume Ratio 取每日 member-first median（非 event-weighted）。
    SQZ_RELEASE 是结构事件流，绝不作为 release_volume_ratio 来源。
    """
    momentum_state: list[Any] = []
    momentum_change: list[Any] = []
    squeeze_state: list[Any] = []
    release_volume_ratio: list[Any] = []
    momentum_volume_relation: list[Any] = []
    volume_percentile20: list[Any] = []
    volume_percentile200: list[Any] = []
    sqzmom_mean: list[float | None] = []
    for d in dates:
        payload = canonical.get(d)
        if not isinstance(payload, dict):
            momentum_state.append(None)
            momentum_change.append(None)
            squeeze_state.append(None)
            release_volume_ratio.append(None)
            momentum_volume_relation.append(None)
            volume_percentile20.append(None)
            volume_percentile200.append(None)
            sqzmom_mean.append(None)
            continue
        momentum = payload.get("momentum")
        m = momentum if isinstance(momentum, dict) else None
        participation = payload.get("participation")
        vol = (
            participation.get("volume")
            if isinstance(participation, dict)
            else None
        )
        v = vol if isinstance(vol, dict) else None
        momentum_state.append(m.get("state") if m else None)
        momentum_change.append(m.get("change") if m else None)
        squeeze_state.append(m.get("squeeze_state") if m else None)
        release_volume_ratio.append(m.get("release_volume_ratio") if m else None)
        momentum_volume_relation.append(m.get("momentum_volume_relation") if m else None)
        sqzmom = m.get("sqzmom") if m else None
        sqzmom_mean.append(sqzmom.get("mean") if isinstance(sqzmom, dict) else None)
        volume_percentile20.append(v.get("percentile20") if v else None)
        volume_percentile200.append(v.get("percentile200") if v else None)
    return {
        "dates": [d.isoformat() for d in dates],
        "momentum_state": momentum_state,
        "momentum_change": momentum_change,
        "squeeze_state": squeeze_state,
        "release_volume_ratio": release_volume_ratio,
        "momentum_volume_relation": momentum_volume_relation,
        "volume_percentile20": volume_percentile20,
        "volume_percentile200": volume_percentile200,
        "sqzmom_mean": sqzmom_mean,
    }


# ---------------------------------------------------------------------------
# service owner (DB)
# ---------------------------------------------------------------------------


async def get_scope_diagnostics(
    db: AsyncSession,
    *,
    trade_date: date,
    scope_type: str,
    scope_key: str,
    display_window: int = HISTORY_DISPLAY_WINDOW,
    warmup_total: int = HISTORY_WARMUP_TOTAL,
) -> dict[str, Any]:
    """Build the Review scope history DTO for the display window.

    Returns ``availability.status == "not_activated"`` (empty fields) for scope
    types that are never persisted historically (market / major_index / style).
    """
    if scope_type not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES:
        return {
            "dates": [],
            "displayWindow": display_window,
            "availability": {
                "status": "not_activated",
                "scopeType": scope_type,
                "scopeKey": scope_key,
                "reason": "scope_type not persisted historically",
            },
            "fields": {},
            "smc": None,
            "momentumVolume": None,
            "price": None,
        }

    from_date = trade_date - timedelta(
        days=display_window + warmup_total + _HISTORY_CALENDAR_SLACK
    )
    rows = await list_scope_observation_facts(
        db,
        scope_type=scope_type,
        scope_key=scope_key,
        from_date=from_date,
        to_date=trade_date,
    )

    # [P1-2] 日期轴 = 正式 published Review 交易日（复用 FORMAL REVIEW READ OWNER），
    # 而非“已有 Scope Fact 的日期”。formal published 的判定（status==published /
    # published_at not null / pointer identity / trade_date 一致）全部在
    # list_formally_published_review_dates 的 DB JOIN 内完成；broken pointer（pointer
    # 存在但 run 未正式发布）直接被排除，不会进入历史轴。
    formal_desc = await list_formally_published_review_dates(
        db, to_date=trade_date, limit=display_window + warmup_total + 100
    )
    candidate_dates = [d for d in reversed(formal_desc) if from_date <= d <= trade_date]

    # 每个正式日期解析其 published run_id（已由 formal owner 保证）。
    run_id_by_date: dict[date, Any] = {}
    for d in candidate_dates:
        run_id_by_date[d] = await get_published_review_run_id(db, d)

    # 事实按 run_id 索引；lineage 门控：只取 published run 的 fact。
    fact_by_run: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row.observation_payload, dict):
            fact_by_run[row.review_run_id] = row.observation_payload

    canonical = build_canonical_by_date(candidate_dates, run_id_by_date, fact_by_run)
    window_dates = (
        candidate_dates[-(display_window + warmup_total):]
        if len(candidate_dates) > (display_window + warmup_total)
        else candidate_dates
    )

    fields_out: dict[str, Any] = {}
    for key, path, extractor, label, unit in _HISTORY_FIELD_SPECS:
        full_series = [
            extractor(_deep_get(canonical[d], path)) if canonical.get(d) is not None else None
            for d in window_dates
        ]
        rolling = _compute_field_rolling(full_series, ROLLING_WINDOW)
        start = max(0, len(full_series) - display_window)
        fields_out[key] = {
            "key": key,
            "label": label,
            "unit": unit,
            "series": full_series[start:],
            "mean20": rolling["mean20"][start:],
            "variance20": rolling["variance20"][start:],
            "std20": rolling["std20"][start:],
            "zscore20": rolling["zscore20"][start:],
            "percentile20": rolling["percentile20"][start:],
            "baselineCount": rolling["baselineCount"][start:],
        }

    display_window_dates = (
        window_dates[-display_window:] if len(window_dates) >= display_window else window_dates
    )
    display_dates = [d.isoformat() for d in display_window_dates]
    smc = _build_smc_projection(canonical, display_window_dates)
    momentum_volume = _build_momentum_volume_projection(canonical, display_window_dates)
    # [SLICE 4 / Price] 窄 Composition 历史（capital_tilt + leadership）：同一正式
    # published 日期轴 + 同一 published-run lineage 门控（纯选择器丢弃同日未发布 run）。
    composition_rows = await list_scope_composition_snapshots_for_dates(
        db,
        scope_type=scope_type,
        scope_key=scope_key,
        from_date=window_dates[0] if window_dates else trade_date,
        to_date=window_dates[-1] if window_dates else trade_date,
    )
    compositions = _select_published_compositions(composition_rows, run_id_by_date)
    price = _build_price_projection(compositions, display_window_dates)
    total = len(window_dates)
    availability = {
        "status": "ready" if total > 0 else "empty",
        "scopeType": scope_type,
        "scopeKey": scope_key,
        "totalSnapshots": total,
        "displayWindow": display_window,
        "fromDate": window_dates[0].isoformat() if window_dates else None,
        "toDate": window_dates[-1].isoformat() if window_dates else None,
    }
    return {
        "dates": display_dates,
        "displayWindow": display_window,
        "availability": availability,
        "fields": fields_out,
        "smc": smc,
        "momentumVolume": momentum_volume,
        "price": price,
    }


__all__ = [
    "get_scope_diagnostics",
    "HISTORY_DISPLAY_WINDOW",
    "HISTORY_WARMUP_TOTAL",
    "_select_published_facts",
    "build_canonical_by_date",
    "_compute_field_rolling",
    "_select_published_compositions",
    "_build_price_projection",
]
