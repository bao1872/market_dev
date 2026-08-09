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
                    "source_kind": "live",
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
                "source_kind": "live",
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
            constraint="uq_review_obs_live_run_scope_component",
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


async def persist_history_replay_observations(
    session: AsyncSession,
    *,
    source_history_run_id: uuid.UUID,
    history_contract_version: str,
    taxonomy_compatibility_key: str | None,
    trade_date: date,
    scope_type: str,
    scope_key: str,
    membership_version: str | None,
    algorithm_version: str,
    flat_list: list[dict[str, Any]],
    payloads: dict[str, dict[str, Any]],
) -> int:
    """[CHANGE-20260808] HISTORY_REPLAY observation 持久化（M2 dual lineage）。

    source_kind='history_replay'，review_run_id=NULL，source_history_run_id=canonical history
    source，history_contract_version=required contract。不得伪造 stock_core/board/review_run_id
    给历史数据。on_conflict 用 HISTORY_REPLAY partial unique index。
    """
    input_hash = build_member_input_hash(flat_list)
    membership = membership_version or "unversioned"
    rows: list[dict[str, Any]] = []
    for metric_code, payload in payloads.items():
        for component in payload.get("components") or []:
            if not isinstance(component, dict):
                continue
            rows.append(
                {
                    "source_kind": "history_replay",
                    "review_run_id": None,
                    "source_history_run_id": source_history_run_id,
                    "history_contract_version": history_contract_version,
                    "taxonomy_compatibility_key": taxonomy_compatibility_key,
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
                "source_kind": "history_replay",
                "review_run_id": None,
                "source_history_run_id": source_history_run_id,
                "history_contract_version": history_contract_version,
                "taxonomy_compatibility_key": taxonomy_compatibility_key,
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
            constraint="uq_review_obs_replay_run_date_scope_component",
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
    """Load only earlier observations produced by the exact algorithm version.

    [CHANGE-20260808] Canonical baseline precedence（M2 dual lineage）：
    同一 logical (date, scope, metric, component) 可能存在 LIVE 与 HISTORY_REPLAY 两条
    observation。只选一条 canonical：
        1. published/succeeded canonical LIVE（review_run_id -> MarketReviewRun published + succeeded）
        2. valid HISTORY_REPLAY（source_kind='history_replay'）
        3. failed/partial/unpublished LIVE 不得覆盖 replay baseline（排除）
    严格 trade_date < target_date，每 date×scope×metric×component baseline 最多 1 条。

    注：本轮 M2 migration 未 apply；source_kind / source_history_run_id 列在模型已定义，
    若 DB 尚无列则 getattr 兼容（旧 schema 无 source_kind → 视为 live）。
    """
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

    # ---- canonical precedence（同 logical date 去重）----
    # 预取 live observation 的 run published/status（若 review_run_id 存在）
    run_ids = {
        o.review_run_id for o in observations
        if getattr(o, "source_kind", "live") == "live" and o.review_run_id is not None
    }
    run_status: dict[uuid.UUID, tuple[bool, str]] = {}
    if run_ids:
        from app.models.market_review import MarketReviewRun
        run_rows = (
            await session.execute(
                select(MarketReviewRun.id, MarketReviewRun.published_at, MarketReviewRun.status)
                .where(MarketReviewRun.id.in_(run_ids))
            )
        ).all()
        run_status = {
            row[0]: (row[1] is not None, row[2] or "")
            for row in run_rows
        }

    def _canonical_rank(o: MarketReviewMetricObservation) -> int:
        """越小越优先。rank 0 = published/succeeded live；1 = history_replay；2 = 其他 live（排除）。"""
        kind = getattr(o, "source_kind", "live")
        if kind == "history_replay":
            return 1
        if o.review_run_id is not None:
            published, status = run_status.get(o.review_run_id, (False, ""))
            if published and status == "succeeded":
                return 0
        return 2  # failed/partial/unpublished live → 排除（rank 2 被过滤）

    # 按 canonical key 选最低 rank；rank==2 的 live 排除
    canonical: dict[tuple, MarketReviewMetricObservation] = {}
    for o in observations:
        rank = _canonical_rank(o)
        if rank >= 2:
            continue
        key = (
            o.trade_date, o.scope_type, o.scope_key,
            o.metric_code, o.component_name,
        )
        existing = canonical.get(key)
        if existing is None or rank < _canonical_rank(existing):
            canonical[key] = o

    canonical_obs = list(canonical.values())
    dates = sorted({o.trade_date for o in canonical_obs}, reverse=True)[:baseline_window]
    allowed_dates = set(dates)
    history: dict[str, dict[str, list[float]]] = {}
    metric_values_by_date: dict[date, dict[str, float]] = {}
    for observation in reversed(canonical_obs):
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
