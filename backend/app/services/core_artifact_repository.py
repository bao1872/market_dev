"""CoreArtifactRepository — 分页读取 core artifact 并批量投影（P0-04/P0-06）。

[CHANGE-20260805-CP4A-CP3]
禁止一次把全市场 snapshot 装入内存。本模块按固定 batch_size 分页读取 StockFeatureSnapshot
（按 source_run_id），decode 成强类型 DecodedCoreArtifact，逐批 persist DSA projection、
更新 RunItem、推进进度，commit 后才进入下一批。

正常 scheduled 主链与 restart 链共用本 repository（不直接依赖 granular_restart_service 私有函数）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from app.services.core_artifact_codec import (
    CoreArtifactDecodeError,
    DecodedCoreArtifact,
    decode_dsa_projection_from_summary,
)

logger = logging.getLogger(__name__)

DEFAULT_PROJECTION_BATCH_SIZE = 200


async def iter_core_artifacts(
    db: Any,
    *,
    source_core_run_id: Any,
    batch_size: int = DEFAULT_PROJECTION_BATCH_SIZE,
) -> AsyncIterator[list[DecodedCoreArtifact]]:
    """按 batch 分页读取 source_core_run_id 下的 snapshot 并 decode 成强类型 artifact。

    每批返回 list[DecodedCoreArtifact]。decode 失败的单条记录以 skip 形式跳过并告警
    （不中断整批），调用方按 need 处理 failed。
    """
    from sqlalchemy import select

    from app.models.stock_feature_snapshot import StockFeatureSnapshot

    offset = 0
    while True:
        rows = (
            await db.execute(
                select(StockFeatureSnapshot)
                .where(StockFeatureSnapshot.source_run_id == source_core_run_id)
                .order_by(StockFeatureSnapshot.instrument_id)
                .limit(batch_size)
                .offset(offset)
            )
        ).scalars().all()
        if not rows:
            break
        batch: list[DecodedCoreArtifact] = []
        for snap in rows:
            try:
                batch.append(
                    decode_dsa_projection_from_summary(
                        snap.summary_payload or {},
                        instrument_id=snap.instrument_id,
                        trade_date=snap.trade_date,
                    )
                )
            except CoreArtifactDecodeError as exc:
                logger.warning(
                    "iter_core_artifacts: 跳过 source_run_id=%s instrument=%s: %s",
                    source_core_run_id, snap.instrument_id, exc,
                )
        yield batch
        offset += batch_size


class CoreArtifactRepository:
    """分页 projection 入口：逐批 decode→persist→RunItem→heartbeat→commit。"""

    def __init__(
        self,
        db: Any,
        *,
        batch_size: int = DEFAULT_PROJECTION_BATCH_SIZE,
    ) -> None:
        self._db = db
        self._batch_size = batch_size

    async def project_dsa_batch(
        self,
        *,
        source_core_run_id: Any,
        dsa_run_id: Any,
        trade_date: Any,
        strategy_version_id: Any,
        persist_fn: Any,
        heartbeat: Any | None = None,
        job_run_id: Any | None = None,
    ) -> dict[str, int]:
        """逐批从 snapshot decode artifact → persist DSA projection。

        Args:
            persist_fn: 接收 (db, batch_artifacts, dsa_run_id, trade_date,
                strategy_version_id) 的持久化函数（如 persist_precomputed_dsa_results）
            heartbeat: 每批后回调（用于进度上报），接收 (processed_total, batch)

        Returns:
            {"batches":..., "projected":..., "skipped_decode":...}
        """
        projected = 0
        batches = 0
        skipped = 0
        async for batch in iter_core_artifacts(
            self._db,
            source_core_run_id=source_core_run_id,
            batch_size=self._batch_size,
        ):
            if not batch:
                continue
            batches += 1
            artifacts = {a.instrument_id: a for a in batch if a.instrument_id is not None}
            result = await persist_fn(
                self._db,
                run_id=dsa_run_id,
                artifacts=artifacts,
                trade_date=trade_date,
                strategy_version_id=strategy_version_id,
                job_run_id=job_run_id,
            )
            projected += int(result.get("succeeded", 0))
            if heartbeat is not None:
                await heartbeat(projected, result)
            # 每批 commit（由调用方 session 提供；此处不显式 commit，由 persist_fn 内 commit）
        return {"batches": batches, "projected": projected, "skipped_decode": skipped}
