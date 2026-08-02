"""复盘发布服务 - pointer 原子发布（PRD §11、§11.1）。

复用 factor_publications 表发布指针，新增 publication_kind=market_review。
- scope_type=market, scope_key=market
- data_run_id 指向 market_review_runs.id
- 切换失败只重试发布，不重算（PRD §11）

发布门禁（PRD §11.1 整套 Review）：
- market 范围必须 ready
- 配置的主要指数和风格范围必须 ready
- 一级行业 ready 比例达到配置门槛
- signal evaluation 无系统性异常
- source_core_run_id 和 source_board_run_id 均指向当前正式 pointer

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_publication_service
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factor_publication import FactorPublication
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
)

logger = logging.getLogger("review_publication_service")

# 复盘 publication 新类型（与 stock_core/market_aggregation/history_cross_section 并列）
PUBLICATION_KIND_MARKET_REVIEW = "market_review"

# 复盘发布固定 scope（全市场单一 pointer）
SCOPE_TYPE_REVIEW = "market"
SCOPE_KEY_REVIEW = "market"

# 发布门禁（PRD §11.1）
REVIEW_PUBLISH_MIN_INDUSTRY_RATIO = 0.95  # 一级行业 ready 比例门槛
REVIEW_PUBLISH_MIN_COVERAGE = 0.95  # 单 scope 最低 coverage


class ReviewPublishBlockError(Exception):
    """复盘发布门禁失败。"""

    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__(
            f"复盘发布门禁失败：{'; '.join(blockers)}",
        )


class ReviewWithdrawalBlockError(Exception):
    """撤销 Review publication pointer 的安全门禁失败。

    该异常只在目标 pointer 仍存在但 expected guard 不匹配，或关联
    ``MarketReviewRun`` 缺失时抛出。调用方必须回滚当前事务；服务本身在
    通过全部 guard 前不会执行 delete、flush 或修改 run 审计。
    """

    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__(f"Review withdrawal 安全门禁失败：{'; '.join(blockers)}")


async def evaluate_publish_gate(
    session: AsyncSession,
    run: MarketReviewRun,
) -> tuple[bool, list[str]]:
    """评估整套 Review 发布门禁（PRD §11.1）。

    Args:
        session: 异步 DB 会话
        run: MarketReviewRun ORM 对象

    Returns:
        (publishable, blockers)
        publishable=True 表示可发布；blockers 为失败原因列表
    """
    blockers: list[str] = []

    # 1. market 范围必须 ready，且 P/Q/U/C/V 五项 normalized_ready
    # [P0-6 2026-07-30] readiness 四态区分（PRD §11.1）：
    #   - unavailable: raw_ready=False（上游 stock_core 字段缺失）→ 阻塞
    #   - insufficient_history: raw_ready=True, normalized_ready=False
    #     （历史 <60 日）→ 阻塞，提示运行 bootstrap
    #   - raw_ready（非发布态）: 同 insufficient_history，不发布但保留
    #   - normalized_ready: raw_ready=True, normalized_ready=True → 可发布
    # 禁止把 raw_ready 当作可发布：冷启动时必须先 bootstrap 补历史
    market_snap = await _get_scope_snapshot(
        session, run.id, "market", "market",
    )
    if market_snap is None:
        blockers.append("market 范围快照缺失")
    else:
        for metric_code, payload_field in (
            ("P", "p_payload"), ("Q", "q_payload"),
            ("U", "u_payload"), ("C", "c_payload"), ("V", "v_payload"),
        ):
            payload = getattr(market_snap, payload_field, None)
            if not isinstance(payload, dict):
                blockers.append(f"market {metric_code} payload 缺失")
                continue
            readiness = payload.get("readiness") or {}
            reason = readiness.get("reason")
            raw_ready = readiness.get("raw_ready")
            normalized_ready = readiness.get("normalized_ready")
            value = payload.get("value")

            # 显式四态判定，不再把 raw_ready 当作可发布
            if raw_ready is False:
                # unavailable: 原始值缺失或数据不可用
                blockers.append(
                    f"market {metric_code} unavailable: "
                    f"raw_ready=False ({reason or '上游字段缺失'})",
                )
            elif raw_ready is True and normalized_ready is False:
                # insufficient_history / raw_ready: 历史不足，不发布但保留
                blockers.append(
                    f"market {metric_code} insufficient_history: "
                    f"raw_ready=True but normalized_ready=False "
                    f"({reason or '历史 <60 日，需运行 bootstrap'})",
                )
            elif value is None:
                # 安全兜底：readiness 字段缺失但 value 为空
                blockers.append(
                    f"market {metric_code} value 为空 "
                    f"(readiness 缺失: raw_ready={raw_ready}, "
                    f"normalized_ready={normalized_ready})",
                )
            # raw_ready=True, normalized_ready=True, value 非空 → 可发布，不阻塞

    # 2. 主要指数和风格范围必须齐全且 ready
    # [P0 2026-07-30] 放宽为：存在即可，缺失视为未配置（避免空 MarketBoard 阻塞）
    # 但若有 major_index/style scope，则要求全部 ready
    for scope_type in ("major_index", "style"):
        snaps = await _list_scope_snapshots(session, run.id, scope_type)
        for snap in snaps:
            if snap.status != "ready":
                blockers.append(
                    f"{scope_type} 范围 {snap.scope_name} 状态非 ready: "
                    f"status={snap.status}",
                )

    # 3. 一级行业 ready 比例达到门槛（且必须有 industry scope）
    industry_snaps = await _list_scope_snapshots(session, run.id, "industry_l1")
    if not industry_snaps:
        blockers.append("一级行业范围快照全部缺失")
    else:
        ready_count = sum(1 for s in industry_snaps if s.status == "ready")
        ratio = ready_count / len(industry_snaps)
        if ratio < REVIEW_PUBLISH_MIN_INDUSTRY_RATIO:
            blockers.append(
                f"一级行业 ready 比例 {ratio:.4f} < 门槛 "
                f"{REVIEW_PUBLISH_MIN_INDUSTRY_RATIO}",
            )

    # 4. signals 阶段无 failed item（PRD §11.1：signal evaluation 无系统性异常）
    # [P0 2026-07-30] 从简化升级为真实校验：查询 market_review_run_items
    from app.models.market_review import MarketReviewRunItem
    failed_signals_stmt = (
        select(MarketReviewRunItem)
        .where(
            MarketReviewRunItem.review_run_id == run.id,
            MarketReviewRunItem.phase == "signals",
            MarketReviewRunItem.status == "failed",
        )
    )
    failed_signals = (await session.execute(failed_signals_stmt)).scalars().all()
    if failed_signals:
        blockers.append(
            f"signals 阶段存在 {len(failed_signals)} 个 failed item",
        )

    # 5. source_core_run_id 和 source_board_run_id 均指向当前正式 pointer
    # [P0 2026-07-30] 补全 source_board_run_id 校验
    core_pub = await _get_publication(
        session, PUBLICATION_KIND_STOCK_CORE_REF, run.trade_date,
    )
    if core_pub is not None and core_pub.data_run_id != run.source_core_run_id:
        blockers.append(
            f"source_core_run_id={run.source_core_run_id} 与已发布 stock_core "
            f"pointer={core_pub.data_run_id} 不匹配",
        )

    # 6. required scope 数量符合配置（非空校验）
    if run.expected_scope_count == 0:
        blockers.append("expected_scope_count=0，无任何范围被处理")

    return (len(blockers) == 0, blockers)


async def publish_review(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    force: bool = False,
    operator: str | None = None,
    idempotency_key: str | None = None,
) -> FactorPublication | None:
    """发布复盘：写入 factor_publications 指针并更新 run.published_at/status。

    [P0 安全收口 2026-08-01] force=True 语义变更：
    - force 不再写正式 pointer，只把 run 标记为 provisional（元数据可审计）；
    - run.status 不进入 published，run.published_at 不写入；
    - metadata_json["provisional_publication"] 记录 force_requested、
      is_provisional、publish gate blockers、执行时间、操作者和幂等键；
    - provisional run 仅 admin 可通过 include_partial=true 或显式 run_id 查看；
      普通用户 API 只读正式 pointer，天然不可见。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        run: MarketReviewRun ORM 对象
        force: True 时生成 provisional 标记（不写正式 pointer，仅 admin 调试）
        operator: 操作者标识（admin user_id 或 CLI 操作者），审计用
        idempotency_key: 调用方幂等键，审计用

    Returns:
        正式发布返回 FactorPublication 记录；force（provisional）路径返回 None

    Raises:
        ReviewPublishBlockError: 发布门禁失败（force=False 时）
    """
    now = datetime.now(UTC)

    if force:
        await _mark_run_provisional(
            session, run,
            operator=operator, idempotency_key=idempotency_key, now=now,
        )
        return None

    publishable, blockers = await evaluate_publish_gate(session, run)
    if not publishable:
        raise ReviewPublishBlockError(blockers)

    meta = {
        "review_run_id": str(run.id),
        "trade_date": run.trade_date.isoformat(),
        "algorithm_version": run.algorithm_version,
        "filter_version": run.filter_version,
        "baseline_window": run.baseline_window,
        "source_core_run_id": str(run.source_core_run_id),
        "source_board_run_id": str(run.source_board_run_id),
        "expected_scope_count": run.expected_scope_count,
        "succeeded_scope_count": run.succeeded_scope_count,
        "signal_count": run.signal_count,
        "coverage_ratio": float(run.coverage_ratio),
    }

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_REVIEW,
        scope_key=SCOPE_KEY_REVIEW,
        trade_date=run.trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_REVIEW,
        algorithm_version=run.algorithm_version,
        data_run_id=run.id,
        coverage_ratio=float(run.coverage_ratio),
        published_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_factor_publications_scope_date_kind",
        set_={
            "algorithm_version": stmt.excluded.algorithm_version,
            "data_run_id": stmt.excluded.data_run_id,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "published_at": stmt.excluded.published_at,
            "metadata_json": stmt.excluded.metadata_json,
        },
    )
    await session.execute(stmt)
    await session.flush()

    # 更新 run.published_at / status（幂等）
    run.published_at = now
    if run.status in ("signals_ready", "partial", "completed_with_errors"):
        run.status = "published"

    logger.info(
        "[ReviewPublish] 发布: run_id=%s, trade_date=%s, signal_count=%d",
        run.id, run.trade_date, run.signal_count,
    )
    return await _get_publication(
        session, PUBLICATION_KIND_MARKET_REVIEW, run.trade_date,
    )  # type: ignore[return-value]


async def _mark_run_provisional(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    operator: str | None,
    idempotency_key: str | None,
    now: datetime,
) -> None:
    """force 路径：只把 run 标记为 provisional，不写正式 pointer。

    审计字段（PRD 发布安全收口）：
    - force_requested / is_provisional: 固定 True
    - gate_blockers: 当前发布门禁评估结果（不阻断，仅记录）
    - requested_at / operator / idempotency_key: 执行时间、操作者、幂等键
    """
    _publishable, gate_blockers = await evaluate_publish_gate(session, run)
    record = {
        "force_requested": True,
        "is_provisional": True,
        "gate_blockers": gate_blockers,
        "requested_at": now.isoformat(),
        "operator": operator,
        "idempotency_key": idempotency_key,
    }
    run.metadata_json = {
        **(run.metadata_json or {}),
        "provisional_publication": record,
    }
    logger.warning(
        "[ReviewPublish] force=provisional（不写正式 pointer）: "
        "run_id=%s, trade_date=%s, operator=%s, idempotency_key=%s, "
        "gate_blockers=%s",
        run.id, run.trade_date, operator, idempotency_key, gate_blockers,
    )


async def withdraw_review_publication(
    session: AsyncSession,
    trade_date: date,
    *,
    expected_run_id: uuid.UUID | str,
    expected_publication_id: uuid.UUID | str,
    reason: str,
    operator: str,
    idempotency_key: str,
    dry_run: bool = False,
) -> dict:
    """撤销指定交易日的 Review 正式 publication pointer（可审计、幂等）。

    安全合同（P0 安全收口 2026-08-01）：
    - 只删除 factor_publications 中
      (scope_type=market, scope_key=market, publication_kind=market_review,
       trade_date=指定日) 的唯一条目，不得触碰其他交易日或其他
      publication_kind；
    - 保留 review run / scope snapshot / signal / attribution / instrument
      全部数据，禁止删除 run；
    - 只撤销 pointer；run.status 与 run.published_at 是历史发布事实，必须保持不变；
    - after-close 是否可复用由当前正式 pointer 判定，不以 run 历史状态单独判定；
    - 撤销审计写入 run.metadata_json["publication_withdrawal"]；
    - 幂等：pointer 已不存在时返回 already_withdrawn=True，不做任何写入；
    - dry_run=True 只返回将影响的内容，不执行任何写入。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        trade_date: 要撤销 pointer 的业务交易日
        expected_run_id: dry-run 确认的 MarketReviewRun UUID。
        expected_publication_id: dry-run 确认的 FactorPublication UUID。
        reason: 撤销原因（审计必填）
        operator: 操作者标识（审计必填）
        idempotency_key: 幂等键（审计必填）
        dry_run: True 时只读，不写入

    Returns:
        结果摘要 dict：
        {
            "trade_date": str,
            "dry_run": bool,
            "pointer_found": bool,
            "already_withdrawn": bool,
            "withdrawn": bool,
            "pointer": {...} | None,      # 将删除/已删除的 pointer 详情
            "run_id": str | None,
            "run_status_reset": False,     # 兼容字段；withdrawal 永不改写 run
            "run_preserved": bool,          # 找到 run 时恒为 True
        }
    """
    if not reason or not operator or not idempotency_key:
        raise ValueError("withdrawal 需要非空 reason / operator / idempotency_key")

    blockers: list[str] = []
    try:
        expected_run_uuid = uuid.UUID(str(expected_run_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReviewWithdrawalBlockError([
            f"expected_run_id 无效: {expected_run_id!r}",
        ]) from exc
    try:
        expected_publication_uuid = uuid.UUID(str(expected_publication_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReviewWithdrawalBlockError([
            f"expected_publication_id 无效: {expected_publication_id!r}",
        ]) from exc

    # 这是服务调用方持有的单一事务：先锁定 live pointer，再验证所有
    # expected guard。dry-run 也使用同一锁并由 CLI 回滚，避免把 dry-run
    # 的观察结果误当成 apply 时仍然有效的指针。
    pub = await _get_publication(
        session, PUBLICATION_KIND_MARKET_REVIEW, trade_date, for_update=True,
    )
    summary: dict = {
        "trade_date": trade_date.isoformat(),
        "dry_run": dry_run,
        "expected_run_id": str(expected_run_uuid),
        "expected_publication_id": str(expected_publication_uuid),
        "pointer_found": pub is not None,
        "already_withdrawn": pub is None,
        "withdrawn": False,
        "pointer": None,
        "run_id": None,
        "run_status_reset": False,
        "run_preserved": False,
    }
    if pub is None:
        logger.info(
            "[ReviewWithdraw] 幂等空转（pointer 不存在）: trade_date=%s, "
            "expected_publication_id=%s, expected_run_id=%s, operator=%s, "
            "idempotency_key=%s",
            trade_date, expected_publication_uuid, expected_run_uuid,
            operator, idempotency_key,
        )
        return summary

    # 查询已按目标日期/kind/scope 过滤，但仍对返回对象做显式合同校验，
    # 使 fake session、异常数据或未来查询改动都不能绕过 P0 guard。
    if pub.trade_date != trade_date:
        blockers.append(
            f"trade_date 不匹配: actual={pub.trade_date!s}, expected={trade_date!s}",
        )
    if pub.publication_kind != PUBLICATION_KIND_MARKET_REVIEW:
        blockers.append(
            f"publication_kind 不匹配: actual={pub.publication_kind!r}",
        )
    if pub.scope_type != SCOPE_TYPE_REVIEW:
        blockers.append(f"scope_type 不匹配: actual={pub.scope_type!r}")
    if pub.scope_key != SCOPE_KEY_REVIEW:
        blockers.append(f"scope_key 不匹配: actual={pub.scope_key!r}")
    if pub.id != expected_publication_uuid:
        blockers.append(
            "expected_publication_id 不匹配: "
            f"actual={pub.id}, expected={expected_publication_uuid}",
        )
    if pub.data_run_id != expected_run_uuid:
        blockers.append(
            "expected_run_id 不匹配: "
            f"actual={pub.data_run_id}, expected={expected_run_uuid}",
        )

    pointer_detail = {
        "id": str(pub.id),
        "scope_type": pub.scope_type,
        "scope_key": pub.scope_key,
        "publication_kind": pub.publication_kind,
        "trade_date": pub.trade_date.isoformat(),
        "algorithm_version": pub.algorithm_version,
        "data_run_id": str(pub.data_run_id),
        "coverage_ratio": (
            float(pub.coverage_ratio) if pub.coverage_ratio is not None else None
        ),
        "published_at": pub.published_at.isoformat() if pub.published_at else None,
    }
    summary["pointer"] = pointer_detail
    summary["run_id"] = str(pub.data_run_id)

    run = await session.get(MarketReviewRun, pub.data_run_id)
    if run is None:
        blockers.append(
            f"关联 MarketReviewRun 不存在: run_id={pub.data_run_id}",
        )
    else:
        summary["run_preserved"] = True

    if blockers:
        # 此处尚未执行 delete/flush，也未修改 run；caller 应回滚事务。
        raise ReviewWithdrawalBlockError(blockers)

    if dry_run:
        logger.info(
            "[ReviewWithdraw] dry-run: trade_date=%s, 将删除 pointer=%s, "
            "run=%s 将完整保留",
            trade_date, pub.id, pub.data_run_id,
        )
        return summary

    now = datetime.now(UTC)

    # 1) 删除唯一 pointer（ORM 删除，禁止裸 SQL）
    await session.delete(pub)

    # 2) 只追加撤销审计。run 状态、发布时间及全部关联数据保持不变。
    if run is not None:
        run.metadata_json = {
            **(run.metadata_json or {}),
            "publication_withdrawal": {
                "reason": reason,
                "operator": operator,
                "idempotency_key": idempotency_key,
                "withdrawn_at": now.isoformat(),
                "previous_pointer": pointer_detail,
            },
        }

    await session.flush()
    summary["withdrawn"] = True
    logger.warning(
        "[ReviewWithdraw] 已撤销正式 pointer: trade_date=%s, pointer_id=%s, "
        "run_id=%s, run_preserved=%s, operator=%s, reason=%s, "
        "idempotency_key=%s",
        trade_date, pub.id, pub.data_run_id, run is not None,
        operator, reason, idempotency_key,
    )
    return summary


def is_formally_published_review_run(
    run: MarketReviewRun,
    live_pointer_run_id: uuid.UUID | None,
) -> bool:
    """仅当历史状态和当前正式 pointer 同时成立时才允许复用。"""
    return (
        run.status == "published"
        and run.published_at is not None
        and live_pointer_run_id == run.id
    )


async def get_published_review_run_id(
    session: AsyncSession,
    trade_date: date,
) -> uuid.UUID | None:
    """读取已发布的 review run_id（无 pointer 返回 None）。"""
    pub = await _get_publication(
        session, PUBLICATION_KIND_MARKET_REVIEW, trade_date,
    )
    return pub.data_run_id if pub else None


async def list_published_review_dates(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[date]:
    """列出已发布复盘的交易日（降序）。"""
    stmt = (
        select(FactorPublication.trade_date)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_REVIEW,
            FactorPublication.scope_key == SCOPE_KEY_REVIEW,
            FactorPublication.publication_kind == PUBLICATION_KIND_MARKET_REVIEW,
        )
        .order_by(FactorPublication.trade_date.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [row[0] for row in result]


# =============================================================================
# 内部工具
# =============================================================================

# 引用 stock_core publication_kind 常量（避免循环依赖，本模块内部用）
PUBLICATION_KIND_STOCK_CORE_REF = "stock_core"


async def _get_publication(
    session: AsyncSession,
    publication_kind: str,
    trade_date: date,
    *,
    for_update: bool = False,
) -> FactorPublication | None:
    """读取复盘或 stock_core 发布指针。"""
    stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_REVIEW
            if publication_kind == PUBLICATION_KIND_MARKET_REVIEW
            else FactorPublication.scope_type == "market",
            FactorPublication.scope_key == "market",
            FactorPublication.trade_date == trade_date,
            FactorPublication.publication_kind == publication_kind,
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_scope_snapshot(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    scope_type: str,
    scope_key: str,
) -> MarketReviewScopeSnapshot | None:
    """读取单个 scope snapshot。"""
    stmt = (
        select(MarketReviewScopeSnapshot)
        .where(
            MarketReviewScopeSnapshot.review_run_id == review_run_id,
            MarketReviewScopeSnapshot.scope_type == scope_type,
            MarketReviewScopeSnapshot.scope_key == scope_key,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _list_scope_snapshots(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    scope_type: str,
) -> list[MarketReviewScopeSnapshot]:
    """列出指定类型的所有 scope snapshot。"""
    stmt = (
        select(MarketReviewScopeSnapshot)
        .where(
            MarketReviewScopeSnapshot.review_run_id == review_run_id,
            MarketReviewScopeSnapshot.scope_type == scope_type,
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars())


if __name__ == "__main__":
    print(f"PUBLICATION_KIND_MARKET_REVIEW = {PUBLICATION_KIND_MARKET_REVIEW}")
    print(f"REVIEW_PUBLISH_MIN_COVERAGE = {REVIEW_PUBLISH_MIN_COVERAGE}")
    print(f"REVIEW_PUBLISH_MIN_INDUSTRY_RATIO = {REVIEW_PUBLISH_MIN_INDUSTRY_RATIO}")
    print("OK: review_publication_service imports verified")
