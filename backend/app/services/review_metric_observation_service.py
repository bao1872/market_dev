"""Persistence and version-safe history reads for Review metric observations."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_review import MarketReviewMetricObservation


def build_member_input_hash(flat_list: list[dict[str, Any]]) -> str:
    stable = [
        {
            key: value
            for key, value in sorted(flat.items())
            if key not in {"_snapshot_id", "_history_state_id"}
        }
        for flat in sorted(flat_list, key=lambda item: str(item.get("_instrument_id", "")))
    ]
    payload = json.dumps(stable, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


async def persist_metric_observations(
    session: AsyncSession,
    *,
    review_run_id: uuid.UUID,
    trade_date: date,
    scope_type: str,
    scope_key: str,
    membership_version: str | None,
    algorithm_version: str,
    flat_list: list[dict[str, Any]],
    payloads: dict[str, dict[str, Any]],
) -> int:
    """Idempotently persist every component raw value and the normalized metric value."""
    input_hash = build_member_input_hash(flat_list)
    membership = membership_version or "unversioned"
    rows: list[dict[str, Any]] = []
    for metric_code, payload in payloads.items():
        for component in payload.get("components") or []:
            if not isinstance(component, dict):
                continue
            rows.append(
                {
                    "review_run_id": review_run_id,
                    "trade_date": trade_date,
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "metric_code": metric_code,
                    "component_name": str(component.get("name")),
                    "raw_value": _decimal(component.get("rawValue")),
                    "denominator": component.get("denominator"),
                    "field_source_json": {
                        "fieldSource": component.get("fieldSource"),
                        "extra": component.get("extra"),
                    },
                    "weight_mode": component.get("weightMode") or "equal_weight",
                    "algorithm_version": algorithm_version,
                    "input_hash": input_hash,
                    "membership_version": membership,
                    "status": component.get("status") or "unavailable",
                }
            )
        rows.append(
            {
                "review_run_id": review_run_id,
                "trade_date": trade_date,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "metric_code": metric_code,
                "component_name": "_metric_value",
                "raw_value": _decimal(payload.get("value")),
                "denominator": None,
                "field_source_json": {"fieldSource": "normalized_metric_value"},
                "weight_mode": "derived",
                "algorithm_version": algorithm_version,
                "input_hash": input_hash,
                "membership_version": membership,
                "status": payload.get("status") or "unavailable",
            }
        )

    for row in rows:
        stmt = pg_insert(MarketReviewMetricObservation).values(**row)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_review_metric_observation_run_scope_component",
            set_={
                "raw_value": stmt.excluded.raw_value,
                "denominator": stmt.excluded.denominator,
                "field_source_json": stmt.excluded.field_source_json,
                "weight_mode": stmt.excluded.weight_mode,
                "algorithm_version": stmt.excluded.algorithm_version,
                "input_hash": stmt.excluded.input_hash,
                "membership_version": stmt.excluded.membership_version,
                "status": stmt.excluded.status,
            },
        )
        await session.execute(stmt)
    await session.flush()
    return len(rows)


async def load_metric_history(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_key: str,
    trade_date: date,
    algorithm_version: str,
    baseline_window: int,
) -> tuple[
    dict[str, dict[str, list[float]]] | None,
    dict[str, float] | None,
    dict[str, float] | None,
]:
    """Load only earlier observations produced by the exact algorithm version."""
    stmt = (
        select(MarketReviewMetricObservation)
        .where(
            MarketReviewMetricObservation.scope_type == scope_type,
            MarketReviewMetricObservation.scope_key == scope_key,
            MarketReviewMetricObservation.trade_date < trade_date,
            MarketReviewMetricObservation.algorithm_version == algorithm_version,
        )
        .order_by(MarketReviewMetricObservation.trade_date.desc())
    )
    observations = list((await session.execute(stmt)).scalars())
    dates = sorted({item.trade_date for item in observations}, reverse=True)[:baseline_window]
    allowed_dates = set(dates)
    history: dict[str, dict[str, list[float]]] = {}
    metric_values_by_date: dict[date, dict[str, float]] = {}
    for observation in reversed(observations):
        if observation.trade_date not in allowed_dates or observation.raw_value is None:
            continue
        value = float(observation.raw_value)
        history.setdefault(observation.metric_code, {}).setdefault(
            observation.component_name, []
        ).append(value)
        if observation.component_name == "_metric_value":
            metric_values_by_date.setdefault(observation.trade_date, {})[
                observation.metric_code
            ] = value
    prev = metric_values_by_date.get(dates[0]) if dates else None
    prev5 = metric_values_by_date.get(dates[4]) if len(dates) >= 5 else None
    return history or None, prev or None, prev5 or None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError):
        return None
