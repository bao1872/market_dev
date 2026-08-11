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
from collections.abc import Collection
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.metric_engine import (
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    compute_all_metrics,
)
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
from app.services.first_pyramid_service import HISTORY_CONTRACT_VERSION
from app.services.review_metric_observation_service import (
    load_metric_history,
    persist_history_replay_observations,
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

# [CHANGE-20260808] Review 关键五维 metric code
_REVIEW_CORE_METRIC_CODES = ("P", "Q", "U", "C", "V")


def _derive_scope_status(
    payloads: dict[str, dict[str, Any]],
) -> str:
    """从 P/Q/U/C/V metric payload 推导真实 scope status。

    规则（与 Review status contract 对齐）：
    - 全部核心 metric ready → completed
    - 存在核心 metric unavailable（P/Q/U 关键维度缺失）→ unavailable
    - 全部 raw ready 但 normalized 不足（insufficient_history）→ insufficient_history
    - 部分 ready / 部分不足 → partial
    - 其他 → insufficient_history（冷启动兜底）
    """
    statuses = [
        payloads.get(code, {}).get("status")
        for code in _REVIEW_CORE_METRIC_CODES
    ]
    statuses = [s for s in statuses if s is not None]
    if not statuses:
        return "insufficient_history"
    if all(s == STATUS_READY for s in statuses):
        return "completed"
    if any(s == STATUS_UNAVAILABLE for s in statuses):
        return "unavailable"
    if all(s == STATUS_INSUFFICIENT_HISTORY for s in statuses):
        return "insufficient_history"
    if all(s in (STATUS_READY, STATUS_INSUFFICIENT_HISTORY) for s in statuses):
        return "insufficient_history" if all(
            s == STATUS_INSUFFICIENT_HISTORY for s in statuses
        ) else "partial"
    return "partial"


def _collect_canonical_source_run(
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """[CHANGE-20260808] 收集所有 participating current facts 的 canonical source history run。

    返回 {"status": "one"|"mixed"|"missing", "run_id": uuid.UUID | None}。
    - one：所有 fact 来自同一 canonical run → 返回 run_id
    - mixed：多个不同 run → fail closed（HISTORY_SOURCE_RUN_MIXED）
    - missing：无任何 fact 带 source_history_run_id → fail closed
    """
    source_run_ids = {
        fact.get("_history_source_run_id")
        for fact in facts
        if fact.get("_history_source_run_id") is not None
    }
    if not source_run_ids:
        return {"status": "missing", "run_id": None}
    if len(source_run_ids) > 1:
        return {"status": "mixed", "run_id": None}
    return {"status": "one", "run_id": uuid.UUID(next(iter(source_run_ids)))}


# bootstrap 可产出的全部 scope_type（用于 scope selector 的 fail-fast 校验）
KNOWN_BOOTSTRAP_SCOPE_TYPES: frozenset[str] = frozenset(
    {
        "market",
        "major_index",
        "style",
        "concept",
        "industry_l1",
        "industry_l2",
        "industry_l3",
    }
)


def _normalize_scope_types(
    scope_types: Collection[str] | None,
) -> frozenset[str] | None:
    """校验并归一化 scope selector。

    ``None`` → ``None``（保持既有全 scope 默认行为）。
    未知 scope_type → ValueError（fail-fast，不静默返回空集）。
    """
    if scope_types is None:
        return None
    selected = frozenset(scope_types)
    if not selected:
        raise ValueError("scope_types must not be empty; pass None for all scopes")
    unknown = sorted(selected - KNOWN_BOOTSTRAP_SCOPE_TYPES)
    if unknown:
        raise ValueError(f"unknown bootstrap scope_types: {unknown}")
    return selected


CANONICAL_HISTORY_RUN_SCOPE = "all_a_share"

# canonical readiness 判定中视为「未终结」的 run item 状态
_NON_TERMINAL_ITEM_STATUSES = ("pending", "running")


async def validate_canonical_history_run_readiness(
    session: AsyncSession,
    run_id: uuid.UUID,
    required_history_contract_version: str,
    required_trade_date: date | None = None,
) -> dict[str, Any]:
    """CANONICAL_HISTORY_RUN_READY predicate。

    [CHANGE-20260809] Phase 4B.1：把 Stage B 的 canonical 判定从
    ``run.status == 'succeeded'`` 改为显式 consumer-eligibility contract。

    背景：``_derive_run_final_status`` 只在 ``skipped == 0`` 时返回 succeeded，
    因此任何存在合法 skip（历史不足 / 无日线数据）的 Stage A run 永久是 ``partial``，
    却仍然可能是完全正确的 canonical source。

    ``HistoryRun.status`` 表达 **execution outcome**；
    canonical readiness 表达 **consumption eligibility**。两者是不同概念，
    因此本函数不修改 ``_derive_run_final_status``，只定义消费侧判定。

    predicate 要求（全部满足才 ready）：

    A. run exists 且 ``scope == 'all_a_share'``
    B. ``metadata_json.history_contract_version == required``
    C. terminal：无 pending / running run item
    D. ``failed_count == 0`` 且无 failed run item
    E. count reconciliation：``expected == succeeded + skipped``
    F. ``succeeded_count > 0``
    G. SUCCESS_SET == CANONICAL_STATE_SET：每个 succeeded run item 都至少有一条
       ``source_history_run_id == run.id`` 且 contract 匹配的 daily state
    H. 所有 skip reason 都属于已知 non-blocking category（UNKNOWN → reject）

    [HISTORY-CURRENT-DATE-LIFECYCLE-01 §9/§11] 新增可选 predicate：

    I. 当 ``required_trade_date`` 非 None 时，
       ``TARGET_DATE_ELIGIBLE_SET == TARGET_DATE_STATE_SET``，其中

       - ELIGIBLE = canonical SUCCESS_SET ∩ 在 required_trade_date 有 completed daily bar
       - STATE    = ``source_history_run_id == run.id`` ∧ contract 匹配
                    ∧ ``trade_date == required_trade_date``

       刻意**不使用** ``MAX(trade_date)``，也**不使用** ``target rows > 0``：
       前者无法发现部分 instrument 缺 target state，后者会让 1 行冒充全量覆盖。
       停牌/退市（target date 无 completed bar）不要求 target state，因此不会误判 not_ready。

    ``required_trade_date=None``（默认）时行为与扩展前完全一致（backward compatible）。

    任何一项不满足 → ``{status:'not_ready', reason: ...}``（fail closed）。
    """
    import json as _json

    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
    from app.services.first_pyramid_history_service import (
        ALLOWED_NON_BLOCKING_SKIP_CATEGORIES,
        classify_history_skip_reason,
    )

    def _not_ready(reason: str) -> dict[str, Any]:
        return {"status": "not_ready", "reason": reason}

    # --- A. run exists + scope ---------------------------------------------
    run = (
        await session.execute(
            select(FirstPyramidHistoryRun).where(FirstPyramidHistoryRun.id == run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        return _not_ready("history_source_run_not_found")
    if run.scope != CANONICAL_HISTORY_RUN_SCOPE:
        return _not_ready(f"history_source_run_wrong_scope:{run.scope}")

    # --- B. contract（pre-v2 / NULL contract 必须继续被拒绝）-----------------
    meta: dict[str, Any] = {}
    if isinstance(run.metadata_json, str) and run.metadata_json:
        try:
            parsed = _json.loads(run.metadata_json)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            meta = parsed
    elif isinstance(run.metadata_json, dict):
        meta = run.metadata_json
    run_contract = meta.get("history_contract_version")
    if run_contract != required_history_contract_version:
        return _not_ready(f"history_source_run_wrong_contract:{run_contract}")

    # --- C/D. terminal + no failure（以 run item 实况为准，不信 counter）------
    status_rows = (
        await session.execute(
            select(
                FirstPyramidHistoryRunItem.status,
                func.count(),
            )
            .where(FirstPyramidHistoryRunItem.history_run_id == run_id)
            .group_by(FirstPyramidHistoryRunItem.status)
        )
    ).all()
    item_counts = {str(row[0]): int(row[1]) for row in status_rows}

    for non_terminal in _NON_TERMINAL_ITEM_STATUSES:
        if item_counts.get(non_terminal, 0) > 0:
            return _not_ready(
                f"history_source_run_not_terminal:{non_terminal}="
                f"{item_counts[non_terminal]}"
            )

    failed_items = item_counts.get("failed", 0)
    if failed_items > 0 or int(run.failed_count or 0) > 0:
        return _not_ready(
            f"history_source_run_has_failures:items={failed_items},"
            f"counter={int(run.failed_count or 0)}"
        )

    # --- E. count reconciliation -------------------------------------------
    expected_count = int(run.expected_count or 0)
    succeeded_count = int(run.succeeded_count or 0)
    skipped_count = int(run.skipped_count or 0)
    if expected_count != succeeded_count + skipped_count:
        return _not_ready(
            "history_source_run_count_mismatch:"
            f"expected={expected_count},succeeded={succeeded_count},"
            f"skipped={skipped_count}"
        )

    # --- F. 至少有一个 succeeded ---------------------------------------------
    if succeeded_count <= 0:
        return _not_ready("history_source_run_no_succeeded_items")

    # --- H. skip reason 白名单（UNKNOWN → reject）----------------------------
    if skipped_count > 0:
        skip_rows = (
            await session.execute(
                select(FirstPyramidHistoryRunItem.last_error)
                .where(FirstPyramidHistoryRunItem.history_run_id == run_id)
                .where(FirstPyramidHistoryRunItem.status == "skipped")
            )
        ).all()
        unknown_reasons: set[str] = set()
        for row in skip_rows:
            category = classify_history_skip_reason(row[0])
            if category not in ALLOWED_NON_BLOCKING_SKIP_CATEGORIES:
                unknown_reasons.add((row[0] or "").strip()[:80] or "<empty>")
        if unknown_reasons:
            sample = ",".join(sorted(unknown_reasons)[:3])
            return _not_ready(
                f"history_source_run_unknown_skip_reason:{sample}"
            )

    # --- G. SUCCESS_SET == CANONICAL_STATE_SET ------------------------------
    success_instruments = select(
        FirstPyramidHistoryRunItem.instrument_id
    ).where(
        FirstPyramidHistoryRunItem.history_run_id == run_id,
        FirstPyramidHistoryRunItem.status == "succeeded",
    )
    canonical_instruments = select(
        FirstPyramidHistoryDailyState.instrument_id
    ).where(
        FirstPyramidHistoryDailyState.source_history_run_id == run_id,
        FirstPyramidHistoryDailyState.history_contract_version
        == required_history_contract_version,
    )
    missing_state_count = (
        await session.execute(
            select(func.count()).select_from(
                success_instruments.except_(canonical_instruments).subquery()
            )
        )
    ).scalar_one()
    if int(missing_state_count or 0) > 0:
        return _not_ready(
            f"history_source_run_success_state_mismatch:missing={int(missing_state_count)}"
        )

    # --- I. TARGET_DATE_ELIGIBLE_SET == TARGET_DATE_STATE_SET ---------------
    # [HISTORY-CURRENT-DATE-LIFECYCLE-01 §9/§11] 只在 caller 显式要求 target date 时生效。
    target_date_eligible_count: int | None = None
    target_date_state_count: int | None = None
    if required_trade_date is not None:
        from app.models.bar import BarDaily

        # ELIGIBLE = SUCCESS_SET ∩ 在 required_trade_date 有 completed daily bar
        # （停牌/退市 instrument 当日无 bar → 不进 ELIGIBLE → 不要求 target state）
        target_eligible = (
            select(FirstPyramidHistoryRunItem.instrument_id)
            .join(
                BarDaily,
                BarDaily.instrument_id == FirstPyramidHistoryRunItem.instrument_id,
            )
            .where(
                FirstPyramidHistoryRunItem.history_run_id == run_id,
                FirstPyramidHistoryRunItem.status == "succeeded",
                BarDaily.trade_date == required_trade_date,
                BarDaily.close.is_not(None),
            )
        )
        target_state = select(FirstPyramidHistoryDailyState.instrument_id).where(
            FirstPyramidHistoryDailyState.source_history_run_id == run_id,
            FirstPyramidHistoryDailyState.history_contract_version
            == required_history_contract_version,
            FirstPyramidHistoryDailyState.trade_date == required_trade_date,
        )

        target_date_eligible_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_eligible.distinct().subquery()
                    )
                )
            ).scalar_one()
            or 0
        )
        target_date_state_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_state.distinct().subquery()
                    )
                )
            ).scalar_one()
            or 0
        )

        # 双向差集：既拒绝缺 target state，也拒绝多出不该有的 target state
        missing_target = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_eligible.except_(target_state).subquery()
                    )
                )
            ).scalar_one()
            or 0
        )
        extra_target = int(
            (
                await session.execute(
                    select(func.count()).select_from(
                        target_state.except_(target_eligible).subquery()
                    )
                )
            ).scalar_one()
            or 0
        )
        if missing_target > 0 or extra_target > 0:
            return _not_ready(
                "history_source_run_target_date_state_mismatch:"
                f"date={required_trade_date.isoformat()},"
                f"eligible={target_date_eligible_count},"
                f"state={target_date_state_count},"
                f"missing={missing_target},extra={extra_target}"
            )

    result: dict[str, Any] = {
        "status": "ok",
        "run_id": run_id,
        "expected_count": expected_count,
        "succeeded_count": succeeded_count,
        "skipped_count": skipped_count,
        "run_status": run.status,
    }
    if required_trade_date is not None:
        result["required_trade_date"] = required_trade_date.isoformat()
        result["target_date_eligible_count"] = target_date_eligible_count
        result["target_date_state_count"] = target_date_state_count
    return result


async def _validate_canonical_history_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    required_history_contract_version: str,
) -> dict[str, Any]:
    """§5 canonical HistoryRun runtime contract。

    [CHANGE-20260809] 委托给 ``validate_canonical_history_run_readiness``，
    不再使用 ``status == 'succeeded'`` 作为判定条件。
    """
    return await validate_canonical_history_run_readiness(
        session,
        run_id,
        required_history_contract_version,
    )


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

    # [CHANGE-20260808] days_back 必须是真交易日数（distinct trade_date），
    # 而非自然日。120 自然日 span 可能仅约 80 交易日，不得误当 120 个 history observations。
    # 从 canonical FP history 取 end_date 之前 days_back 个 distinct trade_date（DESC），
    # 再 reverse 成 ASC（oldest → newest）。
    history_stmt = (
        select(FirstPyramidHistoryDailyState.trade_date)
        .where(
            FirstPyramidHistoryDailyState.trade_date <= end_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
        .distinct()
        .order_by(FirstPyramidHistoryDailyState.trade_date.desc())
        .limit(days_back)
    )
    dates_desc = [row[0] for row in (await session.execute(history_stmt)).all()]
    dates = list(reversed(dates_desc))
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
    source_core_run_id: uuid.UUID | None = None,
    source_board_run_id: uuid.UUID | None = None,
    dry_run: bool = True,
    audit: dict[str, Any] | None = None,
    scope_types: Collection[str] | None = None,
) -> dict[str, Any]:
    """Bootstrap every PIT scope family for one historical date.

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        trade_date: 历史交易日
        source_core_run_id: 可审计 stock_core source identity；apply 时必需
        source_board_run_id: 可审计 board source identity；apply 时必需
        dry_run: 只计算不写入（严格零业务写入）
        audit: {"operator","reason","input_hash"}；仅 apply 时持久化到 run metadata
        scope_types: 可选 scope 过滤器。``None``（默认）= 现有全 scope 行为。
            例如 ``{"market"}`` 只处理 market scope（market-only canary）。

    Returns:
        {"trade_date": ..., "run_id": ..., "metrics": {...}, "written": bool}
    """
    scopes = await _list_bootstrap_scopes(session, trade_date, scope_types=scope_types)
    computed: list[tuple[ScopeDefinition, list[dict[str, Any]], int, int, float, str, dict[str, dict[str, Any]]]] = []
    scope_results: list[dict[str, Any]] = []
    # [CHANGE-20260808] Stage B：每 trade_date 只调一次 load_day_fact_maps，
    # 所有 scope 从 facts_by_instrument 内存筛选，不再每 scope 重复查询 400 日 bars。
    # [REVIEW-CURRENT-FACT-SOURCE-DRIFT FIX] 历史回放/bootstrap 路径无 live stock_core
    # 指针，CURRENT FP 来自 canonical history source 自身的 daily state（trade_date == T）。
    facts_by_instrument = await load_day_fact_maps(
        session, trade_date=trade_date, current_source="history_state",
    )
    if not facts_by_instrument:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "no_historical_facts",
            "scopes": [],
            "written": False,
        }

    # [CHANGE-20260808] §4/§5：load_day_fact_maps 后立即确定 canonical source run
    # 并校验 HistoryRun readiness，然后才进入 scope loop / load_metric_history。
    # historical normalization 从第一步就知道自己属于哪一个 canonical HistoryRun。
    canonical_collect = _collect_canonical_source_run(list(facts_by_instrument.values()))
    if canonical_collect["status"] == "missing":
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "history_source_run_missing",
            "scopes": [],
            "written": False,
        }
    if canonical_collect["status"] == "mixed":
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "failed",
            "reason": "history_source_run_mixed",
            "scopes": [],
            "written": False,
        }
    canonical_source_run_id = canonical_collect["run_id"]
    history_contract_version = HISTORY_CONTRACT_VERSION
    readiness = await _validate_canonical_history_run(
        session, canonical_source_run_id, history_contract_version,
    )
    if readiness["status"] != "ok":
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": readiness["reason"],
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
            required_history_contract_version=HISTORY_CONTRACT_VERSION,
            required_taxonomy_compatibility_key=scope.taxonomy_compatibility_key,
            required_source_history_run_id=canonical_source_run_id,
        )
        payloads = compute_all_metrics(
            flat_list, ready_count=ready_count,
            history_maps=history_maps,
            prev_values=prev_values,
            prev5d_values=prev5d_values,
            registry=DEFAULT_REGISTRY,
        )
        coverage = ready_count / len(instrument_ids)
        status = _derive_scope_status(payloads)
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
    if not computed:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "no_pit_scope_facts",
            "scopes": scope_results,
            "written": False,
        }

    # [CHANGE-20260808] Historical baseline apply（M2 dual lineage）：
    # canonical_source_run_id 已在 scope loop 前确定并校验（§4/§5），此处直接使用。
    # 不再伪造 stock_core/board publication 或 MarketReviewRun。
    for scope, flat_list, _eligible_count, _ready_count, _coverage, _status, payloads in computed:
        # [CHANGE-20260808] HISTORY_REPLAY observation（review_run_id=NULL，
        # source_history_run_id=canonical run，history_contract_version=required）。
        await persist_history_replay_observations(
            session,
            source_history_run_id=canonical_source_run_id,
            history_contract_version=history_contract_version,
            taxonomy_compatibility_key=scope.taxonomy_compatibility_key,
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
        "run_id": str(canonical_source_run_id),
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
    scope_types: Collection[str] | None = None,
) -> list[ScopeDefinition]:
    """列出该 trade_date 需要 bootstrap 的所有 scope。

    Args:
        scope_types: 可选 scope 过滤器。``None``（默认）= 现有全 scope 行为，
            不改变生产默认路径。传入集合时只返回匹配的 scope_type，
            未知 scope_type 直接 fail-fast（ValueError），避免静默空结果。
    """
    selected = _normalize_scope_types(scope_types)

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
    if selected is not None:
        scopes = [scope for scope in scopes if scope.scope_type in selected]
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
