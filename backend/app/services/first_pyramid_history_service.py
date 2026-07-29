"""第一金字塔非筹码历史回补服务（[CHANGE-20260729-003] 核心与筹码解耦 - P0-11）。

设计目标（ref/instruction.md §三.11）：
1. **按个股为外层**：每只股票一次读完整可用日线，一次调用 history SSOT
2. **一次调用 history SSOT**：`compute_first_pyramid_history` 一次计算多日
   daily_state + 不可变 events，禁止逐日调用 snapshot
3. **保存最近 250 日 daily state 与不可变 events**：
   - daily_state: upsert 到 first_pyramid_history_daily_state（幂等）
   - events: insert on_conflict_do_nothing（不可变，重跑不覆盖）
4. **分批 25—50 股**：默认 batch_size=25，每批一个事务（commit + checkpoint）
5. **幂等重跑**：相同 (instrument_id, trade_date, algorithm_version) 重复执行
   只更新 daily_state 内容，events 不重复插入
6. **禁止回补 chip**：chip 由独立 after_close_chip_consensus job 异步处理

调用链：
    backfill_first_pyramid_history_batch
      └─ for each instrument:
           ├─ bar_repository.get_bars(1d, qfq, completed_only=True)
           ├─ compute_first_pyramid_history(bars, include_chip=False)
           ├─ upsert daily_state rows (on_conflict_do_update)
           └─ insert events (on_conflict_do_nothing)

约束：
- 本服务不读取/写入 chip 相关数据
- 本服务不调用 compute_first_pyramid_snapshot（逐日）
- 单股失败不阻塞其他股票，写入 failed_instruments 列表

模块自测：
    python -m app.services.first_pyramid_history_service
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pandas as pd
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

# 默认回补输出天数（与第一金字塔合同对齐）
_DEFAULT_OUTPUT_BARS = 250

# 默认批量大小（instruction 要求 25—50 股）
_DEFAULT_BATCH_SIZE = 25

# 单股失败阈值：超过则整体标 partial
_FAILURE_THRESHOLD = 0.3


# =============================================================================
# 主入口
# =============================================================================


async def backfill_first_pyramid_history_batch(
    session: AsyncSession,
    instrument_ids: Sequence[uuid.UUID],
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    output_bars: int = _DEFAULT_OUTPUT_BARS,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
    _fetch_bars_func: Callable[..., Awaitable[pd.DataFrame | None]] | None = None,
) -> dict[str, Any]:
    """[P0-11] 第一金字塔非筹码历史回补批量入口。

    按"个股为外层，一次调用 history SSOT"模式回补：
    1. 每只股票读取完整可用日线（point-in-time <= 今日，qfq 复权）
    2. 一次调用 compute_first_pyramid_history(bars, include_chip=False)
    3. 持久化最近 output_bars 日 daily_state（upsert）+ events（on_conflict_do_nothing）
    4. 分批提交事务，每批后回调 progress_callback（checkpoint）

    Args:
        session: 异步 DB 会话（由 caller 控制 commit/rollback 边界）
        instrument_ids: 待回补 instrument ID 列表
        batch_size: 每批 instrument 数（默认 25）
        output_bars: 输出最近 N 个有效日的 daily state（默认 250）
        progress_callback: 进度回调，接收 processed/total/succeeded/failed
        _fetch_bars_func: 测试注入的 bars 获取函数（生产留空，使用 bar_repository）

    Returns:
        统计信息 dict：
        {
            "total_count": int,
            "succeeded_count": int,
            "failed_count": int,
            "skipped_count": int,  # bars 不足或为空
            "status": "succeeded" | "failed" | "partial",
            "failed_instruments": list[dict],  # 失败详情
            "algorithm_version": str,
            "output_bars": int,
        }
    """
    # 延迟导入避免循环依赖
    from app.services.first_pyramid_service import compute_first_pyramid_history

    total = len(instrument_ids)
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0
    failed_instruments: list[dict[str, Any]] = []

    for batch_start in range(0, total, batch_size):
        batch = list(instrument_ids[batch_start:batch_start + batch_size])
        for instrument_id in batch:
            try:
                # 1. 读取完整可用日线
                if _fetch_bars_func is not None:
                    bars = await _fetch_bars_func(instrument_id)
                else:
                    bars = await _fetch_history_daily_bars(instrument_id)

                if bars is None or bars.empty:
                    skipped_count += 1
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": "daily bars 为空",
                    })
                    continue

                # 2. 一次调用 history SSOT（include_chip=False）
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                )

                # 3. 持久化 daily_state + events
                persisted = await _persist_history_result(
                    session=session,
                    instrument_id=instrument_id,
                    history=history,
                    algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
                )

                if persisted["daily_state_count"] == 0:
                    skipped_count += 1
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": "history daily_state 为空（可能 bars 长度不足）",
                    })
                    continue

                succeeded_count += 1
            except Exception as exc:
                failed_count += 1
                failed_instruments.append({
                    "instrument_id": str(instrument_id),
                    "error": str(exc)[:500],
                })
                logger.error(
                    "[HistoryBackfill] instrument_id=%s 回补失败: %s",
                    instrument_id, exc, exc_info=True,
                )

        # 每批 commit + checkpoint
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error(
                "[HistoryBackfill] batch commit 失败 batch_start=%s: %s",
                batch_start, exc, exc_info=True,
            )
            raise

        if progress_callback is not None:
            try:
                await progress_callback(
                    processed=min(batch_start + len(batch), total),
                    total=total,
                    succeeded=succeeded_count,
                    failed=failed_count,
                    skipped=skipped_count,
                )
            except Exception as exc:
                logger.warning(
                    "[HistoryBackfill] progress_callback 失败: %s", exc,
                )

    # 统计状态
    if failed_count == 0 and succeeded_count > 0:
        status = "succeeded"
    elif succeeded_count == 0 and failed_count > 0:
        status = "failed"
    elif succeeded_count > 0 and failed_count > 0:
        status = "partial"
    else:
        status = "failed"  # 全部 skipped

    result = {
        "total_count": total,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "status": status,
        "failed_instruments": failed_instruments,
        "algorithm_version": FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        "output_bars": output_bars,
    }

    logger.info(
        "[HistoryBackfill] 批量回补完成: total=%d, succeeded=%d, failed=%d, skipped=%d, status=%s",
        total, succeeded_count, failed_count, skipped_count, status,
    )

    return result


# =============================================================================
# 内部辅助
# =============================================================================


async def _fetch_history_daily_bars(
    instrument_id: uuid.UUID,
) -> pd.DataFrame | None:
    """获取完整可用日线（point-in-time，qfq 复权，已完成 bar）。

    本函数为薄包装，调用 bar_repository.get_bars。
    断点：依赖 bar_repository 的真实接口，本地纯单元测试由 caller 注入 mock。
    """
    try:
        from app.db import AsyncSessionLocal
        from app.repositories.bar_repository import get_bars
    except ImportError:
        logger.warning(
            "[HistoryBackfill] bar_repository 或 AsyncSessionLocal 不可用，返回空 bars",
        )
        return None

    async with AsyncSessionLocal() as db:
        result = await get_bars(
            db,
            instrument_id,
            timeframe="1d",
            adjustment="qfq",
            completed_only=True,
        )
        return result.bars


async def _persist_history_result(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    history: dict[str, Any],
    algorithm_version: str,
) -> dict[str, int]:
    """持久化 history SSOT 结果到两张表。

    - daily_state: upsert（on_conflict_do_update），更新 state_payload
    - events: insert on_conflict_do_nothing（不可变，重跑不覆盖）

    Args:
        session: 异步 DB 会话（不 commit，由 caller 控制）
        instrument_id: 股票 ID
        history: compute_first_pyramid_history 返回的 dict
        algorithm_version: 算法版本

    Returns:
        {"daily_state_count": int, "events_count": int}
    """
    daily_state_list = history.get("daily_state") or []
    events_list = history.get("events") or []
    meta = history.get("meta") or {}
    input_hash = meta.get("input_hash") or ""

    daily_state_count = 0
    events_count = 0

    # 1. upsert daily_state
    for state in daily_state_list:
        time_str = state.get("time")
        if not time_str:
            continue
        try:
            trade_date_val = pd.to_datetime(time_str).date()
        except (ValueError, TypeError):
            continue

        stmt = pg_insert(FirstPyramidHistoryDailyState).values(
            instrument_id=instrument_id,
            trade_date=trade_date_val,
            algorithm_version=algorithm_version,
            input_hash=input_hash,
            state_payload=state,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_first_pyramid_history_daily_state_instr_date_ver",
            set_={
                "input_hash": stmt.excluded.input_hash,
                "state_payload": stmt.excluded.state_payload,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
        daily_state_count += 1

    # 2. insert events (on_conflict_do_nothing - immutable)
    for evt in events_list:
        event_type = evt.get("type") or evt.get("event_type") or "UNKNOWN"
        # 构造稳定 event_id：优先用 event 自带的 id，其次 bar_index+type，最后 time+type
        event_id = (
            evt.get("event_id")
            or evt.get("id")
            or _build_event_id(evt, event_type)
        )
        if not event_id:
            continue

        event_time = evt.get("time") or evt.get("anchor_time")

        stmt = pg_insert(FirstPyramidHistoryEvent).values(
            instrument_id=instrument_id,
            algorithm_version=algorithm_version,
            event_type=event_type,
            event_id=str(event_id),
            event_time=str(event_time) if event_time else None,
            event_payload=evt,
        )
        stmt = stmt.on_conflict_do_nothing(
            constraint="uq_first_pyramid_history_events_instr_ver_evid",
        )
        await session.execute(stmt)
        events_count += 1

    await session.flush()

    return {
        "daily_state_count": daily_state_count,
        "events_count": events_count,
    }


def _build_event_id(evt: dict[str, Any], event_type: str) -> str:
    """构造事件稳定 ID（无自带 id 时使用）。

    优先级：bar_index+type > time+type > anchor_time+type
    """
    bar_index = evt.get("bar_index")
    if bar_index is not None:
        return f"{event_type}_{bar_index}"

    time_val = evt.get("time") or evt.get("anchor_time")
    if time_val:
        return f"{event_type}_{time_val}"

    # fallback: hash of payload
    import hashlib
    import json
    payload_str = json.dumps(evt, sort_keys=True, default=str)
    return f"{event_type}_{hashlib.md5(payload_str.encode()).hexdigest()[:12]}"


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯静态自测：验证 _build_event_id 稳定性
    evt1 = {"type": "BOS", "bar_index": 50, "time": "2026-07-01"}
    evt2 = {"type": "OB_CREATED", "anchor_time": "2026-07-01", "ob_id": "abc"}
    evt3 = {"type": "SQZ_RELEASE", "time": "2026-07-01", "direction": "up"}

    id1 = _build_event_id(evt1, "BOS")
    id2 = _build_event_id(evt2, "OB_CREATED")
    id3 = _build_event_id(evt3, "SQZ_RELEASE")

    assert id1 == "BOS_50", f"id1={id1}"
    assert id2 == "OB_CREATED_2026-07-01", f"id2={id2}"
    assert id3 == "SQZ_RELEASE_2026-07-01", f"id3={id3}"

    # 验证 model 字段
    from app.models.first_pyramid_history import (
        FirstPyramidHistoryDailyState,
        FirstPyramidHistoryEvent,
    )
    ds_cols = {c.name for c in FirstPyramidHistoryDailyState.__table__.columns}
    ev_cols = {c.name for c in FirstPyramidHistoryEvent.__table__.columns}
    assert "state_payload" in ds_cols
    assert "event_payload" in ev_cols

    print("OK: first_pyramid_history_service 自测通过")
    print(f"  daily_state cols: {sorted(ds_cols)}")
    print(f"  events cols: {sorted(ev_cols)}")
    print(f"  event_id samples: {id1}, {id2}, {id3}")
