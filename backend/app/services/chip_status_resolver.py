"""[CHANGE-20260730-010] 共享 chip_status 解析器。

抽取自 market_stocks_service._build_chip_status_struct 和 first-pyramid 路由的 chip 查询逻辑，
供 /market/stocks 列表 API 与 /first-pyramid 详情 API 共同使用。

严格五元组匹配：
- instrument_id
- trade_date == run.trade_date
- core_run_id == run.id
- algorithm_version == CHIP_CONSENSUS_ALGORITHM_VERSION
- status == succeeded（仅查询 succeeded 记录用于 chip 数据读取；
  chip_status 状态查询则放宽 status，扫描所有状态记录）

返回 ChipStatus schema（camelCase），列表/详情 API 序列化结果完全一致。

000021 深科技场景：
- chip_row 存在但 status=skipped, payload.reason=M15_BARS_INSUFFICIENT
- actual_bars=354, required_bars=500
- 返回 state=unavailable, reasonCode=M15_BARS_INSUFFICIENT,
  reasonText="15分钟数据不足（354根，需≥500；4000根为完整质量门槛）",
  actualBars=354, requiredBars=500, fullQualityBars=4000
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import NODE_CLUSTER_LOW_BARS
from app.core.time import to_shanghai_iso
from app.schemas.first_pyramid import CHIP_CONSENSUS_ALGORITHM_VERSION, ChipStatus

logger = logging.getLogger("chip_status_resolver")

# 最低 15m bar 门槛（与 after_close_chip_consensus_service._CHIP_MIN_15M_BARS 一致）
# 个股详情/列表 chip_status 共用此门槛；4000 为完整质量门槛
_CHIP_MIN_15M_BARS = 500

# 完整质量门槛（Node Cluster 完整 15m bar 数 = DAILY_HISTORY_BARS * 16）
_FULL_QUALITY_15M_BARS = NODE_CLUSTER_LOW_BARS


async def resolve_chip_status(
    session: AsyncSession,
    instrument_id: UUID,
    trade_date: date,
    snapshot_run_id: UUID,
    algorithm_version: str = CHIP_CONSENSUS_ALGORITHM_VERSION,
) -> ChipStatus:
    """查询 chip 记录并构建结构化状态（共享给 /market/stocks 和 /first-pyramid）。

    严格匹配五元组（instrument_id + trade_date + core_run_id + algorithm_version），
    取最新一条记录（按 created_at DESC），根据其 status 和 payload 构建状态：

    - 无记录 → pending（chip job 尚未执行）
    - status=succeeded + chip.available=True → ready
    - status=succeeded + chip.available=False → unavailable (NO_VALID_PEAK)
    - status=skipped + M15_BARS_INSUFFICIENT → unavailable + actualBars/requiredBars/fullQualityBars
    - status=skipped + 其他 reason → unavailable + reasonText
    - status=failed → failed (CHIP_JOB_FAILED)
    - 其他未知 status → failed

    Args:
        session: 主业务库只读 session
        instrument_id: 股票 instrument_id
        trade_date: 快照 run.trade_date
        snapshot_run_id: 快照 run.id（core_run_id 严格匹配）
        algorithm_version: chip 算法版本（默认 CHIP_CONSENSUS_ALGORITHM_VERSION）

    Returns:
        ChipStatus schema 实例（camelCase，含诊断字段）
    """
    # 延迟导入避免循环依赖
    from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot

    chip_stmt = (
        select(StockChipConsensusSnapshot)
        .where(
            StockChipConsensusSnapshot.instrument_id == instrument_id,
            StockChipConsensusSnapshot.trade_date == trade_date,
            StockChipConsensusSnapshot.core_run_id == snapshot_run_id,
            StockChipConsensusSnapshot.algorithm_version == algorithm_version,
        )
        .order_by(StockChipConsensusSnapshot.created_at.desc())
        .limit(1)
    )
    chip_result = await session.execute(chip_stmt)
    chip_row = chip_result.scalar_one_or_none()

    if chip_row is None:
        # 无任何 chip 记录：chip job 尚未执行
        return ChipStatus(
            state="pending",
            reasonCode="CHIP_JOB_PENDING",
            reasonText="筹码任务尚未执行",
            computedAt=None,
        )

    return _build_chip_status_from_row(chip_row)


def _build_chip_status_from_row(chip_row: Any) -> ChipStatus:
    """从 chip_row 构建 ChipStatus schema 实例。

    Args:
        chip_row: StockChipConsensusSnapshot ORM 对象（含 status, chip_payload,
            error_message, created_at）

    Returns:
        ChipStatus schema 实例
    """
    status = chip_row.status
    payload = chip_row.chip_payload or {}
    error_message = chip_row.error_message
    created_at = chip_row.created_at

    # 从 payload 提取诊断字段
    reason_code = payload.get("reason") if isinstance(payload, dict) else None
    actual_bars = payload.get("actual_bars") if isinstance(payload, dict) else None

    # error_message 兜底解析 reason_code
    if reason_code is None and error_message:
        if "15m" in error_message.lower():
            reason_code = "M15_BARS_INSUFFICIENT"
        elif "daily" in error_message.lower() or "insufficient_daily" in error_message.lower():
            reason_code = "DAILY_BARS_INSUFFICIENT"
        elif "profile_empty" in error_message.lower():
            reason_code = "NO_VALID_PEAK"
        else:
            reason_code = "CHIP_JOB_FAILED"

    computed_at_iso = to_shanghai_iso(created_at) if created_at else None

    if status == "succeeded":
        # 检查 chip 维度是否可用
        chip_dim = payload.get("chip") if isinstance(payload, dict) else None
        chip_available = bool(
            chip_dim is not None
            and isinstance(chip_dim, dict)
            and chip_dim.get("available") is True
        )
        if chip_available:
            return ChipStatus(
                state="ready",
                reasonCode=None,
                reasonText="已计算",
                computedAt=computed_at_iso,
            )
        # succeeded 但 chip 不可用：PROFILE_EMPTY
        return ChipStatus(
            state="unavailable",
            reasonCode="NO_VALID_PEAK",
            reasonText="Node Cluster 无有效峰",
            computedAt=computed_at_iso,
        )

    if status == "skipped":
        if reason_code == "M15_BARS_INSUFFICIENT":
            actual = actual_bars if isinstance(actual_bars, int) else None
            reason_text = (
                f"15分钟数据不足（{actual}根，需≥{_CHIP_MIN_15M_BARS}；"
                f"{_FULL_QUALITY_15M_BARS}根为完整质量门槛）"
                if actual is not None
                else f"15分钟数据不足（需≥{_CHIP_MIN_15M_BARS}；"
                f"{_FULL_QUALITY_15M_BARS}根为完整质量门槛）"
            )
            return ChipStatus(
                state="unavailable",
                reasonCode="M15_BARS_INSUFFICIENT",
                reasonText=reason_text,
                computedAt=computed_at_iso,
                actualBars=actual,
                requiredBars=_CHIP_MIN_15M_BARS,
                fullQualityBars=_FULL_QUALITY_15M_BARS,
            )
        if reason_code == "DAILY_BARS_INSUFFICIENT":
            actual = actual_bars if isinstance(actual_bars, int) else None
            reason_text = (
                f"日线数据不足（{actual}根）" if actual is not None else "日线数据不足"
            )
            return ChipStatus(
                state="unavailable",
                reasonCode="DAILY_BARS_INSUFFICIENT",
                reasonText=reason_text,
                computedAt=computed_at_iso,
                actualBars=actual,
            )
        # 其他 skipped 原因
        return ChipStatus(
            state="unavailable",
            reasonCode=reason_code,
            reasonText=error_message or "已跳过",
            computedAt=computed_at_iso,
        )

    if status == "failed":
        return ChipStatus(
            state="failed",
            reasonCode="CHIP_JOB_FAILED",
            reasonText=error_message or "计算失败",
            computedAt=computed_at_iso,
        )

    # [QM-63 chip 七态 2026-08-04] 中断态：被取消或 Worker 接管而未完成。
    # 与 failed 区分——中断不是计算错误，重跑即可恢复，不应展示"计算失败"。
    if status in ("interrupted", "cancelled"):
        return ChipStatus(
            state="interrupted",
            reasonCode="CHIP_JOB_INTERRUPTED",
            reasonText=error_message or "筹码任务被中断，等待重新计算",
            computedAt=computed_at_iso,
        )

    # [QM-63 chip 七态 2026-08-04] 运行中：与 pending 区分开的进行态
    if status in ("running", "queued", "pending"):
        return ChipStatus(
            state="pending",
            reasonCode="CHIP_JOB_PENDING",
            reasonText="筹码任务进行中",
            computedAt=computed_at_iso,
        )

    # 未知 status：兜底 failed
    logger.warning(
        "[chip_status_resolver] 未知 chip status=%s, instrument_id=%s",
        status,
        getattr(chip_row, "instrument_id", None),
    )
    return ChipStatus(
        state="failed",
        reasonCode="CHIP_JOB_FAILED",
        reasonText=f"未知状态: {status}",
        computedAt=computed_at_iso,
    )


def build_chip_status_from_realtime(chip_realtime: Any | None) -> ChipStatus:
    """从实时计算的 ChipConsensusResult 构建 ChipStatus（兼容旧路径）。

    用于 first_pyramid_service 实时计算场景（无 chip_row，仅有 ChipConsensusResult）。

    Args:
        chip_realtime: ChipConsensusResult 或 None

    Returns:
        ChipStatus schema 实例
    """
    if chip_realtime is None:
        return ChipStatus(
            state="pending",
            reasonCode="CHIP_JOB_PENDING",
            reasonText="筹码任务尚未执行",
            computedAt=None,
        )

    error = chip_realtime.error
    chip_dim = chip_realtime.chip
    daily_count = chip_realtime.dailyBarsCount
    bars_15m_count = chip_realtime.bars15mCount

    if error is None:
        # 无错误：检查 chip 是否可用
        if chip_dim is not None and chip_dim.available:
            return ChipStatus(
                state="ready",
                reasonCode=None,
                reasonText="已计算",
                computedAt=None,
            )
        return ChipStatus(
            state="unavailable",
            reasonCode="NO_VALID_PEAK",
            reasonText="Node Cluster 无有效峰",
            computedAt=None,
        )

    # 有 error：按错误类型映射
    if error == "INSUFFICIENT_DAILY_BARS":
        return ChipStatus(
            state="unavailable",
            reasonCode="DAILY_BARS_INSUFFICIENT",
            reasonText=f"日线数据不足（{daily_count}根）",
            computedAt=None,
            actualBars=daily_count,
        )
    if error in ("INPUT_CONTRACT_VIOLATION", "MISSING_15M_BARS", "INSUFFICIENT_15M_HISTORY"):
        return ChipStatus(
            state="unavailable",
            reasonCode="M15_BARS_INSUFFICIENT",
            reasonText=(
                f"15分钟数据不足（{bars_15m_count}根，需≥{_CHIP_MIN_15M_BARS}；"
                f"{_FULL_QUALITY_15M_BARS}根为完整质量门槛）"
            ),
            computedAt=None,
            actualBars=bars_15m_count,
            requiredBars=_CHIP_MIN_15M_BARS,
            fullQualityBars=_FULL_QUALITY_15M_BARS,
        )
    if error == "PROFILE_EMPTY":
        return ChipStatus(
            state="unavailable",
            reasonCode="NO_VALID_PEAK",
            reasonText="Node Cluster 无有效峰",
            computedAt=None,
        )

    # 未知 error
    return ChipStatus(
        state="failed",
        reasonCode="CHIP_JOB_FAILED",
        reasonText=str(error),
        computedAt=None,
    )
