"""StateEventService - 状态变化事件生成与清理服务。

PRD V1.1 §7.3 核心实现：
- 盘后快照成功发布后，读取同算法版本的相邻两份快照
- 比较 code/value（禁止比较中文 label）
- 每只股票每个 source_run_id 最多生成一条聚合事件
- changed_fields 列出全部变化字段
- 通过 ON CONFLICT DO NOTHING 写入（稳定幂等键）
- GET 请求只读，禁止请求时写事件

事件生命周期：
1. after_close finish_snapshot_run(status='succeeded') 后调用 generate_events_for_run
2. 读取当前 run 对应的所有 snapshot
3. 对每只股票，查找同算法版本的前一份快照
4. 比较相邻快照的 code（非 label）
5. 有变化时生成一条聚合事件，ON CONFLICT DO NOTHING 写入

设计原则：
- 比较 code 而非 label（V1.1 硬性规定）
- 事件类型使用稳定 code（如 state_transition），不以"第一个变化字段"代表多字段事件
- evidence 只保存触发事件的必要证据（字段 code、前后 code），不保存完整状态
- 90 天清理任务通过 created_at 索引高效删除

用法：
    from app.services.state_event_service import generate_events_for_run
    stats = await generate_events_for_run(session, run_id)

模块自测：
    python -m app.services.state_event_service
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, desc, select
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

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_CLEANUP_DAYS = 90
_EVENT_TYPE_TRANSITION = "state_transition"

# 状态字段路径（用于比较 code）
# 这些路径对应 StockState 中的 StateValue.code
_STATE_FIELD_PATHS = [
    "structure.price",
    "structure.consensusRelation",
    "momentum.macd",
    "momentum.sqzmom",
    "momentum.temporal.daily_dsa_dir",
    "momentum.temporal.trend_alignment",
    "volatility.bollPosition",
]


# =============================================================================
# 纯函数：提取状态 code 用于比较
# =============================================================================


def extract_state_codes(state: StockState) -> dict[str, str | None]:
    """从 StockState 提取稳定 code 字典，用于事件比较。

    V1.1: 比较 code/value，禁止比较中文 label。
    返回 {field_path: code} 字典，code 为 None 表示数据不足。

    Args:
        state: StockState DTO

    Returns:
        {field_path: code_value} 字典
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

    V1.1 核心规则：
    - 比较 code（非 label）
    - None → 非 None 算变化（数据从无到有）
    - 非 None → None 算变化（数据从有到无）
    - None → None 不算变化（都无数据）
    - code 不同算变化

    Args:
        prev_codes: 前一状态 code 字典
        curr_codes: 当前状态 code 字典

    Returns:
        变化字段路径列表（稳定 code 路径，非中文）
    """
    changed: list[str] = []
    for path in _STATE_FIELD_PATHS:
        prev_val = prev_codes.get(path)
        curr_val = curr_codes.get(path)
        # None → None 不算变化
        if prev_val is None and curr_val is None:
            continue
        # 值不同算变化
        if prev_val != curr_val:
            changed.append(path)
    return changed


def build_event_evidence(
    prev_state: StockState | None,
    curr_state: StockState,
    changed_fields: list[str],
) -> list[dict[str, Any]]:
    """构建事件证据（只保存必要证据，不保存完整状态）。

    V1.1: 保存触发事件的必要证据（字段 code、前后 code），禁止保存完整状态。

    Args:
        prev_state: 前一状态（首次无前值时为 None）
        curr_state: 当前状态
        changed_fields: 变化字段列表

    Returns:
        证据列表 [{field, prevCode, currCode}]
    """
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


def build_event_title_and_description(
    curr_state: StockState,
    changed_fields: list[str],
) -> tuple[str, str]:
    """构建事件标题和描述。

    V1.1: event_type 使用稳定类型（state_transition），
    不以"第一个变化字段"代表多字段事件。
    标题和描述基于全部变化字段。
    """
    count = len(changed_fields)
    if count == 0:
        return ("无状态变化", "当前暂无新的状态变化")

    title = f"{count} 项状态发生变化"
    desc_parts = []
    for path in changed_fields:
        # 使用字段路径的最后一部分作为简短描述
        short = path.split(".")[-1]
        desc_parts.append(short)
    description = f"变化字段: {', '.join(desc_parts)}"
    return (title, description)


# =============================================================================
# 数据库查询：查找相邻快照
# =============================================================================


async def _find_previous_snapshot(
    session: AsyncSession,
    instrument_id: UUID,
    current_trade_date: date,
    schema_version: int,
    primary_timeframe: str,
    secondary_timeframe: str,
    adj: str,
) -> StockFeatureSnapshot | None:
    """查找同算法版本的前一份快照（trade_date < current，按 trade_date 降序取第一）。

    V1.1: 读取同算法版本的相邻两份快照进行比较。
    版本不兼容不比较。
    """
    stmt = (
        select(StockFeatureSnapshot)
        .where(
            and_(
                StockFeatureSnapshot.instrument_id == instrument_id,
                StockFeatureSnapshot.trade_date < current_trade_date,
                StockFeatureSnapshot.schema_version == schema_version,
                StockFeatureSnapshot.primary_timeframe == primary_timeframe,
                StockFeatureSnapshot.secondary_timeframe == secondary_timeframe,
                StockFeatureSnapshot.adj == adj,
            )
        )
        .order_by(desc(StockFeatureSnapshot.trade_date))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_run_snapshots(
    session: AsyncSession,
    run_id: UUID,
) -> list[StockFeatureSnapshot]:
    """获取指定 run 对应的所有快照。

    通过 run 的 trade_date + schema_version + timeframes 查询对应快照。
    """
    run = await session.get(StockFeatureSnapshotRun, run_id)
    if run is None:
        logger.warning("run 不存在: run_id=%s", run_id)
        return []

    stmt = (
        select(StockFeatureSnapshot)
        .where(
            and_(
                StockFeatureSnapshot.trade_date == run.trade_date,
                StockFeatureSnapshot.schema_version == run.schema_version,
                StockFeatureSnapshot.primary_timeframe == run.primary_timeframe,
                StockFeatureSnapshot.secondary_timeframe == run.secondary_timeframe,
                StockFeatureSnapshot.adj == run.adj,
            )
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _get_instrument_symbol(
    session: AsyncSession,
    instrument_id: UUID,
) -> str | None:
    """获取 instrument 的 symbol。"""
    stmt = select(Instrument.symbol).where(Instrument.id == instrument_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# =============================================================================
# 核心：为单只股票生成事件
# =============================================================================


async def _generate_event_for_instrument(
    session: AsyncSession,
    curr_snapshot: StockFeatureSnapshot,
    run: StockFeatureSnapshotRun,
    symbol: str,
) -> StockStateEvent | None:
    """为单只股票生成状态变化事件。

    流程：
    1. 查找同算法版本的前一份快照
    2. 构建前后 StockState
    3. 比较 code（非 label）
    4. 有变化时生成事件
    5. ON CONFLICT DO NOTHING 写入

    Returns:
        StockStateEvent ORM（生成了事件）或 None（无变化或无前值）
    """
    # 查找前一份快照
    prev_snapshot = await _find_previous_snapshot(
        session,
        curr_snapshot.instrument_id,
        curr_snapshot.trade_date,
        run.schema_version,
        run.primary_timeframe,
        run.secondary_timeframe,
        run.adj,
    )

    # 构建当前 StockState
    curr_state = build_stock_state(curr_snapshot, run, symbol)

    # 构建前一 StockState（无前值时为 None）
    prev_state: StockState | None = None
    if prev_snapshot is not None:
        prev_state = build_stock_state(prev_snapshot, run, symbol)

    # 比较 code
    prev_codes = extract_state_codes(prev_state) if prev_state else {}
    curr_codes = extract_state_codes(curr_state)
    changed_fields = compare_state_codes(prev_codes, curr_codes)

    # 无变化不建事件
    if len(changed_fields) == 0:
        return None

    # 有变化但无前值（首次）：不生成 transition 事件（无前后对比）
    if prev_snapshot is None:
        return None

    # 构建事件
    title, description = build_event_title_and_description(curr_state, changed_fields)
    evidence = build_event_evidence(prev_state, curr_state, changed_fields)

    # 幂等键
    algorithm_version = f"v{run.schema_version}"
    idempotency_key = f"{symbol}:{run.id}:{algorithm_version}"

    # occurred_at: 当前快照 trade_date 15:00+08:00
    occurred_at = datetime(
        curr_snapshot.trade_date.year,
        curr_snapshot.trade_date.month,
        curr_snapshot.trade_date.day,
        15, 0, 0, tzinfo=_SHANGHAI_TZ,
    )

    event = StockStateEvent(
        instrument_id=curr_snapshot.instrument_id,
        symbol=symbol,
        source_run_id=run.id,
        algorithm_version=algorithm_version,
        occurred_at=occurred_at,
        previous_as_of=prev_snapshot.trade_date,
        current_as_of=curr_snapshot.trade_date,
        event_type=_EVENT_TYPE_TRANSITION,
        title=title,
        description=description,
        changed_fields=changed_fields,
        evidence=evidence,
        idempotency_key=idempotency_key,
    )

    # ON CONFLICT DO NOTHING（幂等写入）
    stmt = pg_insert(StockStateEvent).values(
        instrument_id=event.instrument_id,
        symbol=event.symbol,
        source_run_id=event.source_run_id,
        algorithm_version=event.algorithm_version,
        occurred_at=event.occurred_at,
        previous_as_of=event.previous_as_of,
        current_as_of=event.current_as_of,
        event_type=event.event_type,
        title=event.title,
        description=event.description,
        changed_fields=event.changed_fields,
        evidence=event.evidence,
        idempotency_key=event.idempotency_key,
    ).on_conflict_do_nothing(
        constraint="uq_state_events_idempotency_key",
    )
    await session.execute(stmt)
    return event


# =============================================================================
# 批量：为整个 run 生成事件
# =============================================================================


async def generate_events_for_run(
    session: AsyncSession,
    run_id: UUID,
) -> dict[str, Any]:
    """为指定 run 的所有快照批量生成状态变化事件。

    V1.1 事件生命周期：
    - 盘后快照成功发布后调用此函数
    - 读取当前 run 对应的所有快照
    - 对每只股票查找同算法版本的前一份快照
    - 比较相邻快照 code，有变化时生成聚合事件
    - ON CONFLICT DO NOTHING 保证幂等

    Args:
        session: 异步 DB 会话
        run_id: 特征快照 run ID

    Returns:
        统计信息 {event_count, skipped_count, failed_count, run_id}
    """
    run = await session.get(StockFeatureSnapshotRun, run_id)
    if run is None:
        logger.warning("run 不存在: run_id=%s", run_id)
        return {"event_count": 0, "skipped_count": 0, "failed_count": 0, "run_id": str(run_id)}

    if run.status != STATUS_SUCCEEDED:
        logger.warning("run 未 succeeded，跳过事件生成: run_id=%s status=%s", run_id, run.status)
        return {"event_count": 0, "skipped_count": 0, "failed_count": 0, "run_id": str(run_id)}

    snapshots = await _get_run_snapshots(session, run_id)
    if not snapshots:
        logger.info("run 无快照: run_id=%s", run_id)
        return {"event_count": 0, "skipped_count": 0, "failed_count": 0, "run_id": str(run_id)}

    event_count = 0
    skipped_count = 0
    failed_count = 0

    # 缓存 symbol 查询
    symbol_cache: dict[UUID, str] = {}

    for snapshot in snapshots:
        try:
            # 获取 symbol（带缓存）
            symbol = symbol_cache.get(snapshot.instrument_id)
            if symbol is None:
                symbol = await _get_instrument_symbol(session, snapshot.instrument_id)
                if symbol is None:
                    logger.warning("instrument 不存在: instrument_id=%s", snapshot.instrument_id)
                    failed_count += 1
                    continue
                symbol_cache[snapshot.instrument_id] = symbol

            event = await _generate_event_for_instrument(
                session, snapshot, run, symbol,
            )
            if event is not None:
                event_count += 1
            else:
                skipped_count += 1
        except Exception as exc:
            failed_count += 1
            logger.error(
                "生成事件失败 instrument_id=%s run_id=%s: %s",
                snapshot.instrument_id, run_id, exc, exc_info=True,
            )

    logger.info(
        "事件生成完成 run_id=%s event_count=%d skipped=%d failed=%d",
        run_id, event_count, skipped_count, failed_count,
    )
    return {
        "event_count": event_count,
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

    Args:
        session: 异步 DB 会话
        days: 保留天数（默认 90）

    Returns:
        {deleted_count, cutoff_date}
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = delete(StockStateEvent).where(StockStateEvent.created_at < cutoff)
    result = await session.execute(stmt)
    deleted = result.rowcount or 0
    logger.info("清理旧事件: deleted=%d cutoff=%s", deleted, cutoff.isoformat())
    return {"deleted_count": deleted, "cutoff_date": cutoff.isoformat()}


# =============================================================================
# 查询：获取股票最近事件
# =============================================================================


async def get_recent_events_for_instrument(
    session: AsyncSession,
    instrument_id: UUID,
    limit: int = 10,
) -> list[StockStateEvent]:
    """获取指定股票的最近事件列表。

    GET /stocks/{symbol}/context 只读查询使用。
    """
    stmt = (
        select(StockStateEvent)
        .where(StockStateEvent.instrument_id == instrument_id)
        .order_by(desc(StockStateEvent.occurred_at))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# =============================================================================
# 模块自测
# =============================================================================

if __name__ == "__main__":
    print("state_event_service 自测...")

    # 构造测试数据
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

    # Test 5: 标题和描述
    title, desc = build_event_title_and_description(curr2, changed2)
    assert "2 项" in title
    print(f"Test 5 ✓: 标题={title}, 描述={desc}")

    # Test 6: 证据构建
    evidence = build_event_evidence(prev, curr2, changed2)
    assert len(evidence) == 2
    assert all("field" in e and "prevCode" in e and "currCode" in e for e in evidence)
    print(f"Test 6 ✓: 证据={evidence}")

    print("OK: state_event_service 自测通过")
