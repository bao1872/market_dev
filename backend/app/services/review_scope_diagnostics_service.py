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
    zscore,
)
from app.models.market_review import ReviewScopeObservationFact
from app.services.review_observation_persistence_service import (
    ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES,
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


def _compute_field_rolling(
    full_series: list[float | None], window: int = ROLLING_WINDOW
) -> dict[str, list[float | None | int]]:
    """Compute lagged-baseline 20D rolling diagnostics for one aligned series.

    ``full_series`` is the value aligned to the FULL warmup window (ascending).
    ``baseline(i)`` = finite values in ``full_series[max(0, i-window) : i]``
    (strictly before i -> excludes T). Returns arrays aligned to ``full_series``.
    """
    n = len(full_series)
    mean20: list[float | None] = []
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
        s = safe_std(baseline)
        v = full_series[i]
        mean20.append(m)
        std20.append(s)
        z20.append(zscore(v, m, s))
        window_samples = baseline + ([v] if v is not None else [])
        p20.append(empirical_percentile(v, window_samples))
        bcount.append(len(baseline))
    return {
        "mean20": mean20,
        "std20": std20,
        "zscore20": z20,
        "percentile20": p20,
        "baselineCount": bcount,
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
        db, limit=display_window + warmup_total + 100
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
            "std20": rolling["std20"][start:],
            "zscore20": rolling["zscore20"][start:],
            "percentile20": rolling["percentile20"][start:],
            "baselineCount": rolling["baselineCount"][start:],
        }

    display_dates = [d.isoformat() for d in window_dates[-display_window:]]
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
    }


__all__ = [
    "get_scope_diagnostics",
    "HISTORY_DISPLAY_WINDOW",
    "HISTORY_WARMUP_TOTAL",
    "_select_published_facts",
    "build_canonical_by_date",
    "_compute_field_rolling",
]
