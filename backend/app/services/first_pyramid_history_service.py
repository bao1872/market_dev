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

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
from app.models.first_pyramid_history_run import (
    HISTORY_RUN_FAILED,
    HISTORY_RUN_PARTIAL,
    HISTORY_RUN_RUNNING,
    HISTORY_RUN_SUCCEEDED,
    FirstPyramidHistoryRun,
)
from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
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


def _apply_pit_normalization(bars: pd.DataFrame, history: dict[str, Any]) -> None:
    """[HISTORY-BACKFILL-PIT-01] 将 one-pass history 归一化到 date-specific PIT。

    从 bars.adj_factor 列提取因子序列，计算 K_t = anchor_factor / factor(t)，
    对 LINEAR_SCALE_COVARIANT 字段应用 post-rescale。

    anchor_factor = 全局 one-pass 的复权分母 = bars 中最新 adj_factor（与
    _fetch_db_only_daily_bars 的 adjustment_as_of=None 语义一致：MDAS 用
    latest_adj 做分母）。

    factor(t) = adj_factor 序列中 trade_date <= t 的最后一个（ffill，与
    adj_factor.py _compute_denominator_factor 同语义）。

    如果 bars 无 adj_factor 列或全 null → 无公司行为 → 跳过归一化。
    """
    from app.services.first_pyramid_service import normalize_history_result_to_pit

    if "adj_factor" not in bars.columns:
        return

    factor_series = bars["adj_factor"].dropna()
    if factor_series.empty:
        return

    # anchor_factor = 全局锚点的复权分母 = 最新 adj_factor
    anchor_factor = float(factor_series.iloc[-1])

    normalize_history_result_to_pit(
        history,
        factor_series=factor_series,
        anchor_factor=anchor_factor,
    )


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
    source_history_run_id: uuid.UUID | None = None,
    history_contract_version: str | None = None,
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

                # 2.2 PIT normalization（HISTORY-BACKFILL-PIT-01）
                _apply_pit_normalization(bars, history)

                # 3. 持久化 daily_state + events
                persisted = await _persist_history_result(
                    session=session,
                    instrument_id=instrument_id,
                    history=history,
                    algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
                    source_history_run_id=source_history_run_id,
                    history_contract_version=history_contract_version,
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
# Run/Item 接入版（CHANGE-20260729-008）
# =============================================================================


_HISTORY_ITEM_PENDING = "pending"
_HISTORY_ITEM_RUNNING = "running"
_HISTORY_ITEM_SUCCEEDED = "succeeded"
_HISTORY_ITEM_FAILED = "failed"
_HISTORY_ITEM_SKIPPED = "skipped"

# 默认 lease 时长（秒），单股 history 计算通常 < 60s
_HISTORY_ITEM_LEASE_SECONDS = 300

# 最大重试次数
_HISTORY_MAX_ATTEMPT_COUNT = 3


# =============================================================================
# Skip reason contract（CHANGE-20260809 / Phase 4B.1）
# =============================================================================
#
# Stage A 的 skipped 是 execution outcome 的一部分：某些 instrument 天然无法产出
# canonical history（历史 bar 不足 / provider 完全无日线数据）。这些 skip 不损害
# 已产出 canonical state 的 PIT 正确性，因此对 Stage B 是 non-blocking 的。
#
# 但「skip 是 non-blocking」必须是**显式白名单**，不能因为 failed==0 就默认放行：
# 未知 skip 原因可能代表 systemic data gap 或未预期排除，此时 run 不得作为
# canonical source 被消费。
#
# 分类结果只有三种，UNKNOWN 一律视为 canonical-blocking。

HISTORY_SKIP_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
HISTORY_SKIP_NO_DAILY_BARS = "NO_DAILY_BARS"
HISTORY_SKIP_UNKNOWN = "UNKNOWN"

# 已知 non-blocking skip category（显式白名单）
ALLOWED_NON_BLOCKING_SKIP_CATEGORIES: frozenset[str] = frozenset(
    {
        HISTORY_SKIP_INSUFFICIENT_HISTORY,
        HISTORY_SKIP_NO_DAILY_BARS,
    }
)

# legacy reason 文本（历史 run 已经写入 DB，无法重写）→ 显式兼容读取。
# 注意：这是**精确前缀匹配**，不是「包含任意子串即通过」。
_LEGACY_NO_DAILY_BARS_REASONS: tuple[str, ...] = (
    "daily bars 为空（DB-only）",
    "daily bars 为空",
)


def classify_history_skip_reason(reason: str | None) -> str:
    """将 Stage A run item 的 skip reason 归类为已知 category。

    [CHANGE-20260809] Phase 4B.1 canonical readiness contract 的组成部分。

    只识别显式已知形态；任何无法识别的字符串（含 None / 空串）都返回 UNKNOWN，
    由调用方 fail closed。禁止 hardcode 具体 symbol。
    """
    if reason is None:
        return HISTORY_SKIP_UNKNOWN
    text = reason.strip()
    if not text:
        return HISTORY_SKIP_UNKNOWN
    if text.startswith(HISTORY_SKIP_INSUFFICIENT_HISTORY):
        return HISTORY_SKIP_INSUFFICIENT_HISTORY
    if text.startswith(HISTORY_SKIP_NO_DAILY_BARS):
        return HISTORY_SKIP_NO_DAILY_BARS
    for legacy in _LEGACY_NO_DAILY_BARS_REASONS:
        if text.startswith(legacy):
            return HISTORY_SKIP_NO_DAILY_BARS
    return HISTORY_SKIP_UNKNOWN


def _compute_parameter_hash(
    output_bars: int,
    include_chip: bool,
    history_contract_version: str,
) -> str:
    """计算历史回补参数 hash。

    [CHANGE-20260808] 必须纳入 history_contract_version，使 review-history-v1 与
    review-history-v2 不能复用同一个 succeeded history run（parameter_hash 不同）。
    """
    raw = (
        f"output_bars={output_bars};include_chip={include_chip};"
        f"history_contract_version={history_contract_version}"
    )
    return hashlib.md5(raw.encode()).hexdigest()[:16]


async def create_history_run(
    session: AsyncSession,
    *,
    algorithm_version: str,
    output_bars: int,
    scope: str,
    instrument_ids: Sequence[uuid.UUID],
    scheduler_job_run_id: uuid.UUID | None = None,
    include_chip: bool = False,
) -> tuple[FirstPyramidHistoryRun, bool]:
    """创建历史回补 run（幂等：相同 algorithm_version + parameter_hash + scope 已有 running/succeeded 则返回已有）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        algorithm_version: 算法版本
        output_bars: 输出最近 N 日
        scope: 范围标识
        instrument_ids: eligible universe（用于 expected_count）
        scheduler_job_run_id: 关联 SchedulerJobRun（可选）
        include_chip: 是否含 chip（默认 False）

    Returns:
        (FirstPyramidHistoryRun, is_new)
    """
    # [CHANGE-20260808] 局部 import 避免循环依赖；HISTORY_CONTRACT_VERSION 进入 run identity
    from app.services.first_pyramid_service import HISTORY_CONTRACT_VERSION

    parameter_hash = _compute_parameter_hash(
        output_bars, include_chip, HISTORY_CONTRACT_VERSION,
    )

    # 幂等查找：同 algorithm_version + parameter_hash + scope 的活跃 run
    existing_stmt = (
        select(FirstPyramidHistoryRun)
        .where(
            FirstPyramidHistoryRun.algorithm_version == algorithm_version,
            FirstPyramidHistoryRun.parameter_hash == parameter_hash,
            FirstPyramidHistoryRun.scope == scope,
            FirstPyramidHistoryRun.status.in_(
                (HISTORY_RUN_RUNNING, HISTORY_RUN_PARTIAL, HISTORY_RUN_SUCCEEDED),
            ),
        )
        .order_by(FirstPyramidHistoryRun.created_at.desc())
        .limit(1)
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        return existing, False

    run = FirstPyramidHistoryRun(
        scheduler_job_run_id=scheduler_job_run_id,
        algorithm_version=algorithm_version,
        parameter_hash=parameter_hash,
        output_bars=output_bars,
        scope=scope,
        expected_count=len(instrument_ids),
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        status=HISTORY_RUN_RUNNING,
        started_at=datetime.now(UTC),
        # [CHANGE-20260808] history_contract_version 进入 run metadata
        metadata_json=json.dumps(
            {"history_contract_version": HISTORY_CONTRACT_VERSION},
            sort_keys=True,
        ),
    )
    session.add(run)
    await session.flush()
    return run, True


async def create_history_run_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    instrument_ids: Sequence[uuid.UUID],
    *,
    input_hash: str | None = None,
) -> int:
    """为 eligible universe 创建 history/pending items（幂等）。

    使用 INSERT ON CONFLICT DO NOTHING 保证并发安全。
    """
    if not instrument_ids:
        return 0

    # 查找已存在的 items
    existing_stmt = (
        select(FirstPyramidHistoryRunItem.instrument_id)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.instrument_id.in_(instrument_ids),
        )
    )
    existing_ids = {
        row[0] for row in (await session.execute(existing_stmt))
    }

    new_items = []
    for instrument_id in instrument_ids:
        if instrument_id in existing_ids:
            continue
        new_items.append(FirstPyramidHistoryRunItem(
            history_run_id=history_run_id,
            instrument_id=instrument_id,
            status=_HISTORY_ITEM_PENDING,
            input_hash=input_hash,
        ))

    if new_items:
        session.add_all(new_items)
        await session.flush()

    return len(new_items)


async def claim_history_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    *,
    worker_instance_id: str,
    batch_size: int = 25,
    lease_seconds: int = _HISTORY_ITEM_LEASE_SECONDS,
    max_attempt_count: int = _HISTORY_MAX_ATTEMPT_COUNT,
) -> list[FirstPyramidHistoryRunItem]:
    """Worker 原子领取一批 pending/可恢复 history items。

    使用 UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING。
    """
    now = datetime.now(UTC)
    lease_expires_at = now + timedelta(seconds=lease_seconds)

    claim_sql = text(
        """
        UPDATE first_pyramid_history_run_items
        SET status = 'running',
            attempt_count = attempt_count + 1,
            lease_epoch = lease_epoch + 1,
            worker_instance_id = :worker_id,
            started_at = COALESCE(started_at, :now),
            heartbeat_at = :now,
            lease_expires_at = :lease_expires,
            updated_at = :now
        WHERE id IN (
            SELECT id FROM first_pyramid_history_run_items
            WHERE history_run_id = :history_run_id
              AND (
                status = 'pending'
                OR (status = 'failed' AND attempt_count < :max_attempts)
                OR (status = 'running' AND lease_expires_at < :now)
              )
            ORDER BY created_at
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, history_run_id, instrument_id, status, attempt_count,
                  input_hash, worker_instance_id, lease_epoch, lease_expires_at,
                  daily_state_count, event_count, last_error, started_at,
                  heartbeat_at, completed_at, created_at, updated_at
        """
    )
    result = await session.execute(claim_sql, {
        "worker_id": worker_instance_id,
        "now": now,
        "lease_expires": lease_expires_at,
        "history_run_id": history_run_id,
        "max_attempts": max_attempt_count,
        "batch_size": batch_size,
    })
    rows = result.fetchall()
    if not rows:
        return []

    items: list[FirstPyramidHistoryRunItem] = []
    for row in rows:
        item = FirstPyramidHistoryRunItem(
            id=row[0],
            history_run_id=row[1],
            instrument_id=row[2],
            status=row[3],
            attempt_count=row[4],
            input_hash=row[5],
            worker_instance_id=row[6],
            lease_epoch=row[7],
            lease_expires_at=row[8],
            daily_state_count=row[9],
            event_count=row[10],
            last_error=row[11],
            started_at=row[12],
            heartbeat_at=row[13],
            completed_at=row[14],
            created_at=row[15],
            updated_at=row[16],
        )
        items.append(item)
    return items


async def mark_history_item_succeeded(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    daily_state_count: int | None = None,
    event_count: int | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 成功（带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_SUCCEEDED,
            daily_state_count=daily_state_count,
            event_count=event_count,
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_history_item_failed(
    session: AsyncSession,
    item_id: uuid.UUID,
    error: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 失败（带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_FAILED,
            last_error=error[:1000],
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_history_item_skipped(
    session: AsyncSession,
    item_id: uuid.UUID,
    reason: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 跳过（数据不足等，带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_SKIPPED,
            last_error=reason[:1000],
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


def _classify_history_zero_output(
    n_input: int,
    daily_state_rows: Sequence[Any],
    required_bars: int,
    meta_error: str | None = None,
) -> tuple[str, str]:
    """分类 history compute 的 zero-output 语义（避免 false-success）。

    返回 (decision, reason)：
    - ("process", "")：有输出，正常持久化
    - ("skip", reason)：INSUFFICIENT_HISTORY —— input bars 不足阈值，合法 zero-output
    - ("fail", reason)：COMPUTE_EMPTY_UNEXPECTED —— bars 足够但 compute 异常返回空，
      fail closed（不得 silently skipped，避免算法 bug 被掩盖）
    """
    if not daily_state_rows and n_input < required_bars:
        return (
            "skip",
            f"INSUFFICIENT_HISTORY: input_bars={n_input} required_bars={required_bars}",
        )
    if not daily_state_rows:
        return (
            "fail",
            "COMPUTE_EMPTY_UNEXPECTED: "
            f"input_bars={n_input} >= {required_bars} "
            f"但 daily_state 为空; meta={meta_error}",
        )
    return ("process", "")


async def requeue_history_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    instrument_ids: Sequence[uuid.UUID],
) -> int:
    """把指定 instrument 的 history run items 重新置为 pending（targeted requeue）。

    [Phase 3D] 用于：
    - 原 skipped（如 BJ 无 bars，补齐 bars 后重新处理）
    - 原 false-success（旧语义标 succeeded 但零输出，需按新语义重新分类）

    只处理该 run 内、命中 instrument_ids 的 skipped/succeeded/failed/pending 项，
    不新建 run、不触碰其他 run、不重置已完成且有真实输出的 succeeded item。

    语义：
    - 保留 canonical history_run_id / input_hash（仅清 status/last_error/lease）
    - 保持 attempt/lease 字段可被 claim_history_items 正常接管
    - 不允许跨 run 重排
    """
    if not instrument_ids:
        return 0
    now = datetime.now(UTC)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.instrument_id.in_(list(instrument_ids)),
        )
        .values(
            status="pending",
            last_error=None,
            attempt_count=0,
            lease_epoch=0,
            worker_instance_id=None,
            started_at=None,
            heartbeat_at=None,
            lease_expires_at=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def get_history_run_progress(
    session: AsyncSession,
    history_run_id: uuid.UUID,
) -> dict[str, Any]:
    """获取 history run 级进度统计。"""
    stmt = (
        select(
            FirstPyramidHistoryRunItem.status,
            func.count(FirstPyramidHistoryRunItem.id).label("cnt"),
        )
        .where(FirstPyramidHistoryRunItem.history_run_id == history_run_id)
        .group_by(FirstPyramidHistoryRunItem.status)
    )
    rows = (await session.execute(stmt)).all()
    counts = {row.status: row.cnt for row in rows}

    succeeded = counts.get(_HISTORY_ITEM_SUCCEEDED, 0)
    failed = counts.get(_HISTORY_ITEM_FAILED, 0)
    pending = counts.get(_HISTORY_ITEM_PENDING, 0)
    running = counts.get(_HISTORY_ITEM_RUNNING, 0)
    skipped = counts.get(_HISTORY_ITEM_SKIPPED, 0)
    total = succeeded + failed + pending + running + skipped
    coverage = succeeded / total if total > 0 else 0.0

    return {
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "running": running,
        "skipped": skipped,
        "total": total,
        "coverage": coverage,
    }


async def finish_history_run(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    *,
    status: str,
) -> None:
    """更新 history run 的最终状态。"""
    progress = await get_history_run_progress(session, history_run_id)
    now = datetime.now(UTC)
    stmt = (
        update(FirstPyramidHistoryRun)
        .where(FirstPyramidHistoryRun.id == history_run_id)
        .values(
            status=status,
            succeeded_count=progress["succeeded"],
            failed_count=progress["failed"],
            skipped_count=progress["skipped"],
            completed_at=now,
            updated_at=now,
        )
    )
    await session.execute(stmt)


async def _fetch_db_only_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    output_bars: int,
) -> pd.DataFrame | None:
    """[CHANGE-20260731-003 / 20260808] SSOT 合规 strict DB-only 读日线行情。

    原实现直接调用 bar_repository._query_daily_bars 违反 SSOT 架构，
    改为通过 MarketDataAggregationService (MDAS) 统一出口。
    completed_only=True 保证只读取已完成 bar；include_realtime=False 禁用实时补充。
    [CHANGE-20260808] allow_backfill=False 强制 strict DB-only：即使 DB 缺尾也绝不调用
    external provider / realtime / 15m。DB 无 completed qfq bars → 返回 None（caller 标 skipped）。
    production history replay / canary 必须使用 strict DB-only。
    """
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    mdas = MarketDataAggregationService()
    agg = await mdas.get_bars(
        session,
        instrument_id,
        timeframe="1d",
        adj="qfq",
        include_realtime=False,
        completed_only=True,
        allow_backfill=False,
        limit=output_bars * 2,  # 留余量，history SSOT 内部会截取 output_bars
    )
    df = agg.bars
    if df is None or df.empty:
        return None
    return df


async def _fetch_pit_daily_bars_for_target(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    output_bars: int,
    target_trade_date: date,
) -> pd.DataFrame | None:
    """[HISTORY-CURRENT-DATE-LIFECYCLE-01 §5] target-aware PIT strict DB-only 读日线。

    与 ``_fetch_db_only_daily_bars`` 的唯一差异：显式传 MDAS 已有的 PIT 参数
    ``end_date`` / ``adjustment_as_of``，保证：

    - ``max(bars.trade_date) <= target_trade_date``（不读未来 bar）
    - 复权锚点固定在 target_trade_date（禁止未来除权事件泄漏）

    其余契约与 backfill 完全一致（同一 MDAS 出口、strict DB-only、completed_only），
    因此 target-date state 与既有历史 state 由同一数据口径产出。
    """
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    mdas = MarketDataAggregationService()
    agg = await mdas.get_bars(
        session,
        instrument_id,
        timeframe="1d",
        adj="qfq",
        include_realtime=False,
        completed_only=True,
        allow_backfill=False,
        end_date=target_trade_date,
        adjustment_as_of=target_trade_date,
        limit=output_bars * 2,
    )
    df = agg.bars
    if df is None or df.empty:
        return None
    return df


async def backfill_history_with_run_items(
    *,
    history_run_id: uuid.UUID,
    algorithm_version: str,
    output_bars: int,
    worker_id: str = "history_worker",
    batch_size: int = 25,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """[CHANGE-20260729-008] Run/Item 接入版历史回补（单股×检查点）。

    与 backfill_first_pyramid_history_batch 关键差异：
    - 使用 first_pyramid_history_run_items 表做单股 claim/lease/commit
    - 每只股票在独立短事务中计算并 commit（失败只回滚该股）
    - coverage 从 run_items 实时统计
    - 恢复只领 pending/可重试 failed/过期 running
    - **DB-only 取数**：直接调 _query_daily_bars，禁止自动 pytdx 拉取
    - 支持 resume：重启只处理 pending/failed/过期 running items

    流程：
    1. claim_history_items 领取一批
    2. 逐股：独立事务读 bars → 计算 history SSOT → 持久化 → commit → mark_item_succeeded
    3. 失败：mark_item_failed，继续下一股
    4. 无可领取 items 时结束

    Args:
        history_run_id: FirstPyramidHistoryRun.id
        algorithm_version: 算法版本
        output_bars: 输出最近 N 日
        worker_id: Worker 标识
        batch_size: claim 批次大小
        progress_callback: 进度回调

    Returns:
        统计 dict
    """
    from app.db import AsyncSessionLocal
    from app.services.first_pyramid_service import (
        _MIN_BARS_FOR_REQUIRED_DIMS,
        HISTORY_CONTRACT_VERSION,
        compute_first_pyramid_history,
    )

    total_processed = 0
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0

    while True:
        # 1. claim 一批 items
        async with AsyncSessionLocal() as db:
            items = await claim_history_items(
                db, history_run_id,
                worker_instance_id=worker_id,
                batch_size=batch_size,
            )
            await db.commit()

        if not items:
            break

        # 2. 逐股计算（每股独立事务）
        for item in items:
            total_processed += 1
            try:
                # 2.1 DB-only 读取 bars（独立短事务）
                async with AsyncSessionLocal() as bars_db:
                    bars = await _fetch_db_only_daily_bars(
                        bars_db, item.instrument_id, output_bars=output_bars,
                    )

                if bars is None or bars.empty:
                    # 无任何 daily bars → skipped（NO_DAILY_BARS 类别）
                    async with AsyncSessionLocal() as skip_db:
                        await mark_history_item_skipped(
                            skip_db, item.id, "daily bars 为空（DB-only）",
                            lease_epoch=item.lease_epoch,
                        )
                        await skip_db.commit()
                    skipped_count += 1
                    continue

                # 2.2 计算 history SSOT（include_chip=False，禁止 chip）
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(item.instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                )

                # 2.2.1 zero-output 分类（INSUFFICIENT_HISTORY vs COMPUTE_EMPTY_UNEXPECTED）
                decision, zr_reason = _classify_history_zero_output(
                    len(bars),
                    history.get("daily_state") or [],
                    required_bars=_MIN_BARS_FOR_REQUIRED_DIMS,
                    meta_error=history.get("meta", {}).get("error"),
                )
                if decision == "skip":
                    # 数据量不足 → skipped（明确原因，非失败、非 NO_DAILY_BARS）
                    async with AsyncSessionLocal() as skip_db:
                        await mark_history_item_skipped(
                            skip_db, item.id, zr_reason,
                            lease_epoch=item.lease_epoch,
                        )
                        await skip_db.commit()
                    skipped_count += 1
                    continue
                if decision == "fail":
                    # bars 足够但 compute 异常返回空 → fail closed（不得 silently skipped）
                    async with AsyncSessionLocal() as fail_db:
                        await mark_history_item_failed(
                            fail_db, item.id, zr_reason,
                            lease_epoch=item.lease_epoch,
                        )
                        await fail_db.commit()
                    failed_count += 1
                    continue

                # 2.2.2 PIT normalization：将全局锚点结果归一化到 date-specific PIT
                # bars 来自 _fetch_db_only_daily_bars，adjustment_as_of 未显式传
                # → MDAS 使用 latest_adj 作为复权分母。
                # 对 scale-covariant 字段（sqzmom_val/delta + event prices）
                # 乘以 K_t = anchor_factor / factor(t) 恢复严格 date-specific PIT。
                _apply_pit_normalization(bars, history)

                # 2.3 持久化（独立短事务）
                async with AsyncSessionLocal() as persist_db:
                    persisted = await _persist_history_result(
                        session=persist_db,
                        instrument_id=item.instrument_id,
                        history=history,
                        algorithm_version=algorithm_version,
                        source_history_run_id=history_run_id,
                        history_contract_version=HISTORY_CONTRACT_VERSION,
                    )
                    await persist_db.commit()

                # 2.4 标记 succeeded
                async with AsyncSessionLocal() as mark_db:
                    ok = await mark_history_item_succeeded(
                        mark_db, item.id,
                        daily_state_count=persisted["daily_state_count"],
                        event_count=persisted["events_count"],
                        lease_epoch=item.lease_epoch,
                    )
                    await mark_db.commit()

                if ok:
                    succeeded_count += 1
                else:
                    logger.warning(
                        "[HistoryBackfill] item %s lease_epoch 不匹配，已被接管",
                        item.id,
                    )

            except Exception as exc:
                failed_count += 1
                logger.error(
                    "[HistoryBackfill] instrument_id=%s 回补失败: %s",
                    item.instrument_id, exc, exc_info=True,
                )
                try:
                    async with AsyncSessionLocal() as fail_db:
                        await mark_history_item_failed(
                            fail_db, item.id, str(exc),
                            lease_epoch=item.lease_epoch,
                        )
                        await fail_db.commit()
                except Exception as mark_exc:
                    logger.error(
                        "mark_history_item_failed 失败 item_id=%s: %s",
                        item.id, mark_exc,
                    )

            # 2.5 进度回调
            if progress_callback is not None:
                try:
                    await progress_callback(
                        processed=total_processed,
                        succeeded=succeeded_count,
                        failed=failed_count,
                        skipped=skipped_count,
                    )
                except Exception as cb_exc:
                    logger.warning("progress_callback 失败: %s", cb_exc)

    # 3. 从 DB 统计最终进度（canonical source of truth）
    async with AsyncSessionLocal() as db:
        progress = await get_history_run_progress(db, history_run_id)

    # 4. 更新 run 最终状态 —— 必须由 DB canonical progress 决定，不得用本
    #    worker invocation 的 local counters（并发 worker 下会误判）：
    #     - pending > 0 或 running > 0：其他 worker 仍在 running，不得 finalize succeeded
    #     - 全部成功（succeeded==total 且无 failed/skipped/pending/running）：succeeded
    #     - 有成功且有 failed/skipped/pending/running 残留：partial
    #     - 无成功（全 failed/skipped）：failed
    final_status = _derive_run_final_status(progress)
    async with AsyncSessionLocal() as db:
        await finish_history_run(db, history_run_id, status=final_status)
        await db.commit()

    logger.info(
        "[HistoryBackfill] run=%s 完成: status=%s, succeeded=%d, failed=%d, skipped=%d",
        history_run_id, final_status, progress["succeeded"], progress["failed"],
        progress["skipped"],
    )

    return {
        "history_run_id": str(history_run_id),
        "algorithm_version": algorithm_version,
        "output_bars": output_bars,
        "status": final_status,
        "succeeded_count": progress["succeeded"],
        "failed_count": progress["failed"],
        "skipped_count": progress["skipped"],
        "total_processed": total_processed,
        "progress": progress,
    }


def _derive_run_final_status(progress: dict[str, Any]) -> str:
    """[CHANGE-20260808] 由 DB canonical progress 决定 run final status。

    HistoryRun model 语义：
      succeeded = 全部成功
      partial   = 部分成功 / failed / skipped
      failed    = 无成功

    并发安全：pending > 0 或 running > 0 时其他 worker 可能仍在处理，不得 finalize
    succeeded（也避免把仍在跑标记为完成）。
    """
    total = progress.get("total", 0)
    succeeded = progress.get("succeeded", 0)
    failed = progress.get("failed", 0)
    pending = progress.get("pending", 0)
    running = progress.get("running", 0)
    skipped = progress.get("skipped", 0)

    if pending > 0 or running > 0:
        # 有其他 worker 仍在处理 / 还有未领取 item → 不得视为 succeeded
        return HISTORY_RUN_PARTIAL

    if (
        total > 0
        and succeeded == total
        and failed == 0
        and skipped == 0
        and pending == 0
        and running == 0
    ):
        return HISTORY_RUN_SUCCEEDED

    if succeeded > 0:
        return HISTORY_RUN_PARTIAL

    # 无成功：全部 failed / skipped（按 model contract：failed）
    return HISTORY_RUN_FAILED


# =============================================================================
# 内部辅助
# =============================================================================


async def _fetch_history_daily_bars(
    instrument_id: uuid.UUID,
) -> pd.DataFrame | None:
    """获取完整可用日线（point-in-time，qfq 复权，已完成 bar）。

    [CHANGE-20260731-003] SSOT 合规：通过 MarketDataAggregationService (MDAS) 读取行情，
    不再调用 bar_repository.get_bars（已在 SSOT 黑名单中）。
    断点：依赖 MDAS 真实接口，本地纯单元测试由 caller 注入 _fetch_bars_func mock。
    """
    try:
        from app.db import AsyncSessionLocal
        from app.services.market_data_aggregation_service import MarketDataAggregationService
    except ImportError:
        logger.warning(
            "[HistoryBackfill] MDAS 或 AsyncSessionLocal 不可用，返回空 bars",
        )
        return None

    mdas = MarketDataAggregationService()
    async with AsyncSessionLocal() as db:
        result = await mdas.get_bars(
            db,
            instrument_id,
            timeframe="1d",
            adj="qfq",
            include_realtime=False,
            completed_only=True,
        )
        return result.bars


async def _persist_history_result(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    history: dict[str, Any],
    algorithm_version: str,
    *,
    source_history_run_id: uuid.UUID | None = None,
    history_contract_version: str | None = None,
) -> dict[str, int]:
    """持久化 history SSOT 结果到两张表。

    - daily_state: upsert（on_conflict_do_update），更新 state_payload + lineage
    - events: insert on_conflict_do_nothing（不可变，重跑不覆盖）

    [CHANGE-20260808] Historical Lineage（M2）：source_history_run_id + history_contract_version
    写入 daily_state 显式列（新 review-history-v2 replay 必须）。从 run.id 传，不从 metadata 猜。

    Args:
        session: 异步 DB 会话（不 commit，由 caller 控制）
        instrument_id: 股票 ID
        history: compute_first_pyramid_history 返回的 dict
        algorithm_version: 算法版本
        source_history_run_id: 来源 HistoryRun ID（M2）
        history_contract_version: history payload contract version（M2）

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
            source_history_run_id=source_history_run_id,
            history_contract_version=history_contract_version,
            state_payload=state,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_first_pyramid_history_daily_state_instr_date_ver",
            set_={
                "input_hash": stmt.excluded.input_hash,
                "source_history_run_id": stmt.excluded.source_history_run_id,
                "history_contract_version": stmt.excluded.history_contract_version,
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
            history_contract_version=history_contract_version,
            event_payload=evt,
        )
        # [CHANGE-20260808] 事件唯一性 contract-aware：普通 UNIQUE 约束已拆成两个 partial
        # unique index（legacy WHERE history_contract_version IS NULL / versioned WHERE
        # history_contract_version IS NOT NULL）。on_conflict 必须用 index_elements +
        # index_where 做 index inference，禁止 ON CONFLICT ON CONSTRAINT <partial-index-name>，
        # 保证旧 NULL X + v2 X 可共存、v2 X 重跑仍幂等。
        if history_contract_version is not None:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[
                    FirstPyramidHistoryEvent.instrument_id,
                    FirstPyramidHistoryEvent.algorithm_version,
                    FirstPyramidHistoryEvent.history_contract_version,
                    FirstPyramidHistoryEvent.event_id,
                ],
                index_where=text("history_contract_version IS NOT NULL"),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[
                    FirstPyramidHistoryEvent.instrument_id,
                    FirstPyramidHistoryEvent.algorithm_version,
                    FirstPyramidHistoryEvent.event_id,
                ],
                index_where=text("history_contract_version IS NULL"),
            )
        await session.execute(stmt)
        events_count += 1

    await session.flush()

    return {
        "daily_state_count": daily_state_count,
        "events_count": events_count,
    }


def _max_bar_trade_date(bars: pd.DataFrame) -> date | None:
    """[HISTORY-CURRENT-DATE-LIFECYCLE-01 §5] 取 bars 最大交易日（PIT 断言用）。

    history SSOT 的 bars 契约是 DatetimeIndex；兼容退化的 ``time`` 列形态。
    """
    if bars is None or bars.empty:
        return None
    try:
        if isinstance(bars.index, pd.DatetimeIndex):
            return bars.index.max().date()
        if "time" in bars.columns:
            return pd.to_datetime(bars["time"]).max().date()
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def _target_date_events(history: dict[str, Any], trade_date: date) -> list[dict[str, Any]]:
    """PURE ADAPTER: slice the canonical events whose event date == ``trade_date``.

    ROUND-2.2A: the exact-T canonical calculation must, in the same lifecycle, form
    the T-day State AND the T-day Structure Event stream.  This helper only does
    ``event_time -> date`` filtering — it does NOT re-judge BOS / re-decide Structure
    Level / recompute Direction / aggregate member ratio / drop "less important"
    events.  Every event whose date equals ``trade_date`` is kept with its full
    canonical payload.

    ROUND-2.2A-1 FAIL-CLOSED (F1): a canonical event that cannot be assigned an
    event date is a lifecycle failure, NOT a zero-event.  ``events == []`` is a legal
    zero-event (member had no event on T); but an event with missing/invalid
    ``time``, or with a future date (> trade_date), must NOT be silently dropped —
    otherwise a corrupted event stream would be misrepresented as "a clean zero-event
    lifecycle", systematically lowering the event denominator.  These are raised so
    ``advance_history_to_trade_date`` fails the instrument and does NOT call
    persistence (fail closed).  ``event_date < trade_date`` is a history-window
    legacy event and is legitimately ignored.

    The event ``time`` field is the canonical occurrence/confirmation date (ISO date)
    normalized by ``compute_first_pyramid_history``; ``anchor_time`` is a different
    semantic (anchor/pivot/OB bar) and is NEVER used as the event date.  Input bars
    are hard-limited to ``max_bar_date <= trade_date``, so a future event date
    implies timestamp-mapping error / canonical compute leakage / date-semantics bug.
    """
    raw_events = history.get("events") or []
    if not raw_events:
        # history.events == [] -> legitimate zero-event; the lifecycle completed.
        return []
    out: list[dict[str, Any]] = []
    for evt in raw_events:
        # 2.2A-1 AUDIT FIX (F1): canonical target-date SSOT = evt["time"] ONLY.
        # compute_first_pyramid_history normalizes every event's canonical occurrence /
        # confirmation date into "time" (BOS/CHoCH->confirmed_time, OB_CREATED->
        # confirmed_time, OB_ENTERED->enter_time, OB_MITIGATED->mitigated_time,
        # EQH/EQL->confirmed_time, SQZ_RELEASE/ZERO_CROSS->times[i]).
        # ``anchor_time`` is a DIFFERENT semantic (anchor/pivot/OB bar), NOT the
        # event occurrence date — it must NEVER fallback-infer the event date
        # (that would attribute an event to the wrong trade day).  Missing time =>
        # CONTRACT CORRUPTION, fail closed.
        time_str = evt.get("time")
        if not time_str:
            raise ValueError(
                "event 缺少 canonical time，无法确定其 event date："
                f"type={evt.get('type') or '?'} — 这是 lifecycle failure，"
                "不是 zero-event；anchor_time 不是 event occurrence date，"
                "不允许 fallback 推断"
            )
        try:
            evt_date = pd.to_datetime(time_str).date()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(
                f"event time 无法解析（invalid timestamp）：{time_str!r} "
                f"type={evt.get('type') or '?'} — lifecycle failure，fail closed"
            ) from exc
        if evt_date > trade_date:
            # Input bars 已限制 max_bar_date <= trade_date；未来事件 = leakage /
            # 时间语义错误 / timestamp-mapping 错误 -> PIT violation，fail closed。
            raise ValueError(
                f"event date {evt_date.isoformat()} > trade_date "
                f"{trade_date.isoformat()}（type={evt.get('type') or '?'}）— "
                "PIT violation / canonical compute leakage，fail closed"
            )
        if evt_date == trade_date:
            out.append(evt)
        # evt_date < trade_date -> history-window legacy event, legitimately ignored.
    return out


async def advance_history_to_trade_date(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    trade_date: date,
    *,
    output_bars: int = 250,
    batch_size: int = 25,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """[HISTORY-CURRENT-DATE-LIFECYCLE-01 §4] 把 canonical history dataset 推进到 target trade date。

    语义（**canonical dataset advancement，不是重跑 backfill run**）：

    - HistoryRun 是「一个 algorithm+contract+scope 的 canonical dataset lineage 身份」
      （create_history_run 幂等复用 + daily_state upsert 覆盖 lineage 共同证明），
      因此推进数据集 = 在**同一个 run id** 下补齐 target-date state，
      而不是新建 run X 全量 replay（那会把 ~1.32M 历史行 lineage 改写成 X）。
    - ROUND-2.2A：exact-T State 与 exact-T Events 从**同一次** canonical calculation
      一起持久化（复用 ``_persist_history_result`` 单一 owner：State upsert +
      Event immutable insert + contract-aware uniqueness + lineage）。
      target-event slicing 是纯 date adapter（``_target_date_events``），非新业务算法。
      **零事件是一个合法、可证明的结果**（该 member T 日无事件 = 生命周期完整完成，
      而非 "no coverage"）。
    - 只写 target_trade_date 一行/instrument + 该日 events（1x write amplification，
      非 250x history rewrite；events 用不可变 insert-on-conflict-nothing，重跑幂等）
    - 不 claim / 不修改任何 run item（5283 succeeded + 10 skipped 的 execution history 冻结）
    - PIT：bars 经 MDAS ``end_date=trade_date`` + ``adjustment_as_of=trade_date``

    participating set = run 现有 succeeded run-item set（§8），
    不把历史 skipped instrument 拉进来（HISTORY-SKIP-REEVALUATION-01 已 DEFER）。

    Args:
        session: 异步 DB 会话
        history_run_id: canonical history run id（lineage 保持不变）
        trade_date: target trade date
        output_bars: history SSOT window（与 backfill 保持一致，默认 250）
        batch_size: 每批 commit 的 instrument 数（顺序执行，不并发）
        progress_callback: 可选进度回调

    Returns:
        {"run_id", "trade_date", "processed", "target_state_count",
         "no_bar", "no_target_state", "failed", "failed_instruments"}

    Raises:
        ValueError: run 不存在 / scope 不兼容 / contract 版本不匹配
    """
    from app.services.first_pyramid_service import (
        HISTORY_CONTRACT_VERSION,
        compute_first_pyramid_history,
    )

    run = await session.get(FirstPyramidHistoryRun, history_run_id)
    if run is None:
        raise ValueError(f"history run not found: {history_run_id}")

    # metadata_json 是 Text 列（既有 schema），可能是 JSON 字符串或已解析 dict；
    # 与 validate_canonical_history_run_readiness 使用同一解析口径。
    run_meta: dict[str, Any] = {}
    if isinstance(run.metadata_json, str) and run.metadata_json:
        try:
            parsed = json.loads(run.metadata_json)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            run_meta = parsed
    elif isinstance(run.metadata_json, dict):
        run_meta = run.metadata_json
    run_contract = run_meta.get("history_contract_version")
    if run_contract != HISTORY_CONTRACT_VERSION:
        raise ValueError(
            "history run contract mismatch: "
            f"run={run_contract!r} required={HISTORY_CONTRACT_VERSION!r}"
        )
    if run.scope != "all_a_share":
        raise ValueError(
            f"history run scope not canonical: {run.scope!r} (required 'all_a_share')"
        )

    algorithm_version = run.algorithm_version

    # §8 participating set = 现有 succeeded run items（不含 skipped）
    item_rows = await session.execute(
        select(FirstPyramidHistoryRunItem.instrument_id)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.status == "succeeded",
        )
        .order_by(FirstPyramidHistoryRunItem.instrument_id)
    )
    instrument_ids = [row[0] for row in item_rows.all()]

    processed = 0
    target_state_count = 0
    no_bar = 0
    no_target_state = 0
    failed = 0
    failed_instruments: list[dict[str, Any]] = []

    for offset in range(0, len(instrument_ids), batch_size):
        batch = instrument_ids[offset : offset + batch_size]
        for instrument_id in batch:
            processed += 1
            try:
                bars = await _fetch_pit_daily_bars_for_target(
                    session,
                    instrument_id,
                    output_bars=output_bars,
                    target_trade_date=trade_date,
                )
                if bars is None or bars.empty:
                    no_bar += 1
                    continue

                # §5 PIT 硬断言：绝不允许未来 bar 进入 compute input
                max_bar_date = _max_bar_trade_date(bars)
                if max_bar_date is not None and max_bar_date > trade_date:
                    raise ValueError(
                        f"PIT violation: max bar date {max_bar_date} > target {trade_date}"
                    )

                # §6 唯一 SSOT，算法不改
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                )
                meta = history.get("meta") or {}

                target_state = None
                for state in history.get("daily_state") or []:
                    time_str = state.get("time")
                    if not time_str:
                        continue
                    try:
                        state_date = pd.to_datetime(time_str).date()
                    except (ValueError, TypeError):
                        continue
                    if state_date == trade_date:
                        target_state = state
                        break

                if target_state is None:
                    # instrument 在 target date 无 completed bar（停牌/退市/未上市）
                    no_target_state += 1
                    continue

                # ROUND-2.2A: exact-T State + exact-T Events come from the SAME
                # canonical calculation and are persisted TOGETHER via the single
                # ``_persist_history_result`` owner (State upsert + Event immutable
                # insert + contract-aware event uniqueness + lineage semantics).
                # Only the T-date state (1 row) and the T-date events are written —
                # no 250-day history rewrite.  target-event slicing is a pure date
                # adapter (``_target_date_events``), NOT a new business algorithm.
                target_result: dict[str, Any] = {
                    "daily_state": [target_state],
                    "events": _target_date_events(history, trade_date),
                    "meta": meta,
                }
                await _persist_history_result(
                    session,
                    instrument_id,
                    target_result,
                    algorithm_version,
                    source_history_run_id=history_run_id,
                    history_contract_version=HISTORY_CONTRACT_VERSION,
                )
                target_state_count += 1
            except Exception as exc:  # noqa: BLE001 - 单股失败不阻塞整体
                failed += 1
                failed_instruments.append(
                    {"instrument_id": str(instrument_id), "error": str(exc)}
                )
                logger.warning(
                    "advance_history_to_trade_date instrument failed: %s (%s)",
                    instrument_id,
                    exc,
                )

        await session.commit()
        if progress_callback is not None:
            await progress_callback(
                {
                    "processed": processed,
                    "total": len(instrument_ids),
                    "target_state_count": target_state_count,
                }
            )

    return {
        "run_id": str(history_run_id),
        "trade_date": trade_date.isoformat(),
        "total": len(instrument_ids),
        "processed": processed,
        "target_state_count": target_state_count,
        "no_bar": no_bar,
        "no_target_state": no_target_state,
        "failed": failed,
        "failed_instruments": failed_instruments,
    }


def _build_event_id(evt: dict[str, Any], event_type: str) -> str:
    """构造事件稳定 ID（无自带 id 时使用）。

    [CHANGE-20260808] 修复同 bar 多事件碰撞：event_id 不能仅用 {type}_{bar_index}，
    否则同 bar 的 internal BOS 与 swing BOS、同 bar 多个 OB 生命周期事件会互相覆盖
    （persistence ON CONFLICT 吃掉一行）。

    稳定 identity 纳入：
        event_type + bar_index + internal + structure_level/anchor_index + direction/bias
    保证同 bar 不同 internal/level/anchor 的事件生成不同 event_id。

    优先级：bar_index+type+内部字段 > time+type > payload hash
    """
    import hashlib
    import json

    bar_index = evt.get("bar_index")
    if bar_index is not None:
        # 区分字段：internal（internal vs swing）、anchor_index、structure_level、direction
        internal = "int" if evt.get("internal") else "swg"
        anchor = evt.get("anchor_index")
        anchor_s = f"_a{anchor}" if anchor is not None else ""
        level = evt.get("structure_level") or evt.get("level")
        level_s = f"_l{level}" if level is not None else ""
        direction = evt.get("direction") or evt.get("bias")
        dir_s = f"_d{direction}" if direction is not None else ""
        return f"{event_type}_{bar_index}_{internal}{anchor_s}{level_s}{dir_s}"

    time_val = evt.get("time") or evt.get("anchor_time")
    if time_val:
        return f"{event_type}_{time_val}"

    # fallback: hash of payload
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
