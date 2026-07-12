"""StateEventService - 状态变化事件生成与清理服务（批量查询，无 N+1）。

PRD V1.1 §7.3 核心实现：
- 盘后快照成功发布后，读取同算法版本的相邻两份快照
- 比较 code/values（禁止比较中文 label）
- 每只股票每个 source_run_id 最多生成一条聚合事件
- changed_fields 列出全部变化字段
- 通过 ON CONFLICT DO NOTHING 写入（稳定幂等键）

批量查询设计（固定 3 条 SQL + 1 次 get，不随股票数增长）：
1. session.get(run) - 读取当前 run
2. Query 1: 批量读取当前 run 快照 + instrument symbol（JOIN）
3. Query 2: 子查询 MAX(trade_date) + JOIN 批量取前一兼容快照
4. Query 3: 批量 INSERT ON CONFLICT DO NOTHING

事件时间拆分（P1-3）：
- occurred_at = run.published_at 或 run.finished_at（检测时间）
- current_as_of = trade_date（状态日期）
- previous_as_of = 前一快照 trade_date
- created_at = DB 写入时间

用法：
    from app.services.state_event_service import generate_events_for_run
    stats = await generate_events_for_run(session, run_id)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.models.stock_state_event import StockStateEvent
from app.schemas.stock_state import StateValue, StockState, build_stock_state

logger = logging.getLogger(__name__)

_CLEANUP_DAYS = 90
_EVENT_TYPE_TRANSITION = "state_transition"
# 状态提取算法版本（build_stock_state 逻辑变更时 bump）
_STATE_ALGORITHM_VERSION = "state_v1"

# 状态字段路径（用于比较 code）
_STATE_FIELD_PATHS = [
    "structure.price",
    "structure.consensusRelation",
    "momentum.macd",
    "momentum.sqzmom",
    "momentum.temporal.daily_dsa_dir",
    "momentum.temporal.trend_alignment",
    "volatility.bollPosition",
]

# P1-1: 字段路径 → 用户可读文案白名单（API 层映射，DB 只存稳定 code）
_FIELD_LABELS: dict[str, str] = {
    "structure.price": "价格位置",
    "structure.consensusRelation": "成交密集区关系",
    "momentum.macd": "MACD 动量",
    "momentum.sqzmom": "SQZMOM 动量",
    "momentum.temporal.daily_dsa_dir": "日线 DSA 方向",
    "momentum.temporal.trend_alignment": "趋势对齐",
    "volatility.bollPosition": "布林位置",
}


# =============================================================================
# 纯函数：提取状态 code 用于比较
# =============================================================================


def extract_state_codes(state: StockState) -> dict[str, str | None]:
    """从 StockState 提取稳定 code 字典，用于事件比较。

    V1.1: 比较 code/value，禁止比较中文 label。
    """
    return {
        "structure.price": state.structure.price.code,
        "structure.consensusRelation": state.structure.consensusRelation.code,
        "momentum.macd": state.momentum.macd.code,
        "momentum.sqzmom": state.momentum.sqzmom.code,
        "momentum.temporal.daily_dsa_dir": (
            state.momentum.temporal[0].code
            if len(state.momentum.temporal) > 0 else None
        ),
        "momentum.temporal.trend_alignment": (
            state.momentum.temporal[1].code
            if len(state.momentum.temporal) > 1 else None
        ),
        "volatility.bollPosition": state.volatility.bollPosition.code,
    }


def compare_state_codes(
    prev_codes: dict[str, str | None],
    curr_codes: dict[str, str | None],
) -> list[str]:
    """比较前后状态 code，返回变化字段路径列表。

    规则：
    - None → None 不算变化
    - None → 非 None 算变化
    - 非 None → None 算变化
    - code 不同算变化
    """
    changed: list[str] = []
    for path in _STATE_FIELD_PATHS:
        prev_val = prev_codes.get(path)
        curr_val = curr_codes.get(path)
        if prev_val is None and curr_val is None:
            continue
        if prev_val != curr_val:
            changed.append(path)
    return changed


def build_event_evidence(
    prev_state: StockState | None,
    curr_state: StockState,
    changed_fields: list[str],
) -> list[dict[str, Any]]:
    """构建事件证据（只保存必要证据，不保存完整状态）。"""
    evidence: list[dict[str, Any]] = []
    prev_codes = extract_state_codes(prev_state) if prev_state else {}
    curr_codes = extract_state_codes(curr_state)

    for path in changed_fields:
        evidence.append({
            "field": path,
            "prevCode": prev_codes.get(path),
            "currCode": curr_codes.get(path),
        })
    return evidence


def compute_idempotency_key(
    symbol: str,
    trade_date: date,
    algorithm_version: str,
    evidence: list[dict[str, Any]],
) -> str:
    """C3: 计算幂等键 — 包含 evidence（field + prev/curr code/value）。

    确保不同转换（如 None→positive vs positive→negative）产生不同 key，
    同日重跑（相同 evidence）产生相同 key → ON CONFLICT DO NOTHING。
    """
    evidence_sorted = sorted(evidence, key=lambda e: e["field"])
    changes_hash = hashlib.sha256(
        json.dumps(evidence_sorted, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    return (
        f"{symbol}:{trade_date.isoformat()}:"
        f"{algorithm_version}:{changes_hash}"
    )


def build_event_title_and_description(
    changed_fields: list[str],
) -> tuple[str, str]:
    """构建事件标题和描述（使用白名单映射，不输出内部路径）。

    P1-1: DB 保留稳定 code，API 通过白名单映射生成用户文案。
    """
    count = len(changed_fields)
    if count == 0:
        return ("无状态变化", "当前暂无新的状态变化")

    title = f"{count} 项状态发生变化"
    # 使用白名单映射，不输出内部路径
    labels = []
    for path in changed_fields:
        label = _FIELD_LABELS.get(path, path.split(".")[-1])
        labels.append(label)
    description = f"变化字段: {', '.join(labels)}"
    return (title, description)


# =============================================================================
# 批量查询：消除 N+1
# =============================================================================


async def _batch_get_run_snapshots_with_symbol(
    session: AsyncSession,
    run: StockFeatureSnapshotRun,
) -> list[tuple[StockFeatureSnapshot, str]]:
    """批量获取 run 对应的所有快照 + instrument symbol（单条 JOIN 查询）。

    按 source_run_id 精确查询（P0-3），消除日期+参数猜归属的歧义。
    """
    stmt = (
        select(StockFeatureSnapshot, Instrument.symbol)
        .join(Instrument, StockFeatureSnapshot.instrument_id == Instrument.id)
        .where(StockFeatureSnapshot.source_run_id == run.id)
    )
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def _batch_get_previous_snapshots(
    session: AsyncSession,
    instrument_ids: list[UUID],
    current_trade_date: date,
    schema_version: int,
    primary_timeframe: str,
    secondary_timeframe: str,
    adj: str,
) -> dict[UUID, tuple[StockFeatureSnapshot, StockFeatureSnapshotRun]]:
    """批量获取每个 instrument 的前一兼容快照 + 其所属成功 run（JOIN，标准 SQL）。

    PRD V1.1 P1: 前一状态只能来自前一成功发布且算法兼容的 run。
    JOIN StockFeatureSnapshotRun 过滤 status='succeeded'。

    C4: 同日存在多个成功 run 时，按 published_at DESC、finished_at DESC
    确定性选择最新批次（首行保留，后续同 instrument 行跳过）。

    使用 MAX(trade_date) 子查询取每个 instrument 在成功 run 中的最近一条快照，
    避免使用 PostgreSQL 特有的 DISTINCT ON，保证跨方言兼容。
    固定 1 条 SQL，不随股票数增长。
    """
    if not instrument_ids:
        return {}

    # C4: 子查询在每个 instrument 的成功 run 中取最大 trade_date，
    # 避免失败 run 的高 trade_date 遮蔽成功 run。
    max_date_subq = (
        select(
            StockFeatureSnapshot.instrument_id.label("inst_id"),
            func.max(StockFeatureSnapshot.trade_date).label("max_date"),
        )
        .select_from(StockFeatureSnapshot)
        .outerjoin(
            StockFeatureSnapshotRun,
            StockFeatureSnapshot.source_run_id == StockFeatureSnapshotRun.id,
        )
        .where(
            and_(
                StockFeatureSnapshot.instrument_id.in_(instrument_ids),
                StockFeatureSnapshot.trade_date < current_trade_date,
                StockFeatureSnapshot.schema_version == schema_version,
                StockFeatureSnapshot.primary_timeframe == primary_timeframe,
                StockFeatureSnapshot.secondary_timeframe == secondary_timeframe,
                StockFeatureSnapshot.adj == adj,
                StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            )
        )
        .group_by(StockFeatureSnapshot.instrument_id)
    ).subquery()

    # 主查询：JOIN 回 snapshots 表 + JOIN run 表（过滤 succeeded）
    # C4: ORDER BY published_at DESC NULLS LAST, finished_at DESC NULLS LAST
    # 确定性选择最新批次，配合首行保留逻辑。
    stmt = (
        select(StockFeatureSnapshot, StockFeatureSnapshotRun)
        .join(
            max_date_subq,
            and_(
                StockFeatureSnapshot.instrument_id == max_date_subq.c.inst_id,
                StockFeatureSnapshot.trade_date == max_date_subq.c.max_date,
            ),
        )
        .outerjoin(
            StockFeatureSnapshotRun,
            StockFeatureSnapshot.source_run_id == StockFeatureSnapshotRun.id,
        )
        .where(
            and_(
                StockFeatureSnapshot.schema_version == schema_version,
                StockFeatureSnapshot.primary_timeframe == primary_timeframe,
                StockFeatureSnapshot.secondary_timeframe == secondary_timeframe,
                StockFeatureSnapshot.adj == adj,
                # P1: 前一状态必须来自成功发布的 run
                StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            )
        )
        .order_by(
            StockFeatureSnapshotRun.published_at.desc().nulls_last(),
            StockFeatureSnapshotRun.finished_at.desc().nulls_last(),
        )
    )
    result = await session.execute(stmt)
    # C4: 首行保留（ORDER BY 已确保最新 run 在前，同 instrument 后续行跳过）
    prev_map: dict[UUID, tuple[StockFeatureSnapshot, StockFeatureSnapshotRun]] = {}
    for snap, run in result.all():
        if snap.instrument_id not in prev_map:
            prev_map[snap.instrument_id] = (snap, run)
    return prev_map


# =============================================================================
# 核心：批量生成事件
# =============================================================================


async def generate_events_for_run(
    session: AsyncSession,
    run_id: UUID,
) -> dict[str, Any]:
    """为指定 run 的所有快照批量生成状态变化事件（固定 4 条 SQL，无 N+1）。

    V1.1 事件生命周期：
    - 盘后快照成功发布后调用此函数
    - 批量读取当前 run 快照 + symbol（Query 2）
    - 批量读取前一兼容快照（Query 3，DISTINCT ON）
    - 批量 INSERT ON CONFLICT DO NOTHING（Query 4）

    事件时间：
    - occurred_at = run.published_at 或 finished_at（检测时间）
    - current_as_of = trade_date（状态日期）
    - created_at = DB 写入时间

    Args:
        session: 异步 DB 会话
        run_id: 特征快照 run ID

    Returns:
        统计信息 {event_count, skipped_count, failed_count, run_id}
    """
    # Query 1: 读取当前 run
    run = await session.get(StockFeatureSnapshotRun, run_id)
    if run is None:
        logger.warning("run 不存在: run_id=%s", run_id)
        return {
            "event_count": 0,
            "candidate_count": 0,
            "inserted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "run_id": str(run_id),
        }

    if run.status != STATUS_SUCCEEDED:
        logger.warning("run 未 succeeded，跳过事件生成: run_id=%s status=%s", run_id, run.status)
        return {
            "event_count": 0,
            "candidate_count": 0,
            "inserted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "run_id": str(run_id),
        }

    # Query 2: 批量获取快照 + symbol
    snapshots_with_symbol = await _batch_get_run_snapshots_with_symbol(session, run)
    if not snapshots_with_symbol:
        logger.info("run 无快照: run_id=%s", run_id)
        return {
            "event_count": 0,
            "candidate_count": 0,
            "inserted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "run_id": str(run_id),
        }

    # Query 3: 批量获取前一兼容快照 + 其所属成功 run
    instrument_ids = [snap.instrument_id for snap, _ in snapshots_with_symbol]
    prev_data = await _batch_get_previous_snapshots(
        session,
        instrument_ids,
        run.trade_date,
        run.schema_version,
        run.primary_timeframe,
        run.secondary_timeframe,
        run.adj,
    )

    # 检测时间：run.published_at 或 finished_at
    detected_at = run.published_at or run.finished_at or datetime.now(UTC)

    # P1: algorithm_version 使用真实算法版本（schema + state extraction）
    algorithm_version = f"schema_v{run.schema_version}+{_STATE_ALGORITHM_VERSION}"

    candidate_count = 0
    event_count = 0
    skipped_count = 0
    failed_count = 0
    events_to_insert: list[dict[str, Any]] = []

    for curr_snapshot, symbol in snapshots_with_symbol:
        try:
            # 构建当前 StockState
            curr_state = build_stock_state(curr_snapshot, run, symbol)

            # 构建前一 StockState — P1: 使用前一成功 run（非当前 run）
            prev_entry = prev_data.get(curr_snapshot.instrument_id)
            prev_state: StockState | None = None
            prev_snapshot: StockFeatureSnapshot | None = None
            if prev_entry is not None:
                prev_snapshot, prev_run = prev_entry
                prev_state = build_stock_state(prev_snapshot, prev_run, symbol)

            # 比较 code
            prev_codes = extract_state_codes(prev_state) if prev_state else {}
            curr_codes = extract_state_codes(curr_state)
            changed_fields = compare_state_codes(prev_codes, curr_codes)

            # 无变化或无前值不建事件
            if len(changed_fields) == 0 or prev_snapshot is None:
                skipped_count += 1
                continue

            candidate_count += 1

            # 构建事件
            title, description = build_event_title_and_description(changed_fields)
            evidence = build_event_evidence(prev_state, curr_state, changed_fields)

            # C3: 幂等键 = symbol:current_as_of:algorithm_version:hash(evidence)
            # evidence 包含 field + prev/curr code/value，
            # 确保不同转换（如 None→positive vs positive→negative）产生不同 key。
            # 同日重跑（相同 evidence）产生相同 key → ON CONFLICT DO NOTHING。
            idempotency_key = compute_idempotency_key(
                symbol, curr_snapshot.trade_date, algorithm_version, evidence,
            )

            events_to_insert.append({
                "instrument_id": curr_snapshot.instrument_id,
                "symbol": symbol,
                "source_run_id": run.id,
                "algorithm_version": algorithm_version,
                "occurred_at": detected_at,
                "previous_as_of": prev_snapshot.trade_date,
                "current_as_of": curr_snapshot.trade_date,
                "event_type": _EVENT_TYPE_TRANSITION,
                "title": title,
                "description": description,
                "changed_fields": changed_fields,
                "evidence": evidence,
                "idempotency_key": idempotency_key,
            })
            event_count += 1
        except Exception as exc:
            failed_count += 1
            logger.error(
                "生成事件失败 instrument_id=%s run_id=%s: %s",
                curr_snapshot.instrument_id, run_id, exc, exc_info=True,
            )

    # Query 4: 批量 INSERT ON CONFLICT DO NOTHING
    inserted_count = 0
    if events_to_insert:
        stmt = pg_insert(StockStateEvent).values(events_to_insert).on_conflict_do_nothing(
            constraint="uq_state_events_idempotency_key",
        )
        result = await session.execute(stmt)
        # rowcount 可能 None（某些驱动），fallback 为 candidates 数量
        rc = getattr(result, "rowcount", None)
        inserted_count = rc if rc is not None and rc >= 0 else len(events_to_insert)

    logger.info(
        "事件生成完成 run_id=%s candidate=%d inserted=%d event_count=%d skipped=%d failed=%d",
        run_id, candidate_count, inserted_count, event_count, skipped_count, failed_count,
    )
    return {
        "event_count": event_count,
        "candidate_count": candidate_count,
        "inserted_count": inserted_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "run_id": str(run_id),
    }


# =============================================================================
# 90 天清理任务
# =============================================================================


async def cleanup_old_events(
    session: AsyncSession,
    days: int = _CLEANUP_DAYS,
) -> dict[str, Any]:
    """清理超过指定天数的旧事件。

    V1.1: 90 天清理任务、索引、尺寸预算必须同时完成。
    通过 created_at 索引高效删除。
    记录 deleted_count/cutoff/duration_ms 用于审计。
    """
    import time

    start_time = time.time()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = delete(StockStateEvent).where(StockStateEvent.created_at < cutoff)
    result = await session.execute(stmt)
    # rowcount may be None for some drivers; use -1 as sentinel
    deleted = result.rowcount if result.rowcount is not None else -1  # type: ignore[attr-defined]
    duration_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "清理旧事件: deleted=%d cutoff=%s duration_ms=%d",
        deleted, cutoff.isoformat(), duration_ms,
    )
    return {
        "deleted_count": deleted,
        "cutoff_date": cutoff.isoformat(),
        "duration_ms": duration_ms,
    }


# =============================================================================
# 查询：获取股票最近事件
# =============================================================================


async def get_recent_events_for_instrument(
    session: AsyncSession,
    instrument_id: UUID,
    limit: int = 10,
    occurred_at_lte: datetime | None = None,
) -> list[StockStateEvent]:
    """获取指定股票的最近事件列表（只读查询）。

    P0-4: as_of 历史查询时，occurred_at_lte 截止到 as_of 当日结束，
    禁止返回未来事件。

    Args:
        session: 异步 DB 会话
        instrument_id: 股票 ID
        limit: 返回条数
        occurred_at_lte: 事件检测时间上限（as_of 历史查询时传入）
    """
    stmt = (
        select(StockStateEvent)
        .where(StockStateEvent.instrument_id == instrument_id)
    )
    if occurred_at_lte is not None:
        stmt = stmt.where(StockStateEvent.occurred_at <= occurred_at_lte)
    stmt = stmt.order_by(desc(StockStateEvent.occurred_at)).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# =============================================================================
# 模块自测
# =============================================================================

if __name__ == "__main__":
    print("state_event_service 自测...")

    from uuid import uuid4

    from app.schemas.stock_state import (
        StockMomentum,
        StockStructure,
        StockVolatility,
    )

    def _make_state(
        price_code: str | None = "inside",
        consensus_code: str | None = "inside_va",
        macd_code: str | None = None,
        sqzmom_code: str | None = "positive",
        dsa_dir_code: str | None = "1",
        alignment_code: str | None = "aligned",
        boll_code: str | None = "middle",
    ) -> StockState:
        return StockState(
            symbol="000001",
            asOf="2026-07-10",
            sourceRunId=str(uuid4()),
            version="v1",
            computedAt="2026-07-10T15:00:00+08:00",
            structure=StockStructure(
                price=StateValue(code=price_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
                consensusRelation=StateValue(code=consensus_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            ),
            momentum=StockMomentum(
                macd=StateValue(code=macd_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
                sqzmom=StateValue(code=sqzmom_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
                temporal=[
                    StateValue(code=dsa_dir_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
                    StateValue(code=alignment_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
                ],
            ),
            volatility=StockVolatility(
                bollPosition=StateValue(code=boll_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            ),
        )

    # Test 1: 无变化不建事件
    prev = _make_state()
    curr = _make_state()
    prev_codes = extract_state_codes(prev)
    curr_codes = extract_state_codes(curr)
    changed = compare_state_codes(prev_codes, curr_codes)
    assert len(changed) == 0, f"无变化应返回空列表，实际: {changed}"
    print("Test 1 ✓: 无变化不建事件")

    # Test 2: 真实变化检测
    curr2 = _make_state(sqzmom_code="negative", boll_code="near_upper")
    curr2_codes = extract_state_codes(curr2)
    changed2 = compare_state_codes(prev_codes, curr2_codes)
    assert "momentum.sqzmom" in changed2
    assert "volatility.bollPosition" in changed2
    assert len(changed2) == 2
    print(f"Test 2 ✓: 真实变化检测 {changed2}")

    # Test 3: None → None 不算变化
    prev3 = _make_state(macd_code=None)
    curr3 = _make_state(macd_code=None)
    changed3 = compare_state_codes(
        extract_state_codes(prev3), extract_state_codes(curr3)
    )
    assert "momentum.macd" not in changed3
    print("Test 3 ✓: None→None 不算变化")

    # Test 4: None → 非 None 算变化
    curr4 = _make_state(macd_code="positive")
    changed4 = compare_state_codes(
        extract_state_codes(prev3), extract_state_codes(curr4)
    )
    assert "momentum.macd" in changed4
    print("Test 4 ✓: None→非None 算变化")

    # Test 5: 标题和描述使用白名单映射
    title, desc_text = build_event_title_and_description(changed2)
    assert "2 项" in title
    # P1-1: 不输出内部路径
    assert "sqzmom" not in desc_text or "SQZMOM" in desc_text
    assert "bollPosition" not in desc_text
    print(f"Test 5 ✓: 标题={title}, 描述={desc_text}")

    # Test 6: 证据构建
    evidence = build_event_evidence(prev, curr2, changed2)
    assert len(evidence) == 2
    assert all("field" in e and "prevCode" in e and "currCode" in e for e in evidence)
    print(f"Test 6 ✓: 证据={evidence}")

    print("OK: state_event_service 自测通过")
