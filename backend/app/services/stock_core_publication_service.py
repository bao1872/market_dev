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


async def _has_supersede_columns(db: Any) -> bool:
    """探测 factor_publications 是否已含 supersede/fencing 列（Migration 087 后为 True）。

    [CHANGE-20260806 / PG-暴露缺陷] 原实现用**同步** db.execute 在 async session 上调用，
    psycopg async 会话同步 execute 抛错 → 恒返回 False → Migration 087 被判未应用 →
    真实盘后链 fail-closed 拒绝发布。改为 async await。
    """
    try:
        result = await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'factor_publications'"
            )
        )
        cols = {row[0] for row in result}
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
    if not await _has_supersede_columns(db):
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

    has_sup = await _has_supersede_columns(db)

    # 3. 旧 pointer（当前有效）→ supersede
    # [CHANGE-20260806-CP4A-Amendment / PG-暴露缺陷] FOR UPDATE 行锁：两个并发发布同时读到同一
    # 旧 pointer，需先锁行再 supersede+insert。无锁时并发发布都会读到旧行并各自插入新行 → partial
    # unique 冲突，但冲突结果不确定（谁赢取决于时序）。加行锁后事务串行化：先提交者 supersede 旧行并
    # 插入新 current；后到者 FOR UPDATE 重新读取时旧行已 superseded（不再命中 superseded_by IS NULL），
    # 返回 None → 不再 supersede，其 insert 新行与先提交者冲突 → 唯一约束拒绝 → **并发只成功一个**。
    old_row = (
        await db.execute(
            select(FactorPublication).where(
                FactorPublication.scope_key == scope_key,
                FactorPublication.trade_date == trade_date,
                FactorPublication.publication_kind == publication_kind,
                column("superseded_by").is_(None),  # 动态列（Migration 087 后存在）
            ).with_for_update()  # FOR UPDATE：锁定当前有效 pointer 行，串行化并发发布
        )
    ).scalar_one_or_none() if has_sup else None

    # 4. 写新 publication pointer（partial unique：同一 scope/date/kind 仅一个 superseded_by IS NULL）
    if not has_sup:
        # [CHANGE-20260806 / P0-C] Migration 087 缺失 → fail-closed，禁止发布。
        raise StockCorePublicationError(
            "STOCK_CORE_PUBLICATION_SCHEMA_NOT_READY: "
            "Migration 087 未应用（缺 supersede/fencing 列），禁止 stock_core 发布"
        )
    pub = FactorPublication(
        id=uuid4(),  # [PG-暴露缺陷] 预生成 id：先 supersede 旧行（写入 pub.id），再插入新行，
        # 否则新行 superseded_by IS NULL 与旧行（也是 NULL）同时命中 partial unique 索引冲突。
        scope_type="market" if scope_key == "market" else "instrument",
        scope_key=scope_key,
        trade_date=trade_date,
        publication_kind=publication_kind,
        algorithm_version=algorithm_version,
        data_run_id=snapshot_run_id,
        coverage_ratio=coverage_ratio,
        published_at=datetime.now(UTC),
        publish_worker_id=worker_id,
        publish_lease_epoch=int(lease_epoch),
    )
    # 先 supersede 旧行（使其不再命中 partial unique WHERE superseded_by IS NULL）
    if old_row is not None:
        old_row.superseded_by = pub.id
        old_row.superseded_at = datetime.now(UTC)
    db.add(pub)
    await db.flush()  # 插入新行（此时旧行已 superseded，partial unique 只命中新行）

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


async def reconcile_stock_core_publication(
    db: Any,
    *,
    trade_date: Any,
    scope_key: str = "market",
    publication_kind: str = "stock_core",
) -> dict[str, Any]:
    """独立 reconcile：修复历史/部署中断遗留的 pointer/run 分裂，正常 scheduled 不调用。

    [CHANGE-20260806-CP4A.2 / Step4]
    处理（只读修正，不含新发布）：
    - pointer 已存在但对应 snapshot run 仍 running（旧 two-phase 中断）→ 标 succeeded/published；
    - 存在**多个**当前有效（superseded_by IS NULL）pointer → 保留 data_run 最新/最晚 published，
      supersede 其余（历史分裂）。
    - run 已 succeeded 但 pointer 缺失 → 不动（由下次发布补齐），记录告警。

    正常 scheduled 路径**不**调用本函数（只调用 publish_stock_core_atomically）。
    """
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    result: dict[str, Any] = {
        "run_finalized": 0,
        "superseded_duplicates": 0,
        "warnings": [],
    }

    if not await _has_supersede_columns(db):
        # Migration 087 未应用：无法安全 reconcile supersede → 不写，仅告警
        result["warnings"].append("Migration 087 未应用，跳过 supersede reconcile")
        return result

    # 1. 当前有效 pointers（superseded_by IS NULL）按 scope/date/kind
    pointers = (
        await db.execute(
            select(FactorPublication).where(
                FactorPublication.scope_key == scope_key,
                FactorPublication.trade_date == trade_date,
                FactorPublication.publication_kind == publication_kind,
                column("superseded_by").is_(None),
            ).order_by(FactorPublication.published_at.desc())
        )
    ).scalars().all()

    # 2. 多个当前有效 pointer → 保留最新，supersede 其余（历史分裂修复）
    if len(pointers) > 1:
        keep = pointers[0]
        for dup in pointers[1:]:
            dup.superseded_by = keep.id  # type: ignore[attr-defined]
            dup.superseded_at = datetime.now(UTC)  # type: ignore[attr-defined]
            result["superseded_duplicates"] += 1

    # 3. 每个当前有效 pointer：若对应 snapshot run 仍 running → 标 succeeded/published
    for p in pointers:
        run = await db.get(StockFeatureSnapshotRun, p.data_run_id)
        if run is not None and run.status in ("running", "pending"):
            run.status = "succeeded"
            run.published_at = run.published_at or p.published_at
            result["run_finalized"] += 1

    await db.flush()
    logger.info(
        "reconcile_stock_core_publication: run_finalized=%d superseded_duplicates=%d "
        "warnings=%d",
        result["run_finalized"], result["superseded_duplicates"], len(result["warnings"]),
    )
    return result
