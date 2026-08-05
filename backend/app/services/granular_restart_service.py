"""V2.1 Granular Restart 调度服务（Corrective Pass 3）。

[PRD 31 §6] 正式枚举（10 个 boundary）：
    daily_ready / board_facts / core / stock_core_published /
    dsa_projection / state_events / chip / auction / board_aggregation / review

## Corrective Pass 3 修复的确定性缺陷（相对 fdb09a1）

1. **废除 `_MAINCHAIN_RESUME_STEP` / `last_completed_step` 伪造 restart**。
   orchestrator 的 `_completed_steps` 映射表根本不认识 `checking_coverage`
   （其键只有 refreshing_daily / syncing_boards / computing_features / publishing /
   computing_review / succeeded），把 `last_completed_step="checking_coverage"`
   写进 metadata 会命中 `_completed_steps.get(...)` 的默认空集合，
   语义等于「什么都没完成」——与「从 core 链开始、跳过日线刷新」完全相反。
   本轮改为：主链 boundary 创建 **child SchedulerJobRun**（operation=boundary），
   在 metadata 中写入 `restart_from` / `mainchain_stage` / `execution_mode`
   显式标记，由 worker 从对应阶段执行，**绝不写 last_completed_step**。

2. **`dispatch_restart` 按 `restart_from` 显式分派到 10 个真实 handler**，
   不再有「主链 vs 子产品」两套语义割裂的分支。

3. **真实函数签名对齐**（本轮逐个读取源码确认）：
   - `publish_chip_consensus(session, trade_date: date, chip_run_id, algorithm_version, *, metadata=None)`
     —— **无 `operator` 参数**；上一轮 `publish_chip_consensus(db, run_id, operator=...)` 会 TypeError。
   - `publish_review(session, run: MarketReviewRun, *, force, operator, idempotency_key)`
     —— 第一个业务参数是 **ORM 对象**，不是 id；上一轮传 id 会 AttributeError。
   - `publish_board_analysis(session, snapshot: BoardAnalysisSnapshot, *, threshold=...)`
   - auction 重建 = `generate_auction_anchors(db, trade_date)` → `publish_auction_anchors(db, snapshot_id)`；
     上一轮只调 publish，等于重发旧 snapshot，不是重建。
   - dsa_projection 重建 = 从持久化 core artifact `build_dsa_projection_payload(...)`；
     上一轮调 `StrategyBatchService.publish_run` 只是把 StrategyRun 改 published，不是重建。

4. **`state_events` 补真实重建 handler**：`rebuild_state_events()` 冻结当日 core run
   的 eligible universe，调用领域级 `state_event_service.generate_events_for_run`
   （读 StockFeatureSnapshot core artifact → 派生转换事件 → ON CONFLICT DO NOTHING
   幂等 upsert），并统计 coverage。

5. **幂等键含 parent/source/input_hash**：
   `run_key = f"granular_restart:{trade_date}:{boundary}:{parent}:{source}:{input_hash}"`。
   succeeded 且同 input_hash → 直接返回，**不再执行 handler**；running/queued → 返回
   已有 active child；failed → attempt_no+1 重新执行；source/input 变化 → 新 child。

## 诚实边界

- 主链四 boundary（daily_ready / board_facts / core / stock_core_published）
  在 orchestrator 中**没有 per-step 公共入口**：`execute_orchestrator_step(step, operation, ...)`
  是通用「步骤包装器」，需要调用方自己传 operation 闭包，无法按 boundary 名调度。
  因此主链 handler 的真实实现是「创建 child run + 写阶段标记 + 写事件」，
  child 保持 `queued` 交由 worker 领取执行，**不写 succeeded**（不伪造成功）。
- 子产品六 boundary 在本进程内同步执行真实重建/发布，成功置 succeeded，失败置 failed
  并写 level=error 事件，记录真实异常，绝不返回 501、绝不伪造成功。

测试策略：纯单元测试注入 fake db + handler/publisher（PURE_UNIT_TEST）；
真实 PG 路径在远程验证库首跑验证（Phase 4，见 rules/80 DS-110）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.job_run_event_service import append_event

logger = logging.getLogger(__name__)

ALL_BOUNDARIES: tuple[str, ...] = (
    "daily_ready",
    "board_facts",
    "core",
    "stock_core_published",
    "dsa_projection",
    "state_events",
    "chip",
    "auction",
    "board_aggregation",
    "review",
)

# 主链 boundary → orchestrator 阶段标记（写入 child metadata.mainchain_stage）。
# **注意**：这不是 last_completed_step（那是「已完成检查点」，语义相反）。
# 这里的值表示「worker 应当从哪个阶段开始执行」。
_MAINCHAIN_START_STAGE: dict[str, str] = {
    "daily_ready": "syncing_boards",       # 已有日线：跳过 refreshing_daily，从板块/core 链开始
    "board_facts": "syncing_boards",       # 只重跑 board facts
    "core": "computing_features",          # 新建 core run，算 trend/structure/momentum
    "stock_core_published": "publishing",  # 只重试 stock_core publication
}

_MAINCHAIN_BOUNDARIES = frozenset(_MAINCHAIN_START_STAGE.keys())

# child run 的活跃（未终结）状态
_ACTIVE_CHILD_STATUS = frozenset({"queued", "running", "pending"})
# child run 的成功终态
_SUCCEEDED_CHILD_STATUS = frozenset({"succeeded", "completed", "partial_success"})


class RestartHandler(Protocol):
    """boundary 级真实 handler 协议（真实实现或单测注入）。

    返回新建 / 复用的目标 run id（无明确目标 run 时返回 None）。
    """

    async def __call__(
        self,
        db: AsyncSession,
        *,
        trade_date: str,
        parent_job_run_id: uuid.UUID,
        source_core_run_id: uuid.UUID | None,
        input_hash: str,
        actor: str,
        attempt: int,
    ) -> uuid.UUID | None:
        ...


# =============================================================================
# lineage / 幂等辅助
# =============================================================================


def _as_date(trade_date: str | date) -> date:
    """把 trade_date 归一为 `datetime.date`（真实 publisher 签名要求 date）。"""
    if isinstance(trade_date, date):
        return trade_date
    return date.fromisoformat(str(trade_date))


def compute_input_hash(
    *,
    trade_date: str,
    boundary: str,
    source_core_run_id: uuid.UUID | None,
    extra: dict[str, Any] | None = None,
) -> str:
    """派生 restart 输入 hash（幂等键组成部分）。

    输入变化（换了 source core run / 换了参数）→ hash 变化 → 创建新 child，
    不复用旧 child 的 succeeded 结果。
    """
    material = {
        "trade_date": str(trade_date),
        "boundary": boundary,
        "source_core_run_id": str(source_core_run_id) if source_core_run_id else None,
        "extra": extra or {},
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


def build_run_key(
    *,
    trade_date: str,
    boundary: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
) -> str:
    """幂等键：含 parent / source / input_hash，禁止跨 parent 或跨输入误复用。"""
    return (
        f"granular_restart:{trade_date}:{boundary}:"
        f"{parent_job_run_id}:{source_core_run_id or 'none'}:{input_hash}"
    )


async def _resolve_published_core_run_id(
    db: AsyncSession, trade_date: str,
) -> uuid.UUID | None:
    """读取当日已发布 stock_core pointer 的 data_run_id（lineage 唯一真源）。

    所有子产品 boundary 的重建都必须绑定这个 core run —— 禁止 max(created_at) 猜。
    """
    try:
        from app.models.factor_publication import FactorPublication

        stmt = (
            select(FactorPublication.data_run_id)
            .where(
                FactorPublication.publication_kind == "stock_core",
                FactorPublication.trade_date == _as_date(trade_date),
            )
            .order_by(FactorPublication.published_at.desc())
            .limit(1)
        )
        row = (await db.execute(stmt)).first()
        return row[0] if row else None
    except Exception as exc:  # 表缺失 / 字段漂移：记录后返回 None，由 handler 决定是否硬失败
        logger.warning("解析 stock_core pointer 失败 trade_date=%s: %s", trade_date, exc)
        return None


async def _find_existing_child(
    db: AsyncSession, run_key: str,
) -> SchedulerJobRun | None:
    """按 run_key 查找已有 child（取最新 attempt）。"""
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.run_key == run_key)
        .order_by(SchedulerJobRun.attempt_no.desc(), SchedulerJobRun.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _create_or_reuse_child(
    db: AsyncSession,
    *,
    parent_job_run_id: uuid.UUID,
    boundary: str,
    trade_date: str,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[SchedulerJobRun, bool, int]:
    """创建或复用 child SchedulerJobRun。

    Returns:
        (child, should_execute, attempt)
        - should_execute=False 表示**不得再执行 handler**（succeeded 复用 / 已有 active child）。
        - attempt 为本次执行的 attempt_no。
    """
    run_key = build_run_key(
        trade_date=trade_date,
        boundary=boundary,
        parent_job_run_id=parent_job_run_id,
        source_core_run_id=source_core_run_id,
        input_hash=input_hash,
    )
    existing = await _find_existing_child(db, run_key)

    if existing is not None:
        existing_meta: dict[str, Any] = {}
        try:
            existing_meta = json.loads(existing.metadata_json or "{}")
        except (TypeError, ValueError):
            existing_meta = {}
        same_input = existing_meta.get("input_hash") == input_hash

        # 1) succeeded + 同 input_hash → 直接返回，不再执行 handler（真幂等）
        if existing.status in _SUCCEEDED_CHILD_STATUS and same_input:
            await append_event(
                db,
                job_run_id=existing.id,
                step="manual_restart",
                level="info",
                message=(
                    f"granular restart 幂等命中: boundary={boundary} 已成功且 input_hash 未变，"
                    f"跳过重复执行 (run_key={run_key})"
                ),
                payload={
                    "reused": True,
                    "reason": "already_succeeded_same_input",
                    "parent_job_run_id": str(parent_job_run_id),
                    "input_hash": input_hash,
                },
            )
            return existing, False, int(existing.attempt_no or 1)

        # 2) running / queued → 返回已有 active child，不重复调度
        if existing.status in _ACTIVE_CHILD_STATUS:
            await append_event(
                db,
                job_run_id=existing.id,
                step="manual_restart",
                level="info",
                message=(
                    f"granular restart 已有进行中的子任务: boundary={boundary}, "
                    f"status={existing.status}，不重复调度"
                ),
                payload={
                    "reused": True,
                    "reason": "active_child_exists",
                    "parent_job_run_id": str(parent_job_run_id),
                },
            )
            return existing, False, int(existing.attempt_no or 1)

        # 3) failed / 其他终态 → 创建新 attempt 重新执行（不复用失败 run）
        attempt = int(existing.attempt_no or 1) + 1
    else:
        attempt = 1

    metadata: dict[str, Any] = {
        "parent_job_run_id": str(parent_job_run_id),
        "operation": boundary,
        "restart_from": boundary,
        "trade_date": trade_date,
        "source_core_run_id": str(source_core_run_id) if source_core_run_id else None,
        "input_hash": input_hash,
        "attempt": attempt,
        "triggered_by": actor,
        **(extra_metadata or {}),
    }
    child = SchedulerJobRun(
        job_name=f"granular_restart_{boundary}",
        business_date=trade_date,
        run_key=run_key,
        status="queued",
        attempt_no=attempt,
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    db.add(child)
    await db.flush()
    await append_event(
        db,
        job_run_id=child.id,
        step="manual_restart",
        level="info",
        message=(
            f"granular restart 子任务创建: boundary={boundary}, attempt={attempt}, "
            f"source_core_run_id={source_core_run_id}"
        ),
        payload={
            "parent_job_run_id": str(parent_job_run_id),
            "operation": boundary,
            "input_hash": input_hash,
            "attempt": attempt,
        },
    )
    return child, True, attempt


# =============================================================================
# 主链 boundary handler（真实：创建 child + 阶段标记，不写 last_completed_step）
# =============================================================================


async def _handle_mainchain(
    db: AsyncSession,
    boundary: str,
    *,
    trade_date: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    attempt: int,
) -> uuid.UUID | None:
    """主链 boundary 共用实现：把 child 标记为「待 worker 从指定阶段执行」。

    orchestrator 没有可按 boundary 名调用的 per-step 入口
    （`execute_orchestrator_step(step, operation, ...)` 需要调用方传 operation 闭包），
    因此本 handler **不在 API 进程内执行主链计算**，而是：

    - 保持 child.status=queued（由 after-close worker 领取）；
    - 写 metadata.mainchain_stage = worker 应开始的阶段；
    - 写 metadata.execution_mode = "worker_pull"，明确不在此进程执行；
    - 写事件说明真实状态。

    **绝不设置 last_completed_step**（orchestrator `_completed_steps` 不认识
    `checking_coverage`，且语义为「已完成」，与「从此阶段开始」相反）。
    """
    stage = _MAINCHAIN_START_STAGE[boundary]
    await append_event(
        db,
        job_run_id=parent_job_run_id,
        step="manual_restart",
        level="info",
        message=(
            f"主链 granular restart 已排队: boundary={boundary}, "
            f"start_stage={stage}, attempt={attempt}；"
            f"由 after-close worker 领取执行（不设 last_completed_step）"
        ),
        payload={
            "restart_from": boundary,
            "mainchain_stage": stage,
            "execution_mode": "worker_pull",
            "source_core_run_id": str(source_core_run_id) if source_core_run_id else None,
            "input_hash": input_hash,
        },
    )
    # 主链重算的目标 run（新的 StockFeatureSnapshotRun）由 worker 创建，此处无 run id。
    return None


def _mainchain_extra_metadata(boundary: str) -> dict[str, Any]:
    """主链 child 的额外 metadata（worker 领取时读取）。"""
    stage = _MAINCHAIN_START_STAGE[boundary]
    extra: dict[str, Any] = {
        "mainchain_stage": stage,
        "execution_mode": "worker_pull",
        "skip_refreshing_daily": boundary in ("daily_ready", "board_facts", "core"),
    }
    if boundary == "board_facts":
        extra["restart_scope"] = "board_facts_only"
    if boundary == "stock_core_published":
        extra["restart_scope"] = "stock_core_publication_only"
        extra["reuse_existing_core_run"] = True
    return extra


async def _handle_daily_ready(
    db: AsyncSession, **kw: Any,
) -> uuid.UUID | None:
    return await _handle_mainchain(db, "daily_ready", **kw)


async def _handle_board_facts(
    db: AsyncSession, **kw: Any,
) -> uuid.UUID | None:
    return await _handle_mainchain(db, "board_facts", **kw)


async def _handle_core(
    db: AsyncSession, **kw: Any,
) -> uuid.UUID | None:
    return await _handle_mainchain(db, "core", **kw)


async def _handle_stock_core_published(
    db: AsyncSession, **kw: Any,
) -> uuid.UUID | None:
    return await _handle_mainchain(db, "stock_core_published", **kw)


# =============================================================================
# dsa_projection：从持久化 core artifact 重建投影（禁止再次运行 DSA）
# =============================================================================


def _artifact_from_snapshot(
    snapshot: Any,
    *,
    source_core_run_id: uuid.UUID,
    parameter_hash: str,
    algorithm_versions: dict[str, str],
) -> Any:
    """从持久化 StockFeatureSnapshot 重建 CoreComputationArtifact。

    core artifact 未单独建表，其 DSA 指标持久化在
    `summary_payload["first_pyramid"]["trend"]["continuousFactors"]`
    （见 first_pyramid_flatten.flatten_first_pyramid 读取路径）。
    本函数只做**读取重组**，不重新计算任何 DSA 指标。
    """
    from app.services.core_run_context import CoreComputationArtifact

    summary = getattr(snapshot, "summary_payload", None) or {}
    first_pyramid = summary.get("first_pyramid") or {}
    trend = first_pyramid.get("trend") or {}
    continuous = trend.get("continuousFactors") or {}

    return CoreComputationArtifact(
        instrument_id=snapshot.instrument_id,
        trade_date=snapshot.trade_date,
        payload={"dsa": dict(continuous)},
        visual=first_pyramid.get("visual") or {},
        availability=(first_pyramid.get("availability") or {}),
        source_core_run_id=source_core_run_id,
        parameter_hash=parameter_hash,
        algorithm_versions=dict(algorithm_versions),
    )


async def _handle_dsa_projection(
    db: AsyncSession,
    *,
    trade_date: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    attempt: int,
) -> uuid.UUID | None:
    """[PRD §6] 从持久化 core artifact 重建 DSA projection，**禁止再次运行 DSA**。

    真实路径：
    1. 加载当前 core run（stock_core pointer.data_run_id）的全部 StockFeatureSnapshot；
    2. 逐股从持久化 payload 重建 CoreComputationArtifact（只读重组，不计算）；
    3. `build_dsa_projection_payload(artifact, expected_core_run_id=..., ...)` 强制
       source_core_run_id / parameter_hash / dsa version 三项对账；
    4. 把 projection 写回该 snapshot 的 `summary_payload["dsa_projection"]`
       （DSA projection 无独立表，正式持久化位置即 core snapshot payload）。
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    from app.services.core_run_context import (
        CoreRunContext,
        build_default_algorithm_versions,
    )
    from app.services.dsa_projection_service import build_dsa_projection_payload

    if source_core_run_id is None:
        raise RuntimeError(
            "dsa_projection 重建缺少 source_core_run_id（当日无已发布 stock_core pointer），"
            "禁止基于任意 run 重建投影"
        )

    core_run = await db.get(StockFeatureSnapshotRun, source_core_run_id)
    if core_run is None:
        raise RuntimeError(f"core run 不存在: {source_core_run_id}")

    run_meta = getattr(core_run, "metadata_", None) or {}
    algorithm_versions = run_meta.get("algorithm_versions") or build_default_algorithm_versions()
    parameter_hash = run_meta.get("parameter_hash")
    if not parameter_hash:
        # run 未持久化 parameter_hash 时，按 run 的算法版本 + 配置派生（与 core 计算同源）
        parameter_hash = CoreRunContext(
            trade_date=_as_date(trade_date),
            run_calculated_at=core_run.started_at or datetime.now(UTC),
            algorithm_versions=dict(algorithm_versions),
            config=run_meta.get("config") or {},
            run_id=source_core_run_id,
        ).parameter_hash

    snapshots = list(
        (
            await db.execute(
                select(StockFeatureSnapshot).where(
                    StockFeatureSnapshot.source_run_id == source_core_run_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not snapshots:
        raise RuntimeError(
            f"core run {source_core_run_id} 无任何 StockFeatureSnapshot，无法重建 DSA projection"
        )

    rebuilt = 0
    failed: list[str] = []
    for snapshot in snapshots:
        try:
            artifact = _artifact_from_snapshot(
                snapshot,
                source_core_run_id=source_core_run_id,
                parameter_hash=parameter_hash,
                algorithm_versions=algorithm_versions,
            )
            payload = build_dsa_projection_payload(
                artifact,
                expected_core_run_id=source_core_run_id,
                expected_core_parameter_hash=parameter_hash,
                expected_dsa_version=algorithm_versions.get("dsa"),
            )
            summary = dict(getattr(snapshot, "summary_payload", None) or {})
            summary["dsa_projection"] = payload
            snapshot.summary_payload = summary
            flag_modified(snapshot, "summary_payload")
            rebuilt += 1
        except Exception as exc:
            failed.append(f"{snapshot.instrument_id}: {type(exc).__name__}: {exc}")

    await db.flush()

    eligible = len(snapshots)
    coverage = rebuilt / eligible if eligible else 0.0
    await append_event(
        db,
        job_run_id=parent_job_run_id,
        step="manual_restart",
        level="info" if not failed else "warning",
        message=(
            f"dsa_projection 重建完成: eligible={eligible}, rebuilt={rebuilt}, "
            f"failed={len(failed)}, coverage={coverage:.4f}"
        ),
        payload={
            "source_core_run_id": str(source_core_run_id),
            "eligible_count": eligible,
            "matched_count": rebuilt,
            "coverage_ratio": round(coverage, 4),
            "failed_samples": failed[:5],
        },
    )
    if rebuilt == 0:
        raise RuntimeError(
            f"dsa_projection 重建全部失败（eligible={eligible}）: {failed[:3]}"
        )
    return source_core_run_id


# =============================================================================
# state_events：真实重建（冻结 eligible universe + 从 core artifact 派生 + 幂等 upsert）
# =============================================================================


async def rebuild_state_events(
    db: AsyncSession,
    trade_date: str | date,
    source_core_run_id: uuid.UUID,
) -> dict[str, Any]:
    """[PRD §6] 从当前 core artifact 重建 state events（不重算 core）。

    真实重建路径（不是「重发旧 run」）：
    1. **冻结 eligible universe**：当日归属该 core run 的全部 StockFeatureSnapshot
       instrument 集合（core run 的 universe，不用当日全表）；
    2. **从 core artifact 派生事件**：调用领域级
       `state_event_service.generate_events_for_run`，它读取该 run 的快照、
       批量取每股前一个成功 run 的兼容快照、比较稳定 code、生成生命周期转换事件；
    3. **幂等 upsert**：`ON CONFLICT (idempotency_key) DO NOTHING`，
       幂等键 = `symbol:source_run_id:algorithm_version`，重复重建不产生重复事件；
    4. **coverage / version / lineage 统计**：返回 eligible / matched / coverage /
       algorithm_versions，供门禁与 readiness 使用。

    Returns:
        {source_core_run_id, eligible_count, matched_count, coverage_ratio,
         algorithm_versions, event_type_counts, generated}

    Raises:
        RuntimeError: core run 不存在 / 未 succeeded / eligible universe 为空。
    """
    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import (
        STATUS_SUCCEEDED,
        StockFeatureSnapshotRun,
    )
    from app.models.stock_state_event import StockStateEvent
    from app.services.state_event_service import generate_events_for_run

    core_run = await db.get(StockFeatureSnapshotRun, source_core_run_id)
    if core_run is None:
        raise RuntimeError(f"state_events 重建失败: core run 不存在 {source_core_run_id}")
    if core_run.status != STATUS_SUCCEEDED:
        raise RuntimeError(
            f"state_events 重建失败: core run {source_core_run_id} 状态={core_run.status}，"
            "只允许从 succeeded core run 重建事件"
        )

    # 1) 冻结 eligible universe：归属该 core run 的 instrument 集合
    eligible_rows = (
        await db.execute(
            select(StockFeatureSnapshot.instrument_id)
            .where(StockFeatureSnapshot.source_run_id == source_core_run_id)
            .distinct()
        )
    ).all()
    eligible_ids = {row[0] for row in eligible_rows}
    eligible_count = len(eligible_ids)
    if eligible_count == 0:
        raise RuntimeError(
            f"state_events 重建失败: core run {source_core_run_id} 无任何快照，eligible universe 为空"
        )

    # 2)+3) 从 core artifact 派生事件并幂等 upsert（领域级实现）
    stats = await generate_events_for_run(db, source_core_run_id)
    await db.flush()

    # 4) coverage / version 统计（只统计归属该 core run 的事件）
    rows = (
        await db.execute(
            select(
                StockStateEvent.instrument_id,
                StockStateEvent.event_type,
                StockStateEvent.algorithm_version,
            ).where(StockStateEvent.source_run_id == source_core_run_id)
        )
    ).all()
    matched_ids: set[Any] = set()
    by_type: dict[str, int] = {}
    versions: set[str] = set()
    for instrument_id, event_type, algo_version in rows:
        matched_ids.add(instrument_id)
        by_type[str(event_type)] = by_type.get(str(event_type), 0) + 1
        if algo_version is not None:
            versions.add(str(algo_version))

    matched_count = len(matched_ids)
    coverage_ratio = matched_count / eligible_count if eligible_count else 0.0
    return {
        "source_core_run_id": str(source_core_run_id),
        "trade_date": str(trade_date),
        "eligible_count": eligible_count,
        "matched_count": matched_count,
        "coverage_ratio": round(coverage_ratio, 4),
        "algorithm_versions": sorted(versions),
        "event_type_counts": by_type,
        "generated": stats,
    }


async def _handle_state_events(
    db: AsyncSession,
    *,
    trade_date: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    attempt: int,
) -> uuid.UUID | None:
    """state_events boundary：真实重建 + 事件统计（PRD §6：新建 events run）。"""
    if source_core_run_id is None:
        raise RuntimeError(
            "state_events 重建缺少 source_core_run_id（当日无已发布 stock_core pointer）"
        )
    result = await rebuild_state_events(db, trade_date, source_core_run_id)
    await append_event(
        db,
        job_run_id=parent_job_run_id,
        step="manual_restart",
        level="info",
        message=(
            f"state_events 重建完成: eligible={result['eligible_count']}, "
            f"matched={result['matched_count']}, coverage={result['coverage_ratio']}"
        ),
        payload=result,
    )
    return source_core_run_id


# =============================================================================
# chip / auction / board_aggregation / review：真实领域重建 + 发布
# =============================================================================


async def _handle_chip(
    db: AsyncSession,
    *,
    trade_date: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    attempt: int,
) -> uuid.UUID | None:
    """chip boundary：查找当日可发布 ChipConsensusRun → 真实 publish_chip_consensus。

    真实签名（已核对 factor_publication_service）：
        publish_chip_consensus(session, trade_date: date, chip_run_id: UUID,
                               algorithm_version: str, *, metadata=None)
    —— **无 operator 参数**，trade_date / algorithm_version 从 chip_run 取。
    """
    from app.models.chip_consensus_run import ChipConsensusRun
    from app.services.factor_publication_service import publish_chip_consensus

    stmt = (
        select(ChipConsensusRun)
        .where(
            ChipConsensusRun.trade_date == _as_date(trade_date),
            ChipConsensusRun.status.in_(("succeeded", "partial", "partial_success")),
        )
        .order_by(ChipConsensusRun.created_at.desc())
        .limit(1)
    )
    if source_core_run_id is not None:
        stmt = (
            select(ChipConsensusRun)
            .where(
                ChipConsensusRun.trade_date == _as_date(trade_date),
                ChipConsensusRun.source_core_run_id == source_core_run_id,
                ChipConsensusRun.status.in_(("succeeded", "partial", "partial_success")),
            )
            .order_by(ChipConsensusRun.created_at.desc())
            .limit(1)
        )
    chip_run = (await db.execute(stmt)).scalar_one_or_none()
    if chip_run is None:
        raise RuntimeError(
            f"chip 重建失败: 当日无可发布 ChipConsensusRun "
            f"(trade_date={trade_date}, source_core_run_id={source_core_run_id})"
        )

    await publish_chip_consensus(
        db,
        chip_run.trade_date,
        chip_run.id,
        chip_run.algorithm_version,
        metadata={
            "granular_restart": True,
            "boundary": "chip",
            "operator": actor,
            "attempt": attempt,
            "input_hash": input_hash,
        },
    )
    return chip_run.id


async def _handle_auction(
    db: AsyncSession,
    *,
    trade_date: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    attempt: int,
) -> uuid.UUID | None:
    """auction boundary：**重建 anchor** 后发布（PRD §6：允许重算 anchor）。

    generate_auction_anchors 创建新的 AuctionAnchorSnapshot 并返回 dict 含 snapshot_id；
    publish_auction_anchors 只发布已有 snapshot。**必须先 generate 再 publish**，
    只调 publish 等于重发旧 snapshot，不是重建。
    """
    from app.services.auction_anchor_service import (
        generate_auction_anchors,
        publish_auction_anchors,
    )

    # worker_id 可选，仅写入日志；此处标注 granular restart 来源便于溯源。
    result = await generate_auction_anchors(
        db,
        _as_date(trade_date),
        worker_id=f"granular_restart:{actor}",
    )
    snapshot_id = result.get("snapshot_id") if isinstance(result, dict) else None
    if snapshot_id is None:
        raise RuntimeError(
            f"auction 重建失败: generate_auction_anchors 未返回 snapshot_id, result={result}"
        )
    if not isinstance(snapshot_id, uuid.UUID):
        snapshot_id = uuid.UUID(str(snapshot_id))
    await publish_auction_anchors(db, snapshot_id)
    return snapshot_id


async def _handle_board_aggregation(
    db: AsyncSession,
    *,
    trade_date: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    attempt: int,
) -> uuid.UUID | None:
    """board_aggregation boundary：查找当日 succeeded BoardAnalysisSnapshot → publish_board_analysis。

    真实签名：publish_board_analysis(session, snapshot: BoardAnalysisSnapshot, *, threshold=...)
    —— 接收 **ORM 对象**，不是 id。
    """
    from app.models.board_analysis_snapshot import BoardAnalysisSnapshot
    from app.services.board_analysis_service import publish_board_analysis

    conditions = [
        BoardAnalysisSnapshot.trade_date == _as_date(trade_date),
        BoardAnalysisSnapshot.status == "succeeded",
    ]
    if source_core_run_id is not None:
        conditions.append(BoardAnalysisSnapshot.source_core_run_id == source_core_run_id)
    stmt = (
        select(BoardAnalysisSnapshot)
        .where(*conditions)
        .order_by(BoardAnalysisSnapshot.created_at.desc())
        .limit(1)
    )
    snapshot = (await db.execute(stmt)).scalar_one_or_none()
    if snapshot is None:
        raise RuntimeError(
            f"board_aggregation 重建失败: 当日无 succeeded BoardAnalysisSnapshot "
            f"(trade_date={trade_date}, source_core_run_id={source_core_run_id})"
        )
    await publish_board_analysis(db, snapshot)
    return snapshot.id


async def _handle_review(
    db: AsyncSession,
    *,
    trade_date: str,
    parent_job_run_id: uuid.UUID,
    source_core_run_id: uuid.UUID | None,
    input_hash: str,
    actor: str,
    attempt: int,
) -> uuid.UUID | None:
    """review boundary：查找当日 MarketReviewRun → db.get 取 ORM 对象 → publish_review。

    真实签名：publish_review(session, run: MarketReviewRun, *, force=False,
                            operator=None, idempotency_key=None)
    —— 第一个业务参数是 **ORM 对象**，传 id 会 AttributeError。
    """
    from app.models.market_review import MarketReviewRun
    from app.services.review_publication_service import publish_review

    stmt = (
        select(MarketReviewRun.id)
        .where(
            MarketReviewRun.trade_date == _as_date(trade_date),
            MarketReviewRun.status.in_(("succeeded", "completed", "published")),
        )
        .order_by(MarketReviewRun.created_at.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise RuntimeError(
            f"review 重建失败: 当日无可发布 MarketReviewRun (trade_date={trade_date})"
        )
    run_id = row[0]
    run = await db.get(MarketReviewRun, run_id)
    if run is None:
        raise RuntimeError(f"review 重建失败: MarketReviewRun {run_id} 不存在")

    await publish_review(
        db,
        run,
        operator=actor,
        idempotency_key=f"granular_restart:{trade_date}:review:{input_hash}",
    )
    return run_id


# =============================================================================
# handler registry（真实实现的唯一权威）
# =============================================================================

_REAL_HANDLERS: dict[str, RestartHandler] = {
    "daily_ready": _handle_daily_ready,
    "board_facts": _handle_board_facts,
    "core": _handle_core,
    "stock_core_published": _handle_stock_core_published,
    "dsa_projection": _handle_dsa_projection,
    "state_events": _handle_state_events,
    "chip": _handle_chip,
    "auction": _handle_auction,
    "board_aggregation": _handle_board_aggregation,
    "review": _handle_review,
}


def is_implemented_boundary(boundary: str) -> bool:
    """以真实 handler registry 为唯一权威（禁止「枚举即实现」）。"""
    return boundary in _REAL_HANDLERS


def implemented_boundaries() -> tuple[str, ...]:
    """当前有真实 handler 的 boundary（诚实清单）。"""
    return tuple(b for b in ALL_BOUNDARIES if b in _REAL_HANDLERS)


# =============================================================================
# dispatch
# =============================================================================


async def dispatch_restart(
    db: AsyncSession,
    job_run: SchedulerJobRun,
    restart_from: str | None,
    *,
    actor: str,
    request_id: str,
    handlers: dict[str, RestartHandler] | None = None,
) -> SchedulerJobRun:
    """按 `restart_from` 显式分派到真实 boundary handler。

    统一语义（不再区分「主链改父 run」和「子产品建 child」两套返回）：
    - 所有 boundary 都创建 / 复用一个 child SchedulerJobRun（operation=boundary）；
    - 子产品六 boundary 在本调用内同步执行真实重建/发布，成功 succeeded、失败 failed；
    - 主链四 boundary 的 child 保持 queued，由 after-close worker 从 `mainchain_stage`
      开始执行（**不写 last_completed_step**，不伪造成功）。

    Returns:
        child SchedulerJobRun（调用方据其 status / metadata 反馈给前端）。

    Raises:
        ValueError: 未知 boundary 或无法解析 trade_date。
        NotImplementedError: boundary 无真实 handler（当前 10/10 已实现）。
    """
    if restart_from not in ALL_BOUNDARIES:
        raise ValueError(f"未知 restart_from boundary: {restart_from}")

    handler = (handlers or {}).get(restart_from) or _REAL_HANDLERS.get(restart_from)
    if handler is None:
        raise NotImplementedError(
            f"restart_from={restart_from} 无真实领域级 handler，未实现（不得伪造成功）"
        )

    try:
        meta = json.loads(job_run.metadata_json or "{}")
    except (TypeError, ValueError):
        meta = {}
    trade_date = meta.get("trade_date") or job_run.business_date
    if not trade_date:
        raise ValueError("无法解析 trade_date（metadata.trade_date 与 business_date 均缺失）")
    trade_date = str(trade_date)

    # lineage：所有 boundary 都绑定当日已发布 stock_core pointer 的 data_run_id。
    # 主链 core / daily_ready 会新建 core run，此处仅作为「输入 pointer」记录。
    source_core_run_id = await _resolve_published_core_run_id(db, trade_date)
    input_hash = compute_input_hash(
        trade_date=trade_date,
        boundary=restart_from,
        source_core_run_id=source_core_run_id,
        extra={"request_scope": "granular_restart"},
    )

    is_mainchain = restart_from in _MAINCHAIN_BOUNDARIES
    child, should_execute, attempt = await _create_or_reuse_child(
        db,
        parent_job_run_id=job_run.id,
        boundary=restart_from,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        input_hash=input_hash,
        actor=actor,
        extra_metadata=(
            _mainchain_extra_metadata(restart_from) if is_mainchain else {"execution_mode": "inline"}
        ),
    )

    if not should_execute:
        # 幂等命中：不重复执行 handler，直接返回既有 child。
        await db.commit()
        return child

    if is_mainchain:
        # 主链：写阶段标记 + 事件，child 保持 queued 交由 worker 执行（不伪造成功）。
        await handler(
            db,
            trade_date=trade_date,
            parent_job_run_id=job_run.id,
            source_core_run_id=source_core_run_id,
            input_hash=input_hash,
            actor=actor,
            attempt=attempt,
        )
        await append_event(
            db,
            job_run_id=child.id,
            step="manual_restart",
            level="info",
            message=(
                f"主链 boundary={restart_from} 已排队等待 worker 从 "
                f"{_MAINCHAIN_START_STAGE[restart_from]} 阶段执行"
            ),
            payload={"request_id": request_id, "restart_from": restart_from},
        )
        await db.commit()
        return child

    # 子产品：本调用内同步执行真实重建 / 发布。
    try:
        child.status = "running"
        child.started_at = datetime.now(UTC)
        await db.flush()
        target_run_id = await handler(
            db,
            trade_date=trade_date,
            parent_job_run_id=job_run.id,
            source_core_run_id=source_core_run_id,
            input_hash=input_hash,
            actor=actor,
            attempt=attempt,
        )
        child.status = "succeeded"
        child.finished_at = datetime.now(UTC)
        child_meta = json.loads(child.metadata_json or "{}")
        child_meta["target_run_id"] = str(target_run_id) if target_run_id else None
        child.metadata_json = json.dumps(child_meta, ensure_ascii=False)
        await append_event(
            db,
            job_run_id=child.id,
            step="manual_restart",
            level="info",
            message=f"boundary={restart_from} 重建/发布完成: target_run_id={target_run_id}",
            payload={
                "target_run_id": str(target_run_id) if target_run_id else None,
                "request_id": request_id,
                "attempt": attempt,
            },
        )
    except Exception as exc:  # 真实 lineage/pointer 缺失等：记录真实原因，不 501、不伪造成功
        child.status = "failed"
        child.finished_at = datetime.now(UTC)
        child.error_code = "granular_restart_failed"
        child.error_message = f"{type(exc).__name__}: {exc}"
        await append_event(
            db,
            job_run_id=child.id,
            step="manual_restart",
            level="error",
            message=f"boundary={restart_from} 重建/发布失败（真实原因，非 501）: {exc}",
            payload={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "request_id": request_id,
                "attempt": attempt,
            },
        )
    await db.commit()
    return child
