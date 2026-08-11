"""复盘范围扫描服务 - Discovery scope snapshot（PRD70 V2 §6）。

[V2] 平行扫描模型（PRD70 §6.1-6.2）：
- 所有 Scope Family 在发现阶段独立平行参与 observation：
  market / major_index / style / industry_l1 / industry_l2 / industry_l3 / concept
- Industry taxonomy hierarchy 不成为 discovery eligibility gate
- Concept 不依赖 Industry 命中

[V2] Comparable peer cohort（PRD70 §6.4.1）：
- industry_l1 ↔ industry_l1
- industry_l2 ↔ industry_l2
- industry_l3 ↔ industry_l3
- concept ↔ concept
- market: 无 peer cohort（使用自身历史基线）

输入：
- 已发布 stock_core pointer（factor_publications.data_run_id）
- 已发布 board_analysis pointer（用于行业/概念范围）
- 第一金字塔历史基线（默认 120 个交易日，最低 60 日）

输出：
- MarketReviewScopeSnapshot ORM 记录（每个范围一条，含 P/Q/U/C/V payload）

幂等：
- 相同 (review_run_id, scope_type, scope_key) 不重算
- coverage_ratio = ready_count / eligible_count

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_scope_service
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.member_fact import (
    DailyBarFact,
    ReviewMemberFact,
    previous_state_to_flat,
)
from app.domain.review.metric_engine import _cross_section_percentile, compute_all_metrics
from app.domain.review.metric_registry import DEFAULT_REGISTRY
from app.models.bar import BarDaily
from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
from app.models.instrument import Instrument
from app.models.market_review import MarketReviewScopeSnapshot
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
from app.services.board_membership_service import (
    PITMembershipUnavailableError,
    resolve_board_membership_at,
    resolve_universe_membership_at,
)

logger = logging.getLogger("review_scope_service")

# 默认基线窗口（PRD §0、§7.1）
DEFAULT_BASELINE_WINDOW = 120
MIN_BASELINE_WINDOW = 60

# [CHANGE-20260808] Historical daily_state payload 契约版本（与 first_pyramid_service 一致）。
# Stage B 拒绝旧 payload 混入 replay（progressive backfill 期间防混用）。
_REVIEW_HISTORY_CONTRACT_VERSION = "review-history-v2"

# 单 scope 发布门禁（PRD §11.1）
SCOPE_PUBLISH_MIN_COVERAGE = 0.95

# [V2] 所有正式 Discovery scope 类型（平行独立参与 observation/discovery）
ALL_DISCOVERY_SCOPE_TYPES: tuple[str, ...] = (
    "market", "major_index", "style",
    "industry_l1", "industry_l2", "industry_l3",
    "concept",
)
# Legacy 兼容：第一级范围类型（V1 两级扫描模型，保留向后兼容引用）
# Legacy 兼容：V1 两级扫描模型的常量语义（保留向后兼容引用）
LEVEL1_SCOPE_TYPES: tuple[str, ...] = ("market", "major_index", "style", "industry_l1")
LEVEL2_SCOPE_TYPES: tuple[str, ...] = ("industry_l2", "industry_l3", "concept", "instrument")


class ScopeSnapshotError(Exception):
    """范围快照计算失败。"""

    pass


# [REVIEW-OPTIONAL-SCOPE-TERMINALIZATION-01 2026-08-10]
# publication contract（review_publication_service §2~4）已把
# major_index / style / industry_l1 定义为 PROGRESSIVE OPTIONAL scope：
# 其 PIT membership 不可用（blocked_external_population / bootstrap_unavailable /
# 无 PIT membership 版本）只应记为 scope-level diagnostic，
# 合法终态是 SKIPPED，而不是 RUNNING 残留或 FAILED。
#
# 该 scope_type 集合与 publication contract 的 optional 集合保持一致；
# market 不在其中——market 的任何 membership/data 不可用仍必须 FAILED，不得静默 SKIP。
# [V2] 渐进式 scope readiness：market 是唯一 hard gate；
# major_index/style/industry_l1 = progressive optional；
# industry_l2/l3/concept = parallel independent scope。
# 以上 scope 数据源不可用仅记诊断，不阻塞 Review publication。
OPTIONAL_UNAVAILABLE_SCOPE_TYPES: frozenset[str] = frozenset(
    {"major_index", "style",
     "industry_l1", "industry_l2", "industry_l3",
     "concept"},
)


class OptionalScopeUnavailableError(ScopeSnapshotError):
    """Optional scope 的 PIT membership 合法不可用（非执行异常）。

    仅用于 publication contract 中标记为 optional 的 scope_type
    （见 ``OPTIONAL_UNAVAILABLE_SCOPE_TYPES``）在 PIT membership 不可用时。

    orchestrator 捕获本异常后把 metrics run item 终态化为 ``skipped``，
    不当作执行失败。**不得**用于：
    - scope_type mismatch / hierarchy mismatch（配置或代码错误）
    - 非法 UUID scope_key（调用方错误）
    - board_not_found（数据引用错误）
    - 任何未预期 DB / 实现异常
    以上仍是正常 failure，必须以 ScopeSnapshotError 或原始异常传播。

    禁止通过字符串匹配（如 ``"blocked_external_population" in str(exc)``）
    判定该语义；调用方必须依赖本类型。
    """

    def __init__(
        self,
        *,
        reason: str,
        scope_type: str,
        scope_key: str,
        population_status: str | None = None,
        trade_date: date | None = None,
    ) -> None:
        self.reason = reason
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.population_status = population_status
        self.trade_date = trade_date
        detail = (
            f"optional_scope_unavailable: reason={reason} "
            f"scope={scope_type}/{scope_key}"
        )
        if population_status is not None:
            detail += f" population_status={population_status}"
        if trade_date is not None:
            detail += f" trade_date={trade_date}"
        super().__init__(detail)


# =============================================================================
# Scope 定义
# =============================================================================


class ScopeDefinition:
    """单个范围定义（scope_type + scope_key + scope_name + 成员来源）。

    成员来源由 service 层根据 scope_type 查询：
    - market: 全部 active A 股
    - major_index: 复用指数成分服务
    - style: 复用风格股票池定义
    - industry_l1/l2/l3: 复用行业板块成员关系
    - concept: 复用概念板块成员关系
    - instrument: 单只股票（下钻用）
    """

    def __init__(
        self,
        scope_type: str,
        scope_key: str,
        scope_name: str,
        *,
        parent_scope_type: str | None = None,
        parent_scope_key: str | None = None,
        source_board_snapshot_id: uuid.UUID | None = None,
        taxonomy_version: str | None = None,
        taxonomy_compatibility_key: str | None = None,
        membership_version: str | None = None,
    ) -> None:
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.scope_name = scope_name
        self.parent_scope_type = parent_scope_type
        self.parent_scope_key = parent_scope_key
        self.source_board_snapshot_id = source_board_snapshot_id
        self.taxonomy_version = taxonomy_version
        self.taxonomy_compatibility_key = taxonomy_compatibility_key
        self.membership_version = membership_version

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "scope_name": self.scope_name,
            "parent_scope_type": self.parent_scope_type,
            "parent_scope_key": self.parent_scope_key,
            "source_board_snapshot_id": (
                str(self.source_board_snapshot_id)
                if self.source_board_snapshot_id else None
            ),
            "taxonomy_version": self.taxonomy_version,
            "taxonomy_compatibility_key": self.taxonomy_compatibility_key,
            "membership_version": self.membership_version,
        }


# =============================================================================
# Scope snapshot upsert（幂等）
# =============================================================================


async def upsert_scope_snapshot(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    trade_date: date,
    scope: ScopeDefinition,
    *,
    eligible_count: int,
    ready_count: int,
    coverage_ratio: float,
    status: str,
    p_payload: dict[str, Any] | None,
    q_payload: dict[str, Any] | None,
    u_payload: dict[str, Any] | None,
    c_payload: dict[str, Any] | None,
    v_payload: dict[str, Any] | None,
    data_quality_json: dict[str, Any] | None = None,
) -> MarketReviewScopeSnapshot:
    """upsert scope snapshot 记录（幂等，唯一键 review_run_id+scope_type+scope_key）。"""
    values = {
        "review_run_id": review_run_id,
        "trade_date": trade_date,
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "scope_name": scope.scope_name,
        "parent_scope_type": scope.parent_scope_type,
        "parent_scope_key": scope.parent_scope_key,
        "source_board_snapshot_id": scope.source_board_snapshot_id,
        "taxonomy_version": scope.taxonomy_version,
        "taxonomy_compatibility_key": scope.taxonomy_compatibility_key,
        "membership_version": scope.membership_version,
        "eligible_count": eligible_count,
        "ready_count": ready_count,
        "coverage_ratio": Decimal(str(coverage_ratio)),
        "status": status,
        "p_payload": p_payload,
        "q_payload": q_payload,
        "u_payload": u_payload,
        "c_payload": c_payload,
        "v_payload": v_payload,
        "data_quality_json": data_quality_json,
    }

    stmt = pg_insert(MarketReviewScopeSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_scope_snapshots_run_scope",
        set_={
            "trade_date": stmt.excluded.trade_date,
            "scope_name": stmt.excluded.scope_name,
            "parent_scope_type": stmt.excluded.parent_scope_type,
            "parent_scope_key": stmt.excluded.parent_scope_key,
            "source_board_snapshot_id": stmt.excluded.source_board_snapshot_id,
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

    # 读取 upsert 后的记录
    return await get_scope_snapshot(
        session, review_run_id, scope.scope_type, scope.scope_key,
    )  # type: ignore[return-value]


async def get_scope_snapshot(
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


async def list_scope_snapshots(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    *,
    scope_type: str | None = None,
    parent_scope_type: str | None = None,
    parent_scope_key: str | None = None,
) -> list[MarketReviewScopeSnapshot]:
    """列出 review run 的 scope snapshot（可按 scope_type / parent 过滤）。"""
    stmt = select(MarketReviewScopeSnapshot).where(
        MarketReviewScopeSnapshot.review_run_id == review_run_id,
    )
    if scope_type is not None:
        stmt = stmt.where(MarketReviewScopeSnapshot.scope_type == scope_type)
    if parent_scope_type is not None:
        stmt = stmt.where(
            MarketReviewScopeSnapshot.parent_scope_type == parent_scope_type,
        )
    if parent_scope_key is not None:
        stmt = stmt.where(
            MarketReviewScopeSnapshot.parent_scope_key == parent_scope_key,
        )
    result = await session.execute(stmt)
    return list(result.scalars())


# =============================================================================
# Scope 指标计算
# =============================================================================


async def compute_scope_metrics(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    trade_date: date,
    scope: ScopeDefinition,
    flat_list: list[dict[str, Any]],
    *,
    algorithm_version: str,
    eligible_count: int | None = None,
    history_maps: dict[str, dict[str, list[float]]] | None = None,
    prev_values: dict[str, float] | None = None,
    prev5d_values: dict[str, float] | None = None,
    cross_section_values: dict[str, list[float]] | None = None,
    pyramid_v2_payload: dict[str, Any] | None = None,
) -> MarketReviewScopeSnapshot:
    """计算单个范围的 P/Q/U/C/V 并 upsert scope snapshot。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        review_run_id: 复盘 run ID
        trade_date: 业务交易日
        scope: 范围定义
        flat_list: 成员 first_pyramid_flat 列表
        algorithm_version: 生成 observation 的 Review 算法版本
        eligible_count: 范围成员总数（None=取 len(flat_list)）
        history_maps: 每个 metric 的历史序列 map（用于历史分位归一化）
        prev_values: 前一交易日各 metric 的 value（用于 delta1d）
        prev5d_values: 前 5 交易日各 metric 的 value（用于 delta5d）
        cross_section_values: 当日所有 scope 的 value 序列（用于横截面分位）
        pyramid_v2_payload: 第二金字塔维度数据（PRD §24，来自
            board_analysis_snapshots.payload["pyramid_v2"]）；
            industry/concept scope 由 orchestrator 注入，其他 scope 为 None

    Returns:
        MarketReviewScopeSnapshot ORM 对象
    """
    if eligible_count is None:
        eligible_count = len(flat_list)

    # ready_count = 有 fp_trend_direction 的成员数
    ready_count = sum(
        1 for f in flat_list if f and f.get("fp_trend_direction") is not None
    )

    coverage_ratio = (
        ready_count / eligible_count if eligible_count > 0 else 0.0
    )

    # 计算 P/Q/U/C/V
    payloads = compute_all_metrics(
        flat_list,
        ready_count=ready_count,
        history_maps=history_maps,
        prev_values=prev_values,
        prev5d_values=prev5d_values,
        cross_section_values=cross_section_values,
        registry=DEFAULT_REGISTRY,
    )

    # 状态判定（PRD §7.1：历史少于 60 日 status=insufficient_history）
    statuses = [p.get("status") for p in payloads.values()]
    if not flat_list:
        status = "unavailable"
    elif all(s == "ready" for s in statuses):
        status = "ready"
    elif any(s == "insufficient_history" for s in statuses):
        status = "insufficient_history"
    else:
        status = "partial"

    data_quality = {
        "eligible_count": eligible_count,
        "ready_count": ready_count,
        "missing_count": eligible_count - ready_count,
        "missing_reasons": _classify_missing_reasons(flat_list, eligible_count),
    }
    # [P0-7 2026-07-30] 注入 pyramid_v2 维度数据（PRD §24 D 族筛选器）
    # industry/concept scope 由 orchestrator 从 board_analysis_snapshots 读取并注入；
    # 其他 scope pyramid_v2_payload=None，不写入 data_quality_json
    if pyramid_v2_payload is not None:
        data_quality["pyramid_v2"] = pyramid_v2_payload

    snapshot = await upsert_scope_snapshot(
        session,
        review_run_id,
        trade_date,
        scope,
        eligible_count=eligible_count,
        ready_count=ready_count,
        coverage_ratio=coverage_ratio,
        status=status,
        p_payload=payloads.get("P"),
        q_payload=payloads.get("Q"),
        u_payload=payloads.get("U"),
        c_payload=payloads.get("C"),
        v_payload=payloads.get("V"),
        data_quality_json=data_quality,
    )

    from app.services.review_metric_observation_service import (
        persist_metric_observations,
    )

    await persist_metric_observations(
        session,
        review_run_id=review_run_id,
        trade_date=trade_date,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        membership_version=scope.membership_version,
        algorithm_version=algorithm_version,
        flat_list=flat_list,
        payloads=payloads,
        taxonomy_compatibility_key=scope.taxonomy_compatibility_key,
    )

    logger.info(
        "[ReviewScope] %s/%s eligible=%d ready=%d coverage=%.4f status=%s",
        scope.scope_type, scope.scope_name, eligible_count, ready_count,
        coverage_ratio, status,
    )
    return snapshot


def _scope_family(scope_type: str) -> str | None:
    """[V2] Comparable peer cohort for cross-sectional percentile.
    
    Each taxonomy level is an independent peer cohort:
    - industry_l1 ↔ industry_l1
    - industry_l2 ↔ industry_l2
    - industry_l3 ↔ industry_l3
    - concept ↔ concept
    - major_index ↔ major_index
    - style ↔ style
    - market: None (no peer cohort — uses self-historical baseline)

    Returns None for market to signal no cross-section computation.
    """
    if scope_type == "market":
        return None
    return scope_type


async def apply_cross_section_percentiles(
    session: AsyncSession,
    review_run_id: uuid.UUID,
) -> int:
    """Second pass: rank normalized values among same-day scope-family peers."""
    stmt = (
        select(MarketReviewScopeSnapshot)
        .where(MarketReviewScopeSnapshot.review_run_id == review_run_id)
        .order_by(
            MarketReviewScopeSnapshot.scope_type.asc(),
            MarketReviewScopeSnapshot.scope_key.asc(),
        )
    )
    snapshots = list((await session.execute(stmt)).scalars())
    fields = {
        "P": "p_payload",
        "Q": "q_payload",
        "U": "u_payload",
        "C": "c_payload",
        "V": "v_payload",
    }
    peers: dict[tuple[str, str], list[float]] = defaultdict(list)
    for snapshot in snapshots:
        family = _scope_family(snapshot.scope_type)
        if family is None:
            continue  # market: no peer cohort
        for code, field in fields.items():
            payload = getattr(snapshot, field)
            value = payload.get("value") if isinstance(payload, dict) else None
            if isinstance(value, (int, float)):
                peers[(family, code)].append(float(value))

    updated = 0
    for snapshot in snapshots:
        family = _scope_family(snapshot.scope_type)
        for code, field in fields.items():
            original = getattr(snapshot, field)
            if not isinstance(original, dict):
                continue
            payload = dict(original)
            value = payload.get("value")
            if family is None:
                # market: crossSectionPercentile is always None
                payload["crossSectionPercentile"] = None
            else:
                peer_values = peers.get((family, code), [])
                percentile = (
                    _cross_section_percentile(float(value), peer_values)
                    if isinstance(value, (int, float)) and peer_values
                    else None
                )
                payload["crossSectionPercentile"] = percentile
            setattr(snapshot, field, payload)
        updated += 1
    await session.flush()
    return updated


def _classify_missing_reasons(
    flat_list: list[dict[str, Any]], eligible_count: int,
) -> dict[str, int]:
    """分类成员缺失原因。"""
    reasons: dict[str, int] = {}
    ready = sum(
        1 for f in flat_list if f and f.get("fp_trend_direction") is not None
    )
    missing = eligible_count - ready
    if missing > 0:
        # 简化：所有缺失归为 SNAPSHOT_MISSING 或 FP_TREND_MISSING
        no_trend = sum(
            1 for f in flat_list
            if f and f.get("fp_trend_direction") is None
        )
        no_snapshot = eligible_count - len(flat_list)
        if no_snapshot > 0:
            reasons["SNAPSHOT_MISSING"] = no_snapshot
        if no_trend > 0:
            reasons["FP_TREND_MISSING"] = no_trend
    return reasons


# =============================================================================
# 范围查询（service 层根据 scope_type 查询成员）
# =============================================================================


def _optional_unavailable_or_failure(
    *,
    reason: str,
    scope_type: str,
    scope_key: str,
    trade_date: date,
    fallback_message: str,
    population_status: str | None = None,
) -> ScopeSnapshotError:
    """按 publication contract 决定 membership 不可用是 optional 还是 failure。

    [REVIEW-OPTIONAL-SCOPE-TERMINALIZATION-01 2026-08-10]

    - optional scope（major_index / style / industry_l1）：返回
      ``OptionalScopeUnavailableError``，orchestrator 会终态化为 skipped。
    - 其他 scope（market / industry_l2 / industry_l3 / concept / instrument）：
      返回普通 ``ScopeSnapshotError``，保持 failure 语义，不得静默 SKIP。

    返回异常对象而非抛出，由调用点 ``raise ... from exc`` 保留原始因果链。
    """
    if scope_type in OPTIONAL_UNAVAILABLE_SCOPE_TYPES:
        return OptionalScopeUnavailableError(
            reason=reason,
            scope_type=scope_type,
            scope_key=scope_key,
            population_status=population_status,
            trade_date=trade_date,
        )
    return ScopeSnapshotError(fallback_message)


async def resolve_scope_members(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    *,
    trade_date: date,
) -> tuple[list[uuid.UUID], str]:
    """Resolve members from the source valid on ``trade_date``.

    Board and configured universe scopes are point-in-time only. Historical
    requests never fall back to the latest-state membership projection.
    """
    if scope_type == "market":
        from app.models.instrument import Instrument

        stmt = select(Instrument.id).where(Instrument.status == "active")
        result = await session.execute(stmt)
        return [row[0] for row in result], "全市场"

    if scope_type == "instrument":
        try:
            inst_id = uuid.UUID(scope_key)
        except ValueError as exc:
            raise ScopeSnapshotError(
                f"instrument scope_key 非合法 UUID: {scope_key}",
            ) from exc
        return [inst_id], scope_key

    if scope_type in ("major_index", "style"):
        expected_type = "major_index" if scope_type == "major_index" else "style"
        try:
            definition, membership = await resolve_universe_membership_at(
                session, scope_key, trade_date,
            )
        except PITMembershipUnavailableError as exc:
            # [REVIEW-OPTIONAL-SCOPE-TERMINALIZATION-01] optional scope 无 PIT
            # membership 版本是合法不可用，不是执行异常。
            raise _optional_unavailable_or_failure(
                reason="pit_membership_unavailable",
                scope_type=scope_type,
                scope_key=scope_key,
                trade_date=trade_date,
                fallback_message=str(exc),
            ) from exc
        # scope_type mismatch 是配置/代码错误，仍为正常 failure，不得 SKIP。
        if definition.universe_type != expected_type:
            raise ScopeSnapshotError(
                f"scope_type mismatch: {scope_type} key={scope_key} "
                f"universe_type={definition.universe_type}",
            )
        if membership.population_status != "ready":
            raise _optional_unavailable_or_failure(
                reason="population_not_ready",
                scope_type=scope_type,
                scope_key=scope_key,
                trade_date=trade_date,
                population_status=membership.population_status,
                fallback_message=(
                    f"{membership.population_status}: {scope_type}={scope_key} "
                    f"trade_date={trade_date}"
                ),
            )
        return list(membership.instrument_ids), definition.name

    if scope_type in ("industry_l1", "industry_l2", "industry_l3", "concept"):
        from app.models.market_board import MarketBoard

        try:
            board_uuid = uuid.UUID(scope_key)
        except ValueError as exc:
            raise ScopeSnapshotError(
                f"{scope_type} scope_key 非合法 UUID: {scope_key}",
            ) from exc
        board = (
            await session.execute(
                select(MarketBoard).where(MarketBoard.id == board_uuid).limit(1),
            )
        ).scalar_one_or_none()
        if board is None:
            raise ScopeSnapshotError(f"board_not_found: {scope_key}")
        expected_board_type = _board_type_from_scope(scope_type)
        if board.type != expected_board_type:
            raise ScopeSnapshotError(
                f"scope_type mismatch: {scope_type} board_type={board.type}",
            )
        expected_level = _hierarchy_level_from_scope(scope_type)
        if expected_level is not None and board.hierarchyLevel != expected_level:
            raise ScopeSnapshotError(
                f"scope hierarchy mismatch: {scope_type} "
                f"hierarchy={board.hierarchyLevel}",
            )
        try:
            membership = await resolve_board_membership_at(
                session, board_uuid, trade_date,
            )
        except PITMembershipUnavailableError as exc:
            raise _optional_unavailable_or_failure(
                reason="pit_membership_unavailable",
                scope_type=scope_type,
                scope_key=scope_key,
                trade_date=trade_date,
                fallback_message=str(exc),
            ) from exc
        if membership.population_status != "ready":
            raise _optional_unavailable_or_failure(
                reason="population_not_ready",
                scope_type=scope_type,
                scope_key=scope_key,
                trade_date=trade_date,
                population_status=membership.population_status,
                fallback_message=(
                    f"{membership.population_status}: board={scope_key} "
                    f"trade_date={trade_date}"
                ),
            )
        return list(membership.instrument_ids), board.name

    return [], scope_key


def _board_type_from_scope(scope_type: str) -> str:
    """scope_type → market_boards.type 映射。"""
    if scope_type in ("industry_l1", "industry_l2", "industry_l3"):
        return "industry"
    if scope_type == "concept":
        return "concept"
    return scope_type


def _hierarchy_level_from_scope(scope_type: str) -> str | None:
    if scope_type == "industry_l1":
        return "L1"
    if scope_type == "industry_l2":
        return "L2"
    if scope_type == "industry_l3":
        return "L3"
    return None


# =============================================================================
# 批量获取成员 first_pyramid_flat
# =============================================================================


async def fetch_member_flat_list(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    source_core_run_id: uuid.UUID,
    *,
    trade_date: date | None = None,
) -> list[dict[str, Any]]:
    """Build typed Review member facts from one core run and PIT daily data.

    Returns:
        Metric input dictionaries. When ``trade_date`` is omitted this preserves the
        legacy identity-only behavior for non-Review callers and unit fixtures.
    """
    if not instrument_ids:
        return []
    from app.models.stock_feature_snapshot import StockFeatureSnapshot

    stmt = (
        select(
            StockFeatureSnapshot.id,
            StockFeatureSnapshot.instrument_id,
            Instrument.symbol,
            Instrument.name,
            StockFeatureSnapshot.summary_payload,
        )
        .join(Instrument, Instrument.id == StockFeatureSnapshot.instrument_id)
        .where(
            StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            StockFeatureSnapshot.source_run_id == source_core_run_id,
        )
    )
    result = await session.execute(stmt)
    source_rows: list[tuple[uuid.UUID, uuid.UUID, str, str, dict[str, Any]]] = []
    for row in result:
        snapshot_id = row[0]
        instrument_id = row[1]
        symbol = row[2]
        name = row[3]
        summary = row[4] or {}
        if not isinstance(summary, dict):
            continue
        flat = summary.get("first_pyramid_flat")
        if isinstance(flat, dict):
            source_rows.append((snapshot_id, instrument_id, symbol, name, flat))

    if trade_date is None:
        return [
            {
                **flat,
                "_instrument_id": str(instrument_id),
                "_instrument_symbol": symbol,
                "_instrument_name": name,
                "_snapshot_id": snapshot_id,
            }
            for snapshot_id, instrument_id, symbol, name, flat in source_rows
        ]

    bar_stmt = (
        select(BarDaily)
        .where(
            BarDaily.instrument_id.in_(instrument_ids),
            BarDaily.trade_date <= trade_date,
            BarDaily.trade_date >= trade_date - timedelta(days=400),
        )
        .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
    )
    bars_by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    for bar in (await session.execute(bar_stmt)).scalars():
        bars_by_instrument[bar.instrument_id].append(DailyBarFact.from_row(bar))

    previous_stmt = (
        select(FirstPyramidHistoryDailyState)
        .where(
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            FirstPyramidHistoryDailyState.trade_date < trade_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
        .distinct(FirstPyramidHistoryDailyState.instrument_id)
        .order_by(
            FirstPyramidHistoryDailyState.instrument_id.asc(),
            FirstPyramidHistoryDailyState.trade_date.desc(),
        )
    )
    previous_by_instrument: dict[uuid.UUID, dict[str, Any]] = {}
    for state in (await session.execute(previous_stmt)).scalars():
        previous_by_instrument.setdefault(state.instrument_id, state.state_payload)

    return [
        ReviewMemberFact.build(
            instrument_id=instrument_id,
            symbol=symbol,
            name=name,
            snapshot_id=snapshot_id,
            trade_date=trade_date,
            first_pyramid=flat,
            bars=bars_by_instrument[instrument_id],
            previous_state=previous_by_instrument.get(instrument_id),
        ).to_metric_input()
        for snapshot_id, instrument_id, symbol, name, flat in source_rows
    ]


async def fetch_historical_member_facts(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    *,
    trade_date: date,
) -> list[dict[str, Any]]:
    """Build PIT Review facts directly from canonical FP history and daily bars."""
    if not instrument_ids:
        return []

    identity_stmt = select(Instrument).where(Instrument.id.in_(instrument_ids))
    identities = {
        item.id: item for item in (await session.execute(identity_stmt)).scalars()
    }

    current_stmt = (
        select(FirstPyramidHistoryDailyState)
        .where(
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            FirstPyramidHistoryDailyState.trade_date == trade_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
    )
    current_by_instrument = {
        state.instrument_id: state
        for state in (await session.execute(current_stmt)).scalars()
    }
    previous_stmt = (
        select(FirstPyramidHistoryDailyState)
        .where(
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            FirstPyramidHistoryDailyState.trade_date < trade_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
        .distinct(FirstPyramidHistoryDailyState.instrument_id)
        .order_by(
            FirstPyramidHistoryDailyState.instrument_id.asc(),
            FirstPyramidHistoryDailyState.trade_date.desc(),
        )
    )
    previous_by_instrument = {
        state.instrument_id: state.state_payload
        for state in (await session.execute(previous_stmt)).scalars()
    }

    bar_stmt = (
        select(BarDaily)
        .where(
            BarDaily.instrument_id.in_(instrument_ids),
            BarDaily.trade_date <= trade_date,
            BarDaily.trade_date >= trade_date - timedelta(days=400),
        )
        .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
    )
    bars_by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    for bar in (await session.execute(bar_stmt)).scalars():
        bars_by_instrument[bar.instrument_id].append(DailyBarFact.from_row(bar))

    facts: list[dict[str, Any]] = []
    for instrument_id in instrument_ids:
        current_state = current_by_instrument.get(instrument_id)
        identity = identities.get(instrument_id)
        if current_state is None or identity is None:
            continue
        fact = ReviewMemberFact.build(
            instrument_id=instrument_id,
            symbol=identity.symbol,
            name=identity.name,
            snapshot_id=None,
            trade_date=trade_date,
            first_pyramid=previous_state_to_flat(current_state.state_payload),
            bars=bars_by_instrument[instrument_id],
            previous_state=previous_by_instrument.get(instrument_id),
        ).to_metric_input()
        fact["_history_state_id"] = str(current_state.id)
        fact["_history_input_hash"] = current_state.input_hash
        facts.append(fact)
    return facts


async def load_day_fact_maps(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID | None = None,
    instrument_ids: list[uuid.UUID] | None = None,
    required_source_history_run_id: uuid.UUID | None = None,
    required_history_contract_version: str | None = None,
    current_source: str = "stock_core",
) -> dict[uuid.UUID, dict[str, Any]]:
    """[REVIEW-CURRENT-FACT-SOURCE-DRIFT FIX] 形式 Review 的 load-once 事实加载入口。

    事实语义遵循 PRD70 / PRD31 与 fetch_member_flat_list 已落地的正确契约
    （REVIEW-V2 FINAL CLOSURE 纠正：CURRENT FP 来源必须是当日正式 stock_core 指针，
    而非 FirstPyramidHistoryDailyState(T)）：

    - **CURRENT First Pyramid（当前日 FP facts）** 来源由 ``current_source`` 决定：
        - ``"stock_core"``（默认，正式 Review）：来自当日正式 ``stock_core`` 指针，
          ``StockFeatureSnapshot WHERE source_run_id == source_core_run_id``，
          读取 ``summary_payload.first_pyramid_flat``。**绝不**从
          ``FirstPyramidHistoryDailyState WHERE trade_date == T`` 读取 CURRENT FP。
        - ``"history_state"``（历史回放/bootstrap 路径）：回放历史日期时无 live
          stock_core 指针，CURRENT FP 仍来自 ``FirstPyramidHistoryDailyState
          WHERE trade_date == T``（canonical history source 自身）。
    - **HISTORY previous/historical First Pyramid** 仅来自
      ``FirstPyramidHistoryDailyState WHERE trade_date < T``（严格早于目标日）。
      历史 baseline 的 trade_date <= T-1 即合法；**不要求** T 日 history state。
    - **Review 日线/滚动事实**（review_return_1d / price_position / volume /
      amount / ratio20 / percentile20 / percentile200 等）由 ``ReviewMemberFact.build``
      的共享纯函数派生，复用同一 SSOT，不在此重复硬编码公式。

    设计目标（load-once，消除旧路径 date × scope × 400 日 bars 重复读取）：
    - 全市场只做固定批量查询：current snapshot / previous history state / current bar /
      previous bar / instrument 身份；每个 instrument 只构造一份 fact
      （facts_by_instrument），多个 scope 通过 membership IDs 从内存 map 筛选。

    Args:
        session: 异步 DB 会话
        trade_date: 目标交易日 T
        source_core_run_id: [FIX] 当日正式 stock_core run id（CURRENT FP 来源，
            仅 current_source="stock_core" 时使用）
        instrument_ids: 限制加载的 instrument 子集（None=全市场当日快照）
        required_source_history_run_id: [REVIEW-FACT-PARITY-02 §11] 正式 Review
            绑定的 canonical history source run；非 None 时 previous history state 的
            source_history_run_id 必须与之相等，否则 fail closed。防止 load-once
            阶段重新解析到另一个 history run 造成 lineage drift。
        required_history_contract_version: 同上，显式要求的 history contract
            version；None 时回退到模块级 canonical 常量。
        current_source: CURRENT FP 来源，``"stock_core"``（默认，正式 Review）或
            ``"history_state"``（历史回放/bootstrap）。

    Returns:
        {instrument_id: to_metric_input() dict}，供任意 scope membership 复用。
    """
    _required_version = (
        required_history_contract_version or _REVIEW_HISTORY_CONTRACT_VERSION
    )

    # [P1-D1 FIX] HISTORY FP state 查询必须 bounded：绝不做 trade_date <= T 全历史扫描。
    #   - history_state 模式：CURRENT(== T) 单独查询；PREVIOUS(< T) DISTINCT ON 每 instrument 一行。
    #   - stock_core 模式：只查 PREVIOUS(< T) DISTINCT ON（CURRENT 来自 StockFeatureSnapshot）。
    # "load-once" 是 scope 级规则，不是 "只能有 1 条 SQL"；两条 bounded 查询可接受。
    if current_source == "history_state":
        current_hist_stmt = (
            select(FirstPyramidHistoryDailyState)
            .where(
                FirstPyramidHistoryDailyState.trade_date == trade_date,
                FirstPyramidHistoryDailyState.algorithm_version
                == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            )
        )
        if instrument_ids is not None:
            current_hist_stmt = current_hist_stmt.where(
                FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            )
        current_hist_states = list((await session.execute(current_hist_stmt)).scalars())
        latest_by_instrument: dict[uuid.UUID, FirstPyramidHistoryDailyState] = {
            s.instrument_id: s for s in current_hist_states
        }
    else:
        latest_by_instrument = {}

    previous_hist_stmt = (
        select(FirstPyramidHistoryDailyState)
        .where(
            FirstPyramidHistoryDailyState.trade_date < trade_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
        .distinct(FirstPyramidHistoryDailyState.instrument_id)
        .order_by(
            FirstPyramidHistoryDailyState.instrument_id,
            FirstPyramidHistoryDailyState.trade_date.desc(),
        )
    )
    if instrument_ids is not None:
        previous_hist_stmt = previous_hist_stmt.where(
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
        )
    previous_hist_states = list((await session.execute(previous_hist_stmt)).scalars())
    previous_by_instrument: dict[uuid.UUID, FirstPyramidHistoryDailyState] = {
        s.instrument_id: s for s in previous_hist_states
    }

    # [P1-C FIX] lineage authority 按模式区分，禁止合并两种权威：
    #   - history_state 模式：CURRENT T 状态本身即 lineage 权威。要求
    #       current.source_history_run_id IS NOT NULL
    #       current.history_contract_version == required/current 版本
    #       若提供 required_source_history_run_id，current 必须 == 它（fail closed）
    #       previous(< T) 必须 shared current 的 source run + contract version
    #   - stock_core 模式：previous 必须匹配绑定的 canonical history source（旧逻辑）。
    if current_source == "history_state":
        for _iid, _cur in latest_by_instrument.items():
            _cur_run = getattr(_cur, "source_history_run_id", None)
            if _cur_run is None:
                raise ValueError(
                    f"HISTORY_STATE_CURRENT_SOURCE_RUN_NULL: instrument={_iid} "
                    f"trade_date={trade_date}（history_state 模式 CURRENT T 状态 "
                    f"必须携带 source_history_run_id）"
                )
            if getattr(_cur, "history_contract_version", None) != _required_version:
                raise ValueError(
                    f"HISTORY_STATE_CURRENT_CONTRACT_MISMATCH: instrument={_iid} "
                    f"expected={_required_version} got="
                    f"{getattr(_cur, 'history_contract_version', None)!r}"
                )
            if (
                required_source_history_run_id is not None
                and _cur_run != required_source_history_run_id
            ):
                raise ValueError(
                    f"HISTORY_STATE_CURRENT_SOURCE_RUN_MISMATCH: instrument={_iid} "
                    f"required={required_source_history_run_id!r} got={_cur_run!r}"
                )
            _prev = previous_by_instrument.get(_iid)
            if _prev is not None:
                _prev_run = getattr(_prev, "source_history_run_id", None)
                if _prev_run != _cur_run:
                    raise ValueError(
                        f"HISTORY_STATE_PREVIOUS_SOURCE_RUN_MISMATCH: instrument={_iid} "
                        f"current={_cur_run!r} previous={_prev_run!r} "
                        f"（history_state 模式 previous 必须与 CURRENT T 同 source run）"
                    )
                if getattr(_prev, "history_contract_version", None) != _required_version:
                    raise ValueError(
                        f"HISTORY_STATE_PREVIOUS_CONTRACT_MISMATCH: instrument={_iid} "
                        f"current={_required_version!r} previous="
                        f"{getattr(_prev, 'history_contract_version', None)!r}"
                    )
    else:
        # [REVIEW-FACT-PARITY-02 §11] stock_core 模式：previous 必须匹配绑定 canonical source。
        if required_source_history_run_id is not None:
            for _iid, _prev in previous_by_instrument.items():
                _prev_run = getattr(_prev, "source_history_run_id", None)
                if _prev_run != required_source_history_run_id:
                    raise ValueError(
                        f"HISTORY_PREVIOUS_SOURCE_RUN_MISMATCH: required="
                        f"{required_source_history_run_id!r} got={_prev_run!r} "
                        f"for instrument={_iid} trade_date={trade_date}"
                    )
                _ver = getattr(_prev, "history_contract_version", None) or (
                    _prev.state_payload or {}
                ).get("history_contract_version")
                if _ver != _required_version:
                    raise ValueError(
                        f"HISTORY_CONTRACT_VERSION_MISMATCH(previous): "
                        f"expected={_required_version} got={_ver!r} "
                        f"for trade_date={trade_date}"
                    )

    previous_payloads: dict[uuid.UUID, dict[str, Any]] = {
        iid: state.state_payload for iid, state in previous_by_instrument.items()
    }

    # [FIX] 1. CURRENT First Pyramid：来源由 current_source 决定。
    #    history state 查询（P1-D1 已改为 bounded：== T 取 CURRENT，< T DISTINCT ON 取 PREVIOUS）
    #    在两种模式都执行；stock_core 模式用 StockFeatureSnapshot 作 CURRENT FP。
    current_flat_by_instrument: dict[uuid.UUID, dict[str, Any]] = {}
    snap_trade_date_by_instrument: dict[uuid.UUID, date] = {}
    if current_source == "stock_core":
        # 正式 Review：来自当日正式 stock_core 指针。
        # StockFeatureSnapshot WHERE source_run_id == source_core_run_id，
        # [P2] 额外 WHERE trade_date == trade_date 收窄，绝不静默消费其他日期快照。
        # 读取 summary_payload.first_pyramid_flat。
        if source_core_run_id is None:
            return {}
        current_snap_stmt = (
            select(
                StockFeatureSnapshot.instrument_id,
                StockFeatureSnapshot.trade_date,
                StockFeatureSnapshot.summary_payload,
            )
            .where(StockFeatureSnapshot.source_run_id == source_core_run_id)
        )
        if instrument_ids is not None:
            current_snap_stmt = current_snap_stmt.where(
                StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            )
        current_snap_stmt = current_snap_stmt.where(
            StockFeatureSnapshot.trade_date == trade_date,
        )
        current_snap_rows = (await session.execute(current_snap_stmt)).all()
        if not current_snap_rows:
            # 当日 stock_core 指针无快照：返回空映射（上层 scope phase 用
            # insufficient_history / 空范围处理）。真实数据缺失，非 lineage 漂移。
            return {}
        for _iid, _snap_td, _summary in current_snap_rows:
            # [P2] 防御性 fail-closed：SQL 已收窄，仍显式校验不消费其他日期。
            if _snap_td != trade_date:
                raise ValueError(
                    f"STOCK_CORE_SNAPSHOT_TRADE_DATE_MISMATCH: instrument={_iid} "
                    f"snapshot_trade_date={_snap_td} review_trade_date={trade_date}"
                )
            if not isinstance(_summary, dict):
                continue
            _flat = _summary.get("first_pyramid_flat") or {}
            if not _flat:
                continue
            current_flat_by_instrument[_iid] = _flat
            snap_trade_date_by_instrument[_iid] = _snap_td
    elif current_source == "history_state":
        # 历史回放 / bootstrap 路径：回放历史日期时无 live stock_core 指针，
        # CURRENT FP 来自 canonical history source 自身的 daily state（trade_date == T）。
        # 该分支**仅用于历史回放**，正式 Review 绝不走此路径。
        # 直接用 latest_by_instrument（== T 行，P1-D1 bounded 查询所得）。
        for _iid, _state in latest_by_instrument.items():
            _flat = previous_state_to_flat(_state.state_payload)
            if not _flat:
                continue
            current_flat_by_instrument[_iid] = _flat
            snap_trade_date_by_instrument[_iid] = _state.trade_date
    else:
        raise ValueError(f"unknown current_source={current_source!r}")

    if not current_flat_by_instrument:
        return {}

    ids = list(current_flat_by_instrument.keys())

    # [FIX] history lineage 校验：绑定源作为历史 baseline 必须合法（contract 匹配 /
    # 源存在 / canonical-compatible）。形式 Review **不要求**该 source 覆盖目标日 T
    # （历史 state < T 即接受），故不传 required_trade_date（见 FIX 4）。
    # 该 readiness 校验仅用于**形式 Review（stock_core 来源）**；history_state 回放
    # 路径读的是 canonical source 自身（CURRENT T 状态即权威），由调用方负责可信源，不在此连 DB 校验。
    # 延迟导入避免与 review_bootstrap_service 形成循环依赖。
    if required_source_history_run_id is not None and current_source == "stock_core":
        from app.services.review_bootstrap_service import (
            validate_canonical_history_run_readiness,
        )
        readiness = await validate_canonical_history_run_readiness(
            session,
            required_source_history_run_id,
            _required_version,
        )
        if readiness.get("status") != "ok":
            raise ValueError(
                f"CANONICAL_HISTORY_SOURCE_NOT_READY: source="
                f"{required_source_history_run_id} contract={_required_version} "
                f"未就绪（{readiness.get('reason')}）；fail closed，禁止退回旧源。"
            )

    # 3. [P1-A FIX A1] 完整 BarDaily 历史窗口（trade_date - 400d .. target_date）。
    #    与 fetch_member_flat_list / bootstrap replay load-once 使用**同一 400 日窗口**，
    #    保证 ReviewMemberFact.build 内部派生 price_position(120d)/ratio20(20d)/
    #    percentile20/percentile200 所需的完整序列可用，且与 Historical replay
    #    严格 parity。绝不能用「只取 target_date 当根 + 最近 1 根 previous」的
    #    双 bar 近似——那会令 120/200 日滚动统计失效（source-drift 的本质来源）。
    # 3. [P1-A FIX A1] 完整 BarDaily 历史窗口（trade_date - 400d .. target_date）。
    #    与 fetch_member_flat_list / bootstrap replay load-once 使用**同一 400 日窗口**，
    #    保证 ReviewMemberFact.build 内部派生 price_position(120d)/ratio20(20d)/
    #    percentile20/percentile200 所需的完整序列可用，且与 Historical replay
    #    严格 parity。绝不能用「只取 target_date 当根 + 最近 1 根 previous」的
    #    双 bar 近似——那会令 120/200 日滚动统计失效（source-drift 的本质来源）。
    # [P1-D2 FIX] 不一次性把全市场 400 日 bar 同时物化进内存：按固定 instrument 块
    # 分块加载，每块只取本块标的的窗口 bar，装配完即释放。query count 随
    # ceil(instrument_count / chunk_size) 增长，与 scope 数量无关（load-once scope 级）。
    bar_window_chunk_size = 256  # [P1-D2] 内存分块大小（不一次物化全市场 bar）
    full_bars_by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    _window_start = trade_date - timedelta(days=400)
    for _chunk_start in range(0, len(ids), bar_window_chunk_size):
        _chunk_ids = ids[_chunk_start : _chunk_start + bar_window_chunk_size]
        _chunk_bar_stmt = (
            select(BarDaily)
            .where(
                BarDaily.instrument_id.in_(_chunk_ids),
                BarDaily.trade_date <= trade_date,
                BarDaily.trade_date >= _window_start,
            )
            .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
        )
        for bar in (await session.execute(_chunk_bar_stmt)).scalars():
            full_bars_by_instrument[bar.instrument_id].append(DailyBarFact.from_row(bar))
        # 释放本块 bar 结构
        del _chunk_ids
        del _chunk_bar_stmt

    # 5. instrument 身份（symbol/name）
    identity_stmt = select(Instrument).where(Instrument.id.in_(ids))
    identities = {
        item.id: item for item in (await session.execute(identity_stmt)).scalars()
    }

    facts_by_instrument: dict[uuid.UUID, dict[str, Any]] = {}
    for instrument_id, current_flat in current_flat_by_instrument.items():
        identity = identities.get(instrument_id)
        if identity is None:
            continue
        # [P1-A FIX A2] 所有 Review 滚动事实（return_1d / price_position /
        # volume_ratio20 / amount_ratio20 / volume_percentile20 /
        # amount_percentile200 等）必须**复用 ReviewMemberFact.build 单一 SSOT**，
        # 由 member_fact.py 的共享纯函数派生，与 Historical replay 严格 parity。
        # 严禁在此处 inline 硬编码部分公式（旧实现只算了 return_1d/amount/volume，
        # 漏算 price_position/ratio20/percentile20/percentile200，且 current bar
        # 只取 target_date 当根、previous 只取最近一根，与 SSOT 的 120 日窗口/
        # 排序口径不一致 → REVIEW-CURRENT-FACT-SOURCE-DRIFT 的本质来源）。
        # 此处为 load-once 的单一构造点：所有 scope 从内存 map 取**引用**复用。
        # bars 为 [target_date-400d .. target_date] 升序完整序列（FIX A1 已加载），
        # ReviewMemberFact.build 内部只取 target_date 当根与最近一根 previous，
        # 并基于 ordered 序列派生 120 日 price_position / 20 日 ratio / 200 日
        # percentile（纯函数 SSOT）。
        previous_state_payload = previous_payloads.get(instrument_id)
        bars = full_bars_by_instrument.get(instrument_id, [])
        # 无 target_date 当根 Bar（停牌/无数据）→ 与 Historical replay 同一
        # 口径跳过该成员（不进入 facts），避免单只股票崩溃整体 Review，且保持
        # load-once 内存复用一致。有 bar 的成员统一经 SSOT 派生全部滚动事实。
        if not bars or bars[-1].trade_date != trade_date:
            continue
        fact = ReviewMemberFact.build(
            instrument_id=instrument_id,
            symbol=identity.symbol,
            name=identity.name,
            snapshot_id=None,
            trade_date=trade_date,
            first_pyramid=current_flat,
            bars=bars,
            previous_state=previous_state_payload,
            weight=1.0,
            weight_mode="equal_weight",
        )
        flat = fact.to_metric_input()
        # 补充 loader 专属 lineage 元数据（不属 ReviewMemberFact 业务字段）。
        # [P1-C3 FIX] history_state 模式：lineage 来自 CURRENT T 状态本身
        #   （state id / input_hash / source_history_run_id）。
        # stock_core 模式：lineage 来自绑定的 canonical history source。
        # 绝不合成 lineage ID。
        _snap_td = snap_trade_date_by_instrument.get(instrument_id)
        if current_source == "history_state" and instrument_id in latest_by_instrument:
            _cur_state = latest_by_instrument[instrument_id]
            flat.update({
                "_snapshot_trade_date": _snap_td.isoformat() if _snap_td is not None else None,
                "_history_state_id": str(_cur_state.id),
                "_history_input_hash": _cur_state.input_hash,
                "_history_source_run_id": str(_cur_state.source_history_run_id)
                if _cur_state.source_history_run_id is not None
                else None,
            })
        else:
            flat.update({
                "_snapshot_trade_date": _snap_td.isoformat() if _snap_td is not None else None,
                "_history_state_id": None,
                "_history_input_hash": None,
                "_history_source_run_id": str(required_source_history_run_id)
                if required_source_history_run_id is not None
                else None,
            })
        facts_by_instrument[instrument_id] = flat

    return facts_by_instrument


if __name__ == "__main__":
    print(f"LEVEL1_SCOPE_TYPES = {LEVEL1_SCOPE_TYPES}")
    print(f"LEVEL2_SCOPE_TYPES = {LEVEL2_SCOPE_TYPES}")
    print(f"SCOPE_PUBLISH_MIN_COVERAGE = {SCOPE_PUBLISH_MIN_COVERAGE}")
    print("OK: review_scope_service imports verified")
