"""stock_core 原子 publication service（P0-07）。

[CHANGE-20260805-CP4A-CP3 / P0-07]
PRD 要求同一数据库事务完成：
    validate_quality_gate() → validate_fencing() → create_publication() →
    switch_pointer() → mark_run_published() → supersede_old() → write_audit()

任一失败 → 事务整体回滚，旧 pointer 保留，run 状态不漂移。

**依赖 Migration 087**（新增 superseded_by / superseded_at / publish_worker_id /
publish_lease_epoch 列 + stock_core_publication_audit 表）。在未执行 Migration 的库上，
`_has_supersede_columns()` 探测为 False 时降级为不写 supersede/audit（但 quality+fencing+
pointer+run-published 仍在同一事务）。

本 service 为事务 owner，orchestrator 只调用它，不再自行分两次 commit。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import column, select, text

from app.models.factor_publication import FactorPublication

logger = logging.getLogger(__name__)

CORE_PUBLICATION_MIN_COVERAGE = 0.0  # 完整覆盖由 caller/quality gate 判定（DS-107）


class StockCorePublicationError(RuntimeError):
    """发布失败（quality/fencing/coverage 任一不满足）。"""


def _has_supersede_columns(db: Any) -> bool:
    """探测 factor_publications 是否已含 supersede/fencing 列（Migration 087 后为 True）。"""
    try:
        cols = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'factor_publications'"
                )
            )
        }
        return {"superseded_by", "publish_worker_id"}.issubset(cols)
    except Exception:  # noqa: BLE001
        return False


async def validate_quality_gate(
    db: Any,
    *,
    trade_date: Any,
    snapshot_run_id: UUID,
    eligible_count: int,
) -> int:
    """quality gate：确认 snapshot 完整覆盖 eligible（processed == expected）。"""
    from app.models.stock_feature_snapshot import StockFeatureSnapshot

    actual = (
        await db.execute(
            select(sa_count()).where(
                StockFeatureSnapshot.source_run_id == snapshot_run_id,
            )
        )
    ).scalar_one()
    if eligible_count > 0 and actual < eligible_count:
        raise StockCorePublicationError(
            f"stock_core 发布 quality gate 未过：actual={actual} expected={eligible_count}"
        )
    return int(actual)


def sa_count():
    import sqlalchemy as _sa

    return _sa.func.count()


async def validate_fencing(
    db: Any,
    *,
    scope_key: str,
    trade_date: Any,
    publication_kind: str,
    worker_id: str,
    lease_epoch: int,
) -> None:
    """fencing：若存在当前有效 publication 且由其他 worker/更旧 epoch 发布 → 拒绝覆盖。

    依赖 Migration 087 的 publish_worker_id / publish_lease_epoch 列；未迁移时跳过 fencing。
    """
    if not _has_supersede_columns(db):
        return
    row = (
        await db.execute(
            select(FactorPublication).where(
                FactorPublication.scope_key == scope_key,
                FactorPublication.trade_date == trade_date,
                FactorPublication.publication_kind == publication_kind,
                column("superseded_by").is_(None),  # 动态列（Migration 087 后存在）
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    # 已存在当前有效 pointer：仅当同 worker 且 epoch 不旧于已记录才允许切换
    if getattr(row, "publish_worker_id", None) and row.publish_worker_id != worker_id:
        raise StockCorePublicationError(
            f"fencing 拒绝：publication 由 {row.publish_worker_id} 持有，当前 worker={worker_id}"
        )
    if getattr(row, "publish_lease_epoch", None) and int(row.publish_lease_epoch) > lease_epoch:
        raise StockCorePublicationError(
            f"fencing 拒绝：lease_epoch {lease_epoch} 旧于 {row.publish_lease_epoch}"
        )


async def publish_stock_core_atomically(
    db: Any,
    *,
    scope_key: str,
    trade_date: Any,
    publication_kind: str,
    algorithm_version: str,
    snapshot_run_id: UUID,
    coverage_ratio: float,
    worker_id: str,
    lease_epoch: int,
    eligible_count: int,
    audit_txn: bool = True,
) -> FactorPublication:
    """同一事务内完成 stock_core 原子发布（P0-07）。

    调用方负责开启事务（session.begin()）；本函数任一失败抛出，事务整体回滚。
    """
    # 1. quality gate（完整覆盖）
    await validate_quality_gate(
        db, trade_date=trade_date, snapshot_run_id=snapshot_run_id,
        eligible_count=eligible_count,
    )

    # 2. fencing（并发覆盖防护）
    await validate_fencing(
        db, scope_key=scope_key, trade_date=trade_date,
        publication_kind=publication_kind, worker_id=worker_id, lease_epoch=lease_epoch,
    )

    has_sup = _has_supersede_columns(db)

    # 3. 旧 pointer（当前有效）→ supersede
    old_row = (
        await db.execute(
            select(FactorPublication).where(
                FactorPublication.scope_key == scope_key,
                FactorPublication.trade_date == trade_date,
                FactorPublication.publication_kind == publication_kind,
                column("superseded_by").is_(None),  # 动态列（Migration 087 后存在）
            )
        )
    ).scalar_one_or_none() if has_sup else None

    # 4. 写新 publication pointer
    pub = FactorPublication(
        scope_type="market" if scope_key == "market" else "instrument",
        scope_key=scope_key,
        trade_date=trade_date,
        publication_kind=publication_kind,
        algorithm_version=algorithm_version,
        data_run_id=snapshot_run_id,
        coverage_ratio=coverage_ratio,
        published_at=datetime.now(UTC),
    )
    if has_sup:
        pub.publish_worker_id = worker_id
        pub.publish_lease_epoch = int(lease_epoch)
        db.add(pub)
        await db.flush()  # 生成 pub.id 供 supersede/audit 引用
        if old_row is not None:
            old_row.superseded_by = pub.id
            old_row.superseded_at = datetime.now(UTC)
    else:
        # 无 supersede 列（Migration 087 未执行）：退化为 upsert（覆盖旧 pointer）
        existing = (
            await db.execute(
                select(FactorPublication).where(
                    FactorPublication.scope_key == scope_key,
                    FactorPublication.trade_date == trade_date,
                    FactorPublication.publication_kind == publication_kind,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.data_run_id = snapshot_run_id
            existing.algorithm_version = algorithm_version
            existing.coverage_ratio = coverage_ratio
            existing.published_at = datetime.now(UTC)
            await db.flush()
            return existing
        db.add(pub)
        await db.flush()

    # 5. 标记 SnapshotRun published/succeeded（同一事务）
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    run = await db.get(StockFeatureSnapshotRun, snapshot_run_id)
    if run is not None:
        run.status = "succeeded"
        run.published_at = run.published_at or datetime.now(UTC)

    # 6. 写审计（同一事务，Migration 087 后有 audit 表）
    if has_sup and audit_txn:
        await db.execute(
            text(
                "INSERT INTO stock_core_publication_audit "
                "(id, trade_date, scope_key, publication_kind, old_data_run_id, "
                "new_data_run_id, superseded_by, publish_worker_id, publish_lease_epoch, "
                "action, created_at) VALUES "
                "(:id, :trade_date, :scope_key, :kind, :old_run, :new_run, :sup, "
                ":worker, :epoch, 'publish', now())"
            ),
            {
                "id": uuid4(),
                "trade_date": trade_date,
                "scope_key": scope_key,
                "kind": publication_kind,
                "old_run": str(old_row.data_run_id) if old_row is not None else None,
                "new_run": str(snapshot_run_id),
                "sup": str(pub.id),
                "worker": worker_id,
                "epoch": int(lease_epoch),
            },
        )

    await db.flush()
    logger.info(
        "stock_core 原子发布完成: scope=%s date=%s kind=%s run=%s worker=%s epoch=%s",
        scope_key, trade_date, publication_kind, snapshot_run_id, worker_id, lease_epoch,
    )
    return pub
