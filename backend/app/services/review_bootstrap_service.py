"""Point-in-time Review history bootstrap.

Historical facts come from canonical first-pyramid daily state, daily bars, and
PIT scope memberships. Missing historical membership is recorded as
``bootstrap_unavailable`` and never falls back to today's population. Dry-run is
strictly read-only. Applied runs materialize observations for the production
Review algorithm but never publish a Review pointer.

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_bootstrap_service
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.metric_engine import compute_all_metrics
from app.domain.review.metric_registry import DEFAULT_REGISTRY
from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.models.board_taxonomy import BoardDefinitionVersion
from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
from app.models.market_board import MarketBoard
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
)
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
from app.services.board_membership_service import list_universe_definitions_at
from app.services.calendar_service import get_most_recent_trading_day_async
from app.services.review_metric_observation_service import (
    load_metric_history,
    persist_metric_observations,
)
from app.services.review_scope_service import (
    DEFAULT_BASELINE_WINDOW,
    ScopeDefinition,
    ScopeSnapshotError,
    load_day_fact_maps,
    resolve_scope_members,
)
from app.utils.long_task_budget import (
    LongTaskBudgetState,
    LongTaskStopReason,
    current_rss_mb,
)

logger = logging.getLogger("review_bootstrap_service")

# Bootstrap observations must be comparable with the production algorithm.
BOOTSTRAP_ALGORITHM_VERSION = REVIEW_ALGORITHM_VERSION
BOOTSTRAP_FILTER_VERSION = "bootstrap"

# 默认回填天数（PRD §0：默认 120 日，最低 60 日）
DEFAULT_BOOTSTRAP_DAYS = 120
MIN_BOOTSTRAP_DAYS = 60

# scope 执行结果四类计数：成功 / 跳过 / 不可用 / 失败
SCOPE_COUNT_KEYS = ("succeeded", "skipped", "unavailable", "failed")

# ---------------------------------------------------------------------------
# 内存预算（[FIX-20260802] 60 日全 scope dry-run 曾在 3.4GB RSS 被 OOM Killer 杀死）
#
# 根因有二：
#   1. 逐日结果 ``results`` 保留每个 scope 的完整明细，
#      120 日 × ~400 scope 的明细在进程内线性累积；
#   2. 全程复用同一个 AsyncSession，ORM identity map 持有每日加载的
#      成员事实对象，直到整批结束才释放。
#
# 修复策略（不靠扩内存掩盖）：
#   - 按 trade_date 分片处理，每片结束 expunge_all() + 清引用，主动释放 identity map；
#   - 逐日只保留聚合后的紧凑摘要，不保留全部 scope 明细（可通过 detail_limit 控制）；
#   - 每片采样 RSS 并记录高水位；超过预算时安全停止并如实返回 partial 状态，
#     绝不静默截断也不假装成功。
# ---------------------------------------------------------------------------

# 每个分片处理的交易日数量（分片越小峰值内存越低）
DEFAULT_BOOTSTRAP_CHUNK_DAYS = 5

# 单进程 RSS 软预算（MB）。超过即安全停止，返回 status=memory_budget_exceeded。
DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB = 1536

# 返回体中保留完整 scope 明细的最大天数（其余日期只保留聚合摘要）
DEFAULT_BOOTSTRAP_DETAIL_LIMIT = 5


def _current_rss_mb() -> float | None:
    """读取当前进程 RSS（MB）。委托共享工具 ``long_task_budget.current_rss_mb``（DS-107）。

    保留本薄封装以最小化对既有调用点的改动，行为等价；新增长任务直接使用共享工具。
    """
    return current_rss_mb()


def _compact_day_result(result: dict[str, Any], *, keep_detail: bool) -> dict[str, Any]:
    """把逐日结果压缩为紧凑摘要，避免 scope 明细在长批次中线性累积。

    keep_detail=True 时保留完整 scopes 明细（仅用于最前若干天，便于人工核对）；
    否则只保留四类计数与原因码汇总——这已足够判断"哪些 scope 没算出来"。
    """
    if keep_detail:
        return result
    scopes = result.get("scopes") or []
    compact = {k: v for k, v in result.items() if k != "scopes"}
    compact["scope_counts"] = aggregate_scope_counts([result])
    compact["reason_codes"] = collect_reason_codes([result])
    compact["scope_total"] = len(scopes)
    return compact

# scope status → 四类计数归类
_SCOPE_STATUS_BUCKET = {
    "completed": "succeeded",
    "insufficient_history": "succeeded",
    "skipped": "skipped",
    "bootstrap_unavailable": "unavailable",
    "failed": "failed",
}


def compute_input_hash(
    *,
    end_date: date,
    days_back: int,
    algorithm_version: str,
) -> str:
    """计算 bootstrap 输入指纹，用于审计与重复执行识别。

    仅包含决定计算范围的输入（不含 operator/reason 等审计元数据），
    使同一输入范围的多次执行拥有相同 input_hash。
    """
    payload = json.dumps(
        {
            "end_date": end_date.isoformat(),
            "days_back": days_back,
            "algorithm_version": algorithm_version,
            "filter_version": BOOTSTRAP_FILTER_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bucket_scope_status(status: str | None) -> str:
    """把 scope status 归类到四类计数之一；未知状态计为 failed。"""
    if not status:
        return "failed"
    return _SCOPE_STATUS_BUCKET.get(status, "failed")


def aggregate_scope_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """按 scope 聚合 succeeded / skipped / unavailable / failed 四类计数。

    统计粒度是 (trade_date, scope_type, scope_key) 逐条 scope 结果，
    而不是日期数，便于判断"哪些 scope 没算出来"。
    """
    counter: Counter[str] = Counter()
    for day in results:
        day_status = day.get("status")
        for scope in day.get("scopes") or []:
            # 整日不可用时，该日全部 scope 归为 unavailable
            if day_status == "bootstrap_unavailable":
                counter["unavailable"] += 1
                continue
            counter[_bucket_scope_status(scope.get("status"))] += 1
    return {key: counter.get(key, 0) for key in SCOPE_COUNT_KEYS}


def collect_reason_codes(results: list[dict[str, Any]]) -> dict[str, int]:
    """汇总不可用/失败原因码，便于快速定位阻塞点。"""
    counter: Counter[str] = Counter()
    for day in results:
        if day.get("status") == "bootstrap_unavailable" and day.get("reason"):
            counter[str(day["reason"])] += 1
        for scope in day.get("scopes") or []:
            reason = scope.get("reason")
            if reason:
                counter[str(reason)] += 1
    return dict(counter)


async def resolve_bootstrap_end_date(
    session: AsyncSession,
    *,
    end_date: date | None = None,
) -> date:
    """解析 bootstrap 截止日期。

    end_date 为空时解析为**最近一个完整 A 股交易日**（trading_calendar 表），
    不得直接使用自然日 today —— 周末或节假日直接用 today 会让回填窗口
    错位并把非交易日计入 days_back。

    trading_calendar 无记录时降级为传入参考日，并记录 warning。
    """
    if end_date is not None:
        return end_date
    today = date.today()
    resolved = await get_most_recent_trading_day_async(session, today)
    if resolved is None:
        logger.warning(
            "[Bootstrap] trading_calendar 无可用交易日记录，降级使用自然日 %s",
            today.isoformat(),
        )
        return today
    return resolved


# =============================================================================
# 公开 API
# =============================================================================


async def list_bootstrap_eligible_dates(
    session: AsyncSession,
    *,
    end_date: date | None = None,
    days_back: int = DEFAULT_BOOTSTRAP_DAYS,
) -> list[tuple[date, uuid.UUID | None]]:
    """List canonical FP history dates and optional audit source run identities.

    Args:
        session: 异步 DB 会话
        end_date: 截止日期（None=最近已完成交易日）
        days_back: 回溯天数（默认 120）

    Returns:
        ``[(trade_date, stock_core_run_id | None), ...]`` 按日期升序（oldest → newest）。
        日期来源始终是 FP history；缺少 source identity 不删除该日期。
        升序保证 bootstrap 按时间正序处理，normalized 基线不读取未来 observation。
    """
    if end_date is None:
        end_date = date.today()

    start_date = end_date - timedelta(days=days_back)

    history_stmt = (
        select(FirstPyramidHistoryDailyState.trade_date)
        .where(
            FirstPyramidHistoryDailyState.trade_date >= start_date,
            FirstPyramidHistoryDailyState.trade_date <= end_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
        .distinct()
        .order_by(FirstPyramidHistoryDailyState.trade_date.asc())
    )
    dates = [row[0] for row in (await session.execute(history_stmt)).all()]
    if not dates:
        return []
    source_stmt = select(
        FactorPublication.trade_date, FactorPublication.data_run_id,
    ).where(
        FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
        FactorPublication.trade_date.in_(dates),
    )
    sources = {row[0]: row[1] for row in (await session.execute(source_stmt)).all()}
    return [(item, sources.get(item)) for item in dates]


async def bootstrap_single_date(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID | None,
    source_board_run_id: uuid.UUID | None = None,
    dry_run: bool = True,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bootstrap every PIT scope family for one historical date.

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        trade_date: 历史交易日
        source_core_run_id: 可审计 stock_core source identity；apply 时必需
        source_board_run_id: 可审计 board source identity；apply 时必需
        dry_run: 只计算不写入（严格零业务写入）
        audit: {"operator","reason","input_hash"}；仅 apply 时持久化到 run metadata

    Returns:
        {"trade_date": ..., "run_id": ..., "metrics": {...}, "written": bool}
    """
    scopes = await _list_bootstrap_scopes(session, trade_date)
    computed: list[tuple[ScopeDefinition, list[dict[str, Any]], int, int, float, str, dict[str, dict[str, Any]]]] = []
    scope_results: list[dict[str, Any]] = []
    # [CHANGE-20260808] Stage B：每 trade_date 只调一次 load_day_fact_maps，
    # 所有 scope 从 facts_by_instrument 内存筛选，不再每 scope 重复查询 400 日 bars。
    facts_by_instrument = await load_day_fact_maps(session, trade_date=trade_date)
    if not facts_by_instrument:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "no_historical_facts",
            "scopes": [],
            "written": False,
        }
    for scope in scopes:
        try:
            if scope.scope_type == "market":
                instrument_ids = list(facts_by_instrument.keys())
            else:
                instrument_ids, _ = await resolve_scope_members(
                    session, scope.scope_type, scope.scope_key, trade_date=trade_date,
                )
        except ScopeSnapshotError as exc:
            scope_results.append(_unavailable_scope(scope, str(exc)))
            continue
        if not instrument_ids:
            scope_results.append(_unavailable_scope(scope, "pit_membership_empty"))
            continue
        # 从内存 fact map 筛选，禁止再次读取历史 bars
        flat_list = [
            facts_by_instrument[iid]
            for iid in instrument_ids
            if iid in facts_by_instrument
        ]
        if not flat_list:
            scope_results.append(_unavailable_scope(scope, "historical_member_facts_missing"))
            continue
        ready_count = sum(
            1 for fact in flat_list if fact.get("fp_trend_direction") is not None
        )
        # [CHANGE-20260808] Chronological：载入当日之前已 persist 的观测（严格 < target_date），
        # 传 history_maps/prev/prev5d，normalized baseline 才能真正形成。
        history_maps, prev_values, prev5d_values = await load_metric_history(
            session,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            trade_date=trade_date,
            algorithm_version=BOOTSTRAP_ALGORITHM_VERSION,
            baseline_window=DEFAULT_BASELINE_WINDOW,
        )
        payloads = compute_all_metrics(
            flat_list, ready_count=ready_count,
            history_maps=history_maps,
            prev_values=prev_values,
            prev5d_values=prev5d_values,
            registry=DEFAULT_REGISTRY,
        )
        coverage = ready_count / len(instrument_ids)
        status = "insufficient_history"
        computed.append(
            (scope, flat_list, len(instrument_ids), ready_count, coverage, status, payloads),
        )
        scope_results.append({
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "status": status,
            "eligible_count": len(instrument_ids),
            "ready_count": ready_count,
            "coverage": coverage,
        })

    if dry_run:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "dry_run",
            "scopes": scope_results,
            "written": False,
        }
    if source_core_run_id is None or source_board_run_id is None:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "source_run_identity_missing",
            "scopes": scope_results,
            "written": False,
        }
    if not computed:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "no_pit_scope_facts",
            "scopes": scope_results,
            "written": False,
        }

    run_id = await _upsert_bootstrap_run(
        session,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        source_board_run_id=source_board_run_id,
        expected_scope_count=len(scopes),
        succeeded_scope_count=len(computed),
        failed_scope_count=len(scopes) - len(computed),
        scope_results=scope_results,
        audit=audit,
    )
    for scope, flat_list, eligible_count, ready_count, coverage, status, payloads in computed:
        await _upsert_bootstrap_scope_snapshot(
            session,
            review_run_id=run_id,
            trade_date=trade_date,
            scope=scope,
            eligible_count=eligible_count,
            ready_count=ready_count,
            coverage_ratio=coverage,
            status=status,
            payloads=payloads,
        )
        await persist_metric_observations(
            session,
            review_run_id=run_id,
            trade_date=trade_date,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            membership_version=scope.membership_version,
            algorithm_version=BOOTSTRAP_ALGORITHM_VERSION,
            flat_list=flat_list,
            payloads=payloads,
        )

    return {
        "trade_date": trade_date.isoformat(),
        "run_id": str(run_id),
        "status": "completed",
        "scopes": scope_results,
        "written": True,
    }


async def bootstrap_history(
    session: AsyncSession,
    *,
    end_date: date | None = None,
    days_back: int = DEFAULT_BOOTSTRAP_DAYS,
    dry_run: bool = True,
    algorithm_version: str | None = None,
    operator: str | None = None,
    reason: str | None = None,
    chunk_days: int = DEFAULT_BOOTSTRAP_CHUNK_DAYS,
    memory_budget_mb: int = DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB,
    detail_limit: int = DEFAULT_BOOTSTRAP_DETAIL_LIMIT,
) -> dict[str, Any]:
    """批量执行 canonical PIT history bootstrap（分片执行，内存有上限）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        end_date: 截止日期（None=最近一个完整 A 股交易日，非自然日 today）
        days_back: 回溯天数（默认 120，最低 60）
        dry_run: 只计算不写入（严格零业务写入）
        algorithm_version: 显式算法版本（None=当前 REVIEW_ALGORITHM_VERSION）
        operator: 执行人标识（审计用；仅 apply 时持久化）
        reason: 执行原因（审计用；仅 apply 时持久化）
        chunk_days: 每分片处理的交易日数（默认 5）；分片结束释放 ORM identity map
        memory_budget_mb: RSS 软预算（默认 1536MB）；超出后安全停止并返回 partial
        detail_limit: 返回体保留完整 scope 明细的最大天数（其余只保留聚合摘要）

    Returns:
        {
            "end_date", "days_back", "dry_run", "algorithm_version",
            "eligible_dates", "processed", "skipped", "written",
            "scope_counts": {"succeeded","skipped","unavailable","failed"},
            "reason_codes": {...},
            "input_hash": str,
            "operator", "reason",
            "results": [...],
            "peak_rss_mb": float | None,
            "chunks": int,
        }

    Note:
        - dry_run=True 时不创建 run、不写 metadata_json、不写 observations、
          不切 pointer；operator/reason/input_hash 只出现在返回值与日志中。
        - 并发固定为 1：分片之间串行，绝不并行放大峰值内存。
        - 因预算停止时 status=``memory_budget_exceeded``，
          已处理日期如实计入 processed，未处理日期不伪装为成功。
    """
    if days_back < MIN_BOOTSTRAP_DAYS:
        logger.warning(
            "[Bootstrap] days_back=%d < 最低值 %d，可能无法生成足够历史",
            days_back, MIN_BOOTSTRAP_DAYS,
        )
    if chunk_days < 1:
        raise ValueError(f"chunk_days 必须 >= 1，收到 {chunk_days}")
    if memory_budget_mb < 128:
        raise ValueError(f"memory_budget_mb 必须 >= 128，收到 {memory_budget_mb}")

    resolved_algorithm_version = algorithm_version or BOOTSTRAP_ALGORITHM_VERSION
    if resolved_algorithm_version != BOOTSTRAP_ALGORITHM_VERSION:
        raise ValueError(
            f"algorithm_version 不匹配当前 Review 算法版本: "
            f"传入 {resolved_algorithm_version}，当前 {BOOTSTRAP_ALGORITHM_VERSION}",
        )

    # end_date 为空时必须解析为最近完整交易日，不得直接用自然日 today
    resolved_end_date = await resolve_bootstrap_end_date(session, end_date=end_date)
    input_hash = compute_input_hash(
        end_date=resolved_end_date,
        days_back=days_back,
        algorithm_version=resolved_algorithm_version,
    )
    audit = {
        "operator": operator,
        "reason": reason,
        "input_hash": input_hash,
    }

    logger.info(
        "[Bootstrap] 开始: end_date=%s days_back=%d dry_run=%s "
        "algorithm_version=%s operator=%s input_hash=%s",
        resolved_end_date.isoformat(), days_back, dry_run,
        resolved_algorithm_version, operator, input_hash,
    )

    # 1. 列出可 bootstrap 的日期
    eligible = await list_bootstrap_eligible_dates(
        session, end_date=resolved_end_date, days_back=days_back,
    )

    if not eligible:
        logger.warning(
            "[Bootstrap] 无 canonical FP history: end_date=%s days_back=%d",
            resolved_end_date.isoformat(), days_back,
        )
        return {
            "end_date": resolved_end_date.isoformat(),
            "days_back": days_back,
            "dry_run": dry_run,
            "algorithm_version": resolved_algorithm_version,
            "eligible_dates": 0,
            "processed": 0,
            "skipped": 0,
            "written": 0,
            "scope_counts": dict.fromkeys(SCOPE_COUNT_KEYS, 0),
            "reason_codes": {},
            "results": [],
            "status": "no_canonical_fp_history",
            "peak_rss_mb": _current_rss_mb(),
            "chunks": 0,
            **audit,
        }

    # 2. 分片逐日执行 bootstrap（并发固定 1，分片间释放 ORM identity map）
    results: list[dict[str, Any]] = []
    processed = 0
    skipped = 0
    written = 0
    chunks = 0
    stopped_reason: str | None = None

    # 全批次聚合计数：增量累加，不依赖保留全部明细
    total_scope_counts: Counter[str] = Counter()
    total_reason_codes: Counter[str] = Counter()

    peak_rss = _current_rss_mb()
    start_rss = peak_rss
    logger.info(
        "[Bootstrap] 分片配置: chunk_days=%d memory_budget_mb=%d detail_limit=%d start_rss=%s",
        chunk_days, memory_budget_mb, detail_limit,
        f"{start_rss:.1f}MB" if start_rss is not None else "n/a",
    )

    for chunk_start in range(0, len(eligible), chunk_days):
        chunk = eligible[chunk_start:chunk_start + chunk_days]
        chunks += 1

        for trade_date, core_run_id in chunk:
            board_run_id = await _try_resolve_board_run_id(session, trade_date)

            result = await bootstrap_single_date(
                session,
                trade_date=trade_date,
                source_core_run_id=core_run_id,
                source_board_run_id=board_run_id,
                dry_run=dry_run,
                audit=None if dry_run else audit,
            )

            # 增量聚合后立即压缩，避免明细线性累积
            total_scope_counts.update(aggregate_scope_counts([result]))
            total_reason_codes.update(collect_reason_codes([result]))
            results.append(
                _compact_day_result(result, keep_detail=len(results) < detail_limit),
            )

            processed += 1
            if result.get("written"):
                written += 1
            elif result.get("status") == "skipped":
                skipped += 1

        # 分片收尾：释放 ORM identity map，防止跨日对象长期驻留
        #   dry_run 时 expunge_all 前先 rollback，确保零业务写入语义不被破坏
        if dry_run:
            await session.rollback()
        session.expunge_all()

        rss = _current_rss_mb()
        if rss is not None:
            peak_rss = rss if peak_rss is None else max(peak_rss, rss)
        logger.info(
            "[Bootstrap] 分片 %d 完成: 已处理 %d/%d 日, rss=%s peak=%s",
            chunks, processed, len(eligible),
            f"{rss:.1f}MB" if rss is not None else "n/a",
            f"{peak_rss:.1f}MB" if peak_rss is not None else "n/a",
        )

        # 内存预算门禁：超出即安全停止，如实上报，绝不靠扩内存掩盖（DS-107）
        if rss is not None and rss > memory_budget_mb:
            stopped_reason = LongTaskStopReason.MEMORY_BUDGET_EXCEEDED.value
            logger.error(
                "[Bootstrap] RSS %.1fMB 超出预算 %dMB，在第 %d 分片安全停止。"
                "已处理 %d/%d 日；请减小 days_back 或 chunk_days 后分批续跑。",
                rss, memory_budget_mb, chunks, processed, len(eligible),
            )
            break

    scope_counts = {key: total_scope_counts.get(key, 0) for key in SCOPE_COUNT_KEYS}
    reason_codes = dict(total_reason_codes)

    if stopped_reason:
        status = stopped_reason
    elif dry_run or written > 0:
        status = "ok"
    else:
        status = "no_writes"

    logger.info(
        "[Bootstrap] 完成: dry_run=%s eligible=%d processed=%d written=%d "
        "chunks=%d peak_rss=%s status=%s scope_counts=%s",
        dry_run, len(eligible), processed, written, chunks,
        f"{peak_rss:.1f}MB" if peak_rss is not None else "n/a",
        status, scope_counts,
    )

    # [CHANGE-20260804 / DS-107] 用共享工具汇总资源治理状态并暴露
    # stop_reason / resume_token / progress，不改既有返回字段语义（消费端均 .get）。
    budget_state = LongTaskBudgetState(
        chunk_size=chunk_days,
        concurrency=1,
        memory_budget_mb=memory_budget_mb,
        total=len(eligible),
        processed=processed,
    )
    budget_state.peak_rss_mb = peak_rss
    if stopped_reason:
        budget_state.mark_stopped(LongTaskStopReason(stopped_reason))

    return {
        "end_date": resolved_end_date.isoformat(),
        "days_back": days_back,
        "dry_run": dry_run,
        "algorithm_version": resolved_algorithm_version,
        "eligible_dates": len(eligible),
        "processed": processed,
        "skipped": skipped,
        "written": written,
        "scope_counts": scope_counts,
        "reason_codes": reason_codes,
        "results": results,
        "status": status,
        "peak_rss_mb": round(peak_rss, 1) if peak_rss is not None else None,
        "chunks": chunks,
        # DS-107 新增字段：长任务统一资源治理状态（安全停止原因 / 断点 / 进度）
        "stop_reason": (
            LongTaskStopReason(stopped_reason).value if stopped_reason else None
        ),
        "resume_token": budget_state.make_checkpoint(),
        "progress": budget_state.progress,
        **audit,
    }


# =============================================================================
# 内部工具
# =============================================================================


async def _market_history_members(
    session: AsyncSession,
    trade_date: date,
) -> list[uuid.UUID]:
    stmt = select(FirstPyramidHistoryDailyState.instrument_id).where(
        FirstPyramidHistoryDailyState.trade_date == trade_date,
        FirstPyramidHistoryDailyState.algorithm_version
        == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
    )
    return list((await session.execute(stmt)).scalars())


async def _list_bootstrap_scopes(
    session: AsyncSession,
    trade_date: date,
) -> list[ScopeDefinition]:
    scopes = [ScopeDefinition("market", "market", "全市场", membership_version="fp-history")]
    for universe_type in ("major_index", "style"):
        definitions = await list_universe_definitions_at(
            session, trade_date, universe_type=universe_type,
        )
        scopes.extend(
            ScopeDefinition(
                definition.universe_type,
                definition.universe_key,
                definition.name,
                taxonomy_version=definition.version,
                taxonomy_compatibility_key=definition.compatibility_key,
                membership_version=definition.membership_version,
            )
            for definition in definitions
        )

    board_stmt = (
        select(BoardDefinitionVersion, MarketBoard)
        .join(MarketBoard, MarketBoard.id == BoardDefinitionVersion.board_id)
        .where(
            BoardDefinitionVersion.effective_from <= trade_date,
            or_(
                BoardDefinitionVersion.effective_to.is_(None),
                BoardDefinitionVersion.effective_to > trade_date,
            ),
            BoardDefinitionVersion.board_type.in_(("industry", "concept")),
        )
        .order_by(BoardDefinitionVersion.board_type, MarketBoard.name)
    )
    for definition, board in (await session.execute(board_stmt)).all():
        if definition.board_type == "concept":
            scope_type = "concept"
        else:
            level = definition.hierarchy_level.lower()
            if level not in {"l1", "l2", "l3"}:
                continue
            scope_type = f"industry_{level}"
        scopes.append(
            ScopeDefinition(
                scope_type,
                str(definition.board_id),
                board.name,
                parent_scope_type=(
                    "industry_l1" if definition.parent_board_id and scope_type == "industry_l2"
                    else "industry_l2" if definition.parent_board_id and scope_type == "industry_l3"
                    else None
                ),
                parent_scope_key=(
                    str(definition.parent_board_id)
                    if definition.parent_board_id is not None else None
                ),
                taxonomy_version=definition.taxonomy_version,
                taxonomy_compatibility_key=definition.taxonomy_compatibility_key,
                membership_version=definition.membership_version,
            ),
        )
    return scopes


def _unavailable_scope(scope: ScopeDefinition, reason: str) -> dict[str, Any]:
    return {
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "status": "bootstrap_unavailable",
        "reason": reason,
    }


async def _upsert_bootstrap_run(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    source_board_run_id: uuid.UUID,
    expected_scope_count: int,
    succeeded_scope_count: int,
    failed_scope_count: int,
    scope_results: list[dict[str, Any]],
    audit: dict[str, Any] | None = None,
) -> uuid.UUID:
    """创建或复用 bootstrap run（幂等）。

    bootstrap run 特征：
    - algorithm_version = BOOTSTRAP_ALGORITHM_VERSION
    - filter_version = BOOTSTRAP_FILTER_VERSION
    - metadata.bootstrap = True
    - status = partial（仅提供历史 observation，不创建正式 publication）
    """
    now = datetime.now(UTC)
    meta: dict[str, Any] = {
        "bootstrap": True,
        "bootstrap_created_at": now.isoformat(),
        "bootstrap_algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
        "bootstrap_scope_results": scope_results,
        "bootstrap_scope_counts": aggregate_scope_counts(
            [{"status": "completed", "scopes": scope_results}],
        ),
    }
    # 审计字段只在 apply 路径写入（dry-run 不会走到这里）
    if audit:
        meta["bootstrap_operator"] = audit.get("operator")
        meta["bootstrap_reason"] = audit.get("reason")
        meta["bootstrap_input_hash"] = audit.get("input_hash")
        meta["bootstrap_executed_at"] = now.isoformat()

    values = {
        "trade_date": trade_date,
        "source_core_run_id": source_core_run_id,
        "source_board_run_id": source_board_run_id,
        "algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
        "filter_version": BOOTSTRAP_FILTER_VERSION,
        "baseline_window": DEFAULT_BOOTSTRAP_DAYS,
        "status": "partial",
        "expected_scope_count": expected_scope_count,
        "succeeded_scope_count": succeeded_scope_count,
        "failed_scope_count": failed_scope_count,
        "signal_count": 0,
        "coverage_ratio": (
            succeeded_scope_count / expected_scope_count
            if expected_scope_count else 0
        ),
        "started_at": now,
        "completed_at": now,
        "metadata_json": meta,
    }
    stmt = pg_insert(MarketReviewRun).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_runs_date_core_board_algo_filter",
        set_={
            "metadata_json": stmt.excluded.metadata_json,
            "status": stmt.excluded.status,
            "expected_scope_count": stmt.excluded.expected_scope_count,
            "succeeded_scope_count": stmt.excluded.succeeded_scope_count,
            "failed_scope_count": stmt.excluded.failed_scope_count,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "completed_at": stmt.excluded.completed_at,
        },
    )
    await session.execute(stmt)
    await session.flush()

    # 读取 run_id
    stmt2 = (
        select(MarketReviewRun.id)
        .where(
            MarketReviewRun.trade_date == trade_date,
            MarketReviewRun.source_core_run_id == source_core_run_id,
            MarketReviewRun.source_board_run_id == source_board_run_id,
            MarketReviewRun.algorithm_version == BOOTSTRAP_ALGORITHM_VERSION,
            MarketReviewRun.filter_version == BOOTSTRAP_FILTER_VERSION,
        )
        .limit(1)
    )
    result = await session.execute(stmt2)
    run_id = result.scalar_one_or_none()
    if run_id is None:
        raise RuntimeError(
            f"bootstrap run upsert 后读不到 run_id: trade_date={trade_date}",
        )
    return run_id


async def _upsert_bootstrap_scope_snapshot(
    session: AsyncSession,
    *,
    review_run_id: uuid.UUID,
    trade_date: date,
    scope: ScopeDefinition,
    eligible_count: int,
    ready_count: int,
    coverage_ratio: float,
    status: str,
    payloads: dict[str, dict[str, Any]],
) -> None:
    """upsert bootstrap scope snapshot（幂等）。"""
    values = {
        "review_run_id": review_run_id,
        "trade_date": trade_date,
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "scope_name": scope.scope_name,
        "eligible_count": eligible_count,
        "ready_count": ready_count,
        "coverage_ratio": coverage_ratio,
        "status": status,
        "p_payload": payloads.get("P"),
        "q_payload": payloads.get("Q"),
        "u_payload": payloads.get("U"),
        "c_payload": payloads.get("C"),
        "v_payload": payloads.get("V"),
        "data_quality_json": {
            "bootstrap": True,
            "eligible_count": eligible_count,
            "ready_count": ready_count,
        },
    }
    stmt = pg_insert(MarketReviewScopeSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_scope_snapshots_run_scope",
        set_={
            "trade_date": stmt.excluded.trade_date,
            "eligible_count": stmt.excluded.eligible_count,
            "ready_count": stmt.excluded.ready_count,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "status": stmt.excluded.status,
            "p_payload": stmt.excluded.p_payload,
            "q_payload": stmt.excluded.q_payload,
            "u_payload": stmt.excluded.u_payload,
            "c_payload": stmt.excluded.c_payload,
            "v_payload": stmt.excluded.v_payload,
            "data_quality_json": stmt.excluded.data_quality_json,
        },
    )
    await session.execute(stmt)
    await session.flush()


async def _try_resolve_board_run_id(
    session: AsyncSession,
    trade_date: date,
) -> uuid.UUID | None:
    """尝试解析 board run_id（不存在返回 None）。"""
    from app.models.factor_publication import PUBLICATION_KIND_MARKET_AGGREGATION

    stmt = (
        select(FactorPublication.data_run_id)
        .where(
            FactorPublication.trade_date == trade_date,
            FactorPublication.publication_kind == PUBLICATION_KIND_MARKET_AGGREGATION,
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


if __name__ == "__main__":
    print(f"BOOTSTRAP_ALGORITHM_VERSION = {BOOTSTRAP_ALGORITHM_VERSION}")
    print(f"DEFAULT_BOOTSTRAP_DAYS = {DEFAULT_BOOTSTRAP_DAYS}")
    print(f"MIN_BOOTSTRAP_DAYS = {MIN_BOOTSTRAP_DAYS}")
    print("OK: review_bootstrap_service imports verified")
