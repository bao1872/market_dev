"""BoardFactsService - 行业/概念事实（pywencai）领域 run 生产链。

[PRD V2.1 §5 / next.md EPIC-02]
一次 pywencai 行业/概念事实抓取 + 规范化 + 门禁 + PIT 发布 = 一个 BoardFactsRun。

设计要点：
1. 与 SchedulerJobRun 分离，BoardFactsRun 是独立领域产品 run（Commit 1 新增表）。
2. 状态机（PRD §5.2 E02-T13）：
        queued → fetching → normalizing → validating → persisting → published
        queued → reused_previous（失败复用）
        任意 → failed / cancelled / interrupted
3. 失败复用：当 trade_date 前 BOARD_MAX_REUSE_TRADING_DAYS 个交易日内已有
   published 的 board facts run，则复用（status=reused_previous + readiness=ready_reused），
   不重新调用 pywencai。
4. historical_replay 模式：禁止调用 pywencai，只消费已存在的 PIT publication
   （BoardDefinitionVersion/BoardMembershipHistory），回填历史日期时不允许写过去。
5. 原子发布：小事务 upsert factor_publications（board_facts kind），
   失败只重试指针，不重算数据。

复用链路基于现有 board_sync_service.sync_boards（含绝对/相对门禁 + PIT 半开区间写）。

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.board_facts_service
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain_status import (
    BOARD_FACTS_STATUS_FETCHING,
    BOARD_FACTS_STATUS_PERSISTING,
    BOARD_FACTS_STATUS_PUBLISHED,
    BOARD_FACTS_STATUS_REUSED_PREVIOUS,
    BOARD_FACTS_STATUS_VALIDATING,
    BOARD_MAX_REUSE_TRADING_DAYS_DEFAULT,
    ERR_BOARD_HISTORICAL_SNAPSHOT_MISSING,
    ERR_BOARD_PROVIDER_UNAVAILABLE,
    ERR_BOARD_QUALITY_GATE_FAILED,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_UNAVAILABLE,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
)
from app.models.board_facts_run import BoardFactsRun
from app.models.factor_publication import (
    PUBLICATION_KIND_BOARD_FACTS,
    SCOPE_TYPE_MARKET,
    FactorPublication,
)

logger = logging.getLogger(__name__)

# run_mode 常量
RUN_MODE_SCHEDULED_CURRENT = "scheduled_current"
RUN_MODE_MANUAL_CURRENT = "manual_current"
RUN_MODE_HISTORICAL_REPLAY = "historical_replay"

ALL_RUN_MODES = frozenset({
    RUN_MODE_SCHEDULED_CURRENT,
    RUN_MODE_MANUAL_CURRENT,
    RUN_MODE_HISTORICAL_REPLAY,
})

# 数据源
SOURCE_WENCAI = "pywencai"

# publication_kind 版本
PUBLICATION_ALGORITHM_VERSION = "board-facts-v1"


class BoardFactsServiceError(Exception):
    """BoardFactsService 错误基类。"""


class BoardFactsProviderError(BoardFactsServiceError):
    """pywencai 拉取失败（provider 不可用）。"""


class BoardFactsQualityGateError(BoardFactsServiceError):
    """质量门禁未通过。"""


class BoardFactsHistoricalReplayError(BoardFactsServiceError):
    """历史回放模式禁 pywencai。"""


class BoardReuseError(BoardFactsServiceError):
    """复用前一个 run 失败。"""


def _stable_snapshot_hash(snapshot: Any) -> str:
    """对 BoardSnapshot 计算顺序无关的稳定 hash（EPIC-02 要求）。

    - boards 按 (type, external_code) 排序
    - memberships 按 key 排序、symbol 列表排序去重
    - 用于判断同一快照幂等，不重复发布
    """
    boards = sorted(
        snapshot.boards,
        key=lambda b: (b.get("type", ""), b.get("external_code", "")),
    )
    board_material = "\n".join(
        f"{b.get('type')}|{b.get('external_code')}|{b.get('name')}"
        for b in boards
    )
    members = sorted(
        (k[0], k[1], v)
        for k, v in snapshot.memberships.items()
    )
    mem_material = "\n".join(
        f"{bt}|{code}|{','.join(sorted(set(syms)))}"
        for code, bt, syms in members
    )
    return hashlib.sha256(
        f"{board_material}\n---\n{mem_material}".encode()
    ).hexdigest()


async def _count_trading_days_between(
    db: AsyncSession,
    earlier: date,
    later: date,
) -> int:
    """统计 [earlier, later] 之间的 A 股交易日数（含两端）。"""
    from app.models.calendar import TradingCalendar

    result = await db.scalar(
        select(func.count())
        .select_from(TradingCalendar)
        .where(
            TradingCalendar.trade_date >= earlier,
            TradingCalendar.trade_date <= later,
            TradingCalendar.is_trading_day.is_(True),
            TradingCalendar.market == "A",
        )
    )
    return result or 0


async def _find_latest_published_run(db: AsyncSession, trade_date: date) -> BoardFactsRun | None:
    """查找 trade_date 之前最近一个 published 的 board facts run。"""
    result = await db.execute(
        select(BoardFactsRun)
        .where(
            BoardFactsRun.status == BOARD_FACTS_STATUS_PUBLISHED,
            BoardFactsRun.trade_date <= trade_date,
        )
        .order_by(BoardFactsRun.trade_date.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _find_publication(
    db: AsyncSession,
    trade_date: date,
) -> FactorPublication | None:
    result = await db.execute(
        select(FactorPublication)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_MARKET,
            FactorPublication.scope_key == "market",
            FactorPublication.trade_date == trade_date,
            FactorPublication.publication_kind == PUBLICATION_KIND_BOARD_FACTS,
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _publish_board_facts(
    db: AsyncSession,
    run: BoardFactsRun,
) -> FactorPublication:
    """原子发布 board facts pointer（upsert，唯一约束）。"""
    now = datetime.now(UTC)
    meta = {
        "board_facts_run_id": str(run.id),
        "snapshot_hash": run.snapshot_hash,
        "taxonomy_version": run.taxonomy_version,
        "membership_version": run.membership_version,
    }
    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=run.trade_date,
        publication_kind=PUBLICATION_KIND_BOARD_FACTS,
        algorithm_version=PUBLICATION_ALGORITHM_VERSION,
        data_run_id=run.id,
        coverage_ratio=_coverage_ratio(run),
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
    await db.execute(stmt)
    await db.flush()
    pub = await _find_publication(db, run.trade_date)
    assert pub is not None
    return pub


def _coverage_ratio(run: BoardFactsRun) -> float:
    """覆盖率：resolved / (resolved + unresolved)。"""
    total = (run.resolved_count or 0) + (run.unresolved_count or 0)
    if total == 0:
        return 0.0
    return (run.resolved_count or 0) / total


async def _reuse_previous(
    db: AsyncSession,
    run: BoardFactsRun,
    previous_run: BoardFactsRun,
    staleness: int,
) -> BoardFactsRun:
    """复用前一个 published run（不调用 pywencai，不刷新 source timestamp）。"""
    run.status = BOARD_FACTS_STATUS_REUSED_PREVIOUS
    run.readiness = READINESS_READY_REUSED
    run.reused_from_run_id = previous_run.id
    run.staleness = staleness
    run.snapshot_hash = previous_run.snapshot_hash
    run.taxonomy_version = previous_run.taxonomy_version
    run.membership_version = previous_run.membership_version
    run.raw_rows = previous_run.raw_rows
    run.resolved_count = previous_run.resolved_count
    run.unresolved_count = previous_run.unresolved_count
    run.industry_l1_count = previous_run.industry_l1_count
    run.industry_l2_count = previous_run.industry_l2_count
    run.industry_l3_count = previous_run.industry_l3_count
    run.concept_count = previous_run.concept_count
    run.membership_count = previous_run.membership_count
    run.coverage_json = previous_run.coverage_json
    run.diagnostics_json = {
        **(previous_run.diagnostics_json or {}),
        "reused_from_run_id": str(previous_run.id),
        "staleness": staleness,
    }
    run.finished_at = datetime.now(UTC)
    await db.flush()
    # 复用：pointer 指向被复用的 run（readiness=ready_reused），不新建数据
    await _ensure_pointer_points_to_reused(db, run, previous_run)
    return run


async def _ensure_pointer_points_to_reused(
    db: AsyncSession,
    run: BoardFactsRun,
    previous_run: BoardFactsRun,
) -> None:
    """复用 run 的 pointer 指向被复用 run 的 data_run_id（幂等 upsert）。"""
    now = datetime.now(UTC)
    meta = {
        "board_facts_run_id": str(run.id),
        "reused_from_run_id": str(previous_run.id),
        "staleness": run.staleness,
    }
    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=run.trade_date,
        publication_kind=PUBLICATION_KIND_BOARD_FACTS,
        algorithm_version=PUBLICATION_ALGORITHM_VERSION,
        data_run_id=previous_run.id,
        coverage_ratio=_coverage_ratio(previous_run),
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
    await db.execute(stmt)
    await db.flush()


async def run_board_facts(
    db: AsyncSession,
    trade_date: date,
    *,
    run_mode: str = RUN_MODE_SCHEDULED_CURRENT,
    max_reuse_trading_days: int = BOARD_MAX_REUSE_TRADING_DAYS_DEFAULT,
    snapshot: Any | None = None,
    instrument_resolver: Any | None = None,
) -> BoardFactsRun:
    """编排一次 BoardFactsRun 生产链。

    Args:
        db: 异步 DB 会话
        trade_date: 业务交易日
        run_mode: scheduled_current / manual_current / historical_replay
        max_reuse_trading_days: 失败复用最大陈旧交易日数
        snapshot: 可选预注入 BoardSnapshot（测试/调用方已拉取时）
        instrument_resolver: symbol→id 批量解析函数（默认走 DB instrument 表）

    Returns:
        BoardFactsRun（终态：published / reused_previous / failed）
    """
    if run_mode not in ALL_RUN_MODES:
        raise BoardFactsServiceError(f"未知 run_mode: {run_mode}")

    run = BoardFactsRun(
        trade_date=trade_date,
        run_mode=run_mode,
        source=SOURCE_WENCAI,
        status=RUN_STATUS_QUEUED,
    )
    db.add(run)
    await db.flush()
    run_id = run.id

    try:
        # 1. 失败复用判定
        previous_run = await _find_latest_published_run(db, trade_date)
        if previous_run is not None:
            staleness = await _count_trading_days_between(
                db, previous_run.trade_date, trade_date
            )
            if staleness > max_reuse_trading_days:
                logger.info(
                    "[BoardFacts] run=%s 前一个 published run=%s 陈旧 %d 交易日 > 上限 %d，不复用",
                    run_id, previous_run.id, staleness, max_reuse_trading_days,
                )
            else:
                run = await _reuse_previous(db, run, previous_run, staleness)
                logger.info(
                    "[BoardFacts] run=%s 复用前一个 published run=%s (staleness=%d)",
                    run_id, previous_run.id, staleness,
                )
                return run

        # 2. historical_replay：禁 pywencai，消费已有 PIT publication
        if run_mode == RUN_MODE_HISTORICAL_REPLAY:
            pub = await _find_publication(db, trade_date)
            if pub is None:
                run.status = RUN_STATUS_FAILED
                run.readiness = READINESS_UNAVAILABLE
                run.error_code = ERR_BOARD_HISTORICAL_SNAPSHOT_MISSING
                run.error_message = (
                    f"historical_replay 模式：trade_date={trade_date} 无已发布 board facts "
                    "PIT snapshot，禁止调用 pywencai 回填过去"
                )
                run.finished_at = datetime.now(UTC)
                await db.flush()
                return run
            run.status = BOARD_FACTS_STATUS_PUBLISHED
            run.readiness = READINESS_READY
            run.snapshot_hash = _publication_snapshot_hash(pub)
            run.finished_at = datetime.now(UTC)
            await db.flush()
            return run

        # 3. 拉取（provider）或使用注入 snapshot
        run.status = BOARD_FACTS_STATUS_FETCHING
        run.started_at = datetime.now(UTC)
        await db.flush()

        if snapshot is None:
            from app.services import wencai_board_provider

            try:
                snapshot = await wencai_board_provider.fetch_board_snapshot()
            except Exception as exc:
                run.status = RUN_STATUS_FAILED
                run.readiness = READINESS_UNAVAILABLE
                run.error_code = ERR_BOARD_PROVIDER_UNAVAILABLE
                run.error_message = f"pywencai provider 拉取失败: {exc}"
                run.finished_at = datetime.now(UTC)
                await db.flush()
                return run

        snapshot_hash = _stable_snapshot_hash(snapshot)
        run.snapshot_hash = snapshot_hash
        run.raw_rows = snapshot.raw_rows
        run.diagnostics_json = {
            "unresolved_sample": snapshot.unresolved_symbols[:50],
        }

        # 4. 规范化 + 门禁 + PIT 写入（复用 board_sync_service.sync_boards）
        run.status = BOARD_FACTS_STATUS_VALIDATING
        await db.flush()

        from app.services import board_sync_service

        try:
            sync_result = await board_sync_service.sync_boards(
                db,
                snapshot,
                instrument_resolver=instrument_resolver,
                effective_date=trade_date,
            )
        except Exception as exc:
            run.status = RUN_STATUS_FAILED
            run.readiness = READINESS_UNAVAILABLE
            run.error_code = ERR_BOARD_QUALITY_GATE_FAILED
            run.error_message = f"board facts 门禁/写入失败: {exc}"
            run.finished_at = datetime.now(UTC)
            await db.flush()
            return run

        run.status = BOARD_FACTS_STATUS_PERSISTING
        run.resolved_count = sync_result.get("resolved")
        run.unresolved_count = sync_result.get("unresolved")
        run.industry_l2_count = sync_result.get("industry_count")
        run.concept_count = sync_result.get("concept_count")
        run.membership_count = sync_result.get("membership_count")
        run.raw_rows = sync_result.get("raw_rows", run.raw_rows)
        run.coverage_json = {
            "resolved": sync_result.get("resolved", 0),
            "unresolved": sync_result.get("unresolved", 0),
            "parse_rate": sync_result.get("parse_rate"),
        }
        await db.flush()

        # 5. 原子发布
        run.status = BOARD_FACTS_STATUS_PUBLISHED
        run.readiness = READINESS_READY
        run.finished_at = datetime.now(UTC)
        await _publish_board_facts(db, run)
        await db.flush()

        logger.info(
            "[BoardFacts] run=%s published: trade_date=%s, hash=%.16s, "
            "resolved=%s, unresolved=%s",
            run_id, trade_date, snapshot_hash,
            run.resolved_count, run.unresolved_count,
        )
        return run

    except Exception as exc:
        logger.exception("[BoardFacts] run=%s 非预期失败: %s", run_id, exc)
        run.status = RUN_STATUS_FAILED
        run.readiness = READINESS_UNAVAILABLE
        if not run.error_code:
            run.error_code = "BOARD_FACTS_UNEXPECTED_ERROR"
        run.error_message = run.error_message or str(exc)
        run.finished_at = datetime.now(UTC)
        await db.flush()
        return run


def _publication_snapshot_hash(pub: FactorPublication) -> str | None:
    """从 publication metadata 提取 snapshot_hash。"""
    if not pub.metadata_json:
        return None
    try:
        meta = json.loads(pub.metadata_json)
        return meta.get("snapshot_hash")
    except Exception:
        return None


if __name__ == "__main__":
    print(f"RUN_MODE_SCHEDULED_CURRENT = {RUN_MODE_SCHEDULED_CURRENT}")
    print(f"RUN_MODE_HISTORICAL_REPLAY = {RUN_MODE_HISTORICAL_REPLAY}")
    print(f"PUBLICATION_KIND_BOARD_FACTS = {PUBLICATION_KIND_BOARD_FACTS}")
    print("OK: board_facts_service imports verified")
