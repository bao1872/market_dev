"""XDXR 安全刷新 planner（G1B-3B1）。

纯函数 / 不可变 DTO，无 IO、不连 DB、不依赖具体 scheduler。

设计目标：为下一轮 G1B-3B2（真正削减每日 XDXR 请求）提供可靠事实层。
最终刷新集合（future）：

    必须查 XDXR
    = previous_close 当日异常候选
      ∪ 已知 next_event_date <= trade_date
      ∪ 3 个交易日轮转组
      ∪ schedule metadata 缺失 / 非法

为什么不用自然日轮转：自然日含周末 / 节假日，会破坏「三个交易日完整轮转」语义。
``trade_day_ordinal`` 必须由 scheduler 从 TradingCalendar 取得，本模块只接收 int。

为什么 unknown schedule 仍强制刷新：部署后首次（或 Redis 失效）必须全市场刷新一次，
这是有意的安全 bootstrap；之后约 1/3 + 候选 + 到期事件。绝对不要为了首次看起来快，
就把 unknown 当成 no-event。

常量依据（非硬编码监管规则进业务数学）：沪深权益分派实施公告通常在股权登记日前
3–5 个交易日披露，除权除息日为登记日下一交易日；rotation_size=3 的轮转应在实施公告
到正式除权之间至少完整刷新一次。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.adjustment_factor_service import CorporateActionScheduleState


class PreviousCloseSignal(StrEnum):
    """EOD snapshot ``previous_close`` 相对前一交易日 raw close 的候选信号。

    - NO_ACTION_SIGNAL：两者相等（canonical Decimal 精确比较，无 tolerance）。
    - CORPORATE_ACTION_CANDIDATE：``previous_close`` 与 ``prior_raw_close`` 不相等，
      极可能是除权除息日（交易所规则：除权除息日行情显示的前收盘 = 除权参考价）。
    - UNKNOWN：任一侧缺失 / 非有限 / <= 0，无法判断 → 强制刷新。
    """

    NO_ACTION_SIGNAL = "no_action_signal"
    CORPORATE_ACTION_CANDIDATE = "corporate_action_candidate"
    UNKNOWN = "unknown"


def classify_previous_close_signal(
    *,
    snapshot_previous_close: Decimal | None,
    prior_raw_close: Decimal | None,
) -> PreviousCloseSignal:
    """比较 EOD snapshot ``previous_close`` 与前一交易日 raw close。

    故意不做 tolerance：这是候选检测，不是价格误差比较。只要 canonical Decimal
    不相等就进入 XDXR；即使相等也不能单独证明无事件（仍有 rotation / schedule 两道
    保护）。极小现金分红可能因价格显示精度导致 previous_close 显示值恰好等于昨日收盘，
    因此 UNKNOWN / 相等都不能单独排除事件。
    """
    if (
        snapshot_previous_close is None
        or prior_raw_close is None
        or not snapshot_previous_close.is_finite()
        or not prior_raw_close.is_finite()
        or snapshot_previous_close <= 0
        or prior_raw_close <= 0
    ):
        return PreviousCloseSignal.UNKNOWN

    if snapshot_previous_close != prior_raw_close:
        return PreviousCloseSignal.CORPORATE_ACTION_CANDIDATE

    return PreviousCloseSignal.NO_ACTION_SIGNAL


def rotation_bucket(symbol: str, rotation_size: int = 3) -> int:
    """稳定 rotation bucket（跨进程 / 跨调用确定性）。

    禁止用 ``hash(symbol)``（Python hash 进程间不稳定）。
    用 ``sha256(symbol)`` 前 8 字节对 ``rotation_size`` 取模。
    """
    if rotation_size <= 0:
        raise ValueError(f"rotation_size 必须 > 0，got {rotation_size}")
    digest = hashlib.sha256(symbol.encode("ascii")).digest()
    value = int.from_bytes(digest[:8], "big")
    return value % rotation_size


@dataclass(frozen=True)
class XdxrRefreshDecision:
    """单只标的 XDXR 刷新决策（不可变）。"""

    symbol: str
    refresh: bool
    reasons: tuple[str, ...]


def plan_xdxr_refresh(
    *,
    symbol: str,
    trade_date: date,
    trade_day_ordinal: int,
    schedule: CorporateActionScheduleState | None,
    schedule_age_trade_days: int | None,
    previous_close_signal: PreviousCloseSignal,
    rotation_size: int = 3,
) -> XdxrRefreshDecision:
    """计算单只标的是否需要在 ``trade_date`` 拉取 XDXR。

    refresh 当且仅当存在任一原因（reasons 非空）：

    - ``schedule_unknown``：schedule metadata 缺失 / 无法证明
    - ``schedule_invalid_future_scan``：``scanned_as_of`` 晚于 trade_date（异常）
    - ``schedule_age_unknown``：schedule 存在但交易日 age 无法证明（调用方未提供 /
      负数 / 非 int 如 bool）→ 不能假设 rotation 已执行，强制刷新
    - ``schedule_stale``：schedule 已 ``>= rotation_size`` 个交易日未更新（服务停跑 /
      scheduler 未执行 / 部署中断）→ 必须补刷新，不能依赖「本应执行但实际没执行」的
      rotation
    - ``known_event_due``：已知 ``next_event_date <= trade_date``（含 missed / delayed）
    - ``previous_close_mismatch``：previous_close 与 prior_raw_close 不等
    - ``previous_close_unknown``：previous_close 信号无法判断
    - ``rotation_refresh``：当前 rotation bucket 命中

    ``schedule is None`` → 强制刷新（安全 bootstrap）。

    ``schedule_age_trade_days`` 含义：**当前 trade_date 与 ``schedule.scanned_as_of``
    之间经过的 A 股交易日数量**（同日=0，前一交易日=1，...）。planner 自己不计算，
    下一轮 scheduler 用 TradingCalendar 批量提供。禁止用
    ``(trade_date - scanned_as_of).days`` 自然日（周末 / 节假日会破坏 rotation 语义）。

    恢复语义：正常连续运行约 1/3 / 天；停跑 ``>= rotation_size`` 个交易日后首次恢复，
    stale 股票强制补刷新，而不是继续等待 symbol 自己的 bucket。
    """
    reasons: list[str] = []

    if schedule is None:
        reasons.append("schedule_unknown")
    else:
        if schedule.scanned_as_of > trade_date:
            reasons.append("schedule_invalid_future_scan")
        else:
            if schedule.scanned_as_of == trade_date:
                # 同日扫描：age 可直接证明为 0，无需 calendar，即使 age=None。
                effective_age = 0
            elif (
                type(schedule_age_trade_days) is not int
                or schedule_age_trade_days < 0
            ):
                effective_age = None
                reasons.append("schedule_age_unknown")
            else:
                effective_age = schedule_age_trade_days

            if effective_age is not None and effective_age >= rotation_size:
                reasons.append("schedule_stale")

        if (
            schedule.next_event_date is not None
            and schedule.next_event_date <= trade_date
        ):
            reasons.append("known_event_due")

    if previous_close_signal is PreviousCloseSignal.CORPORATE_ACTION_CANDIDATE:
        reasons.append("previous_close_mismatch")
    elif previous_close_signal is PreviousCloseSignal.UNKNOWN:
        reasons.append("previous_close_unknown")

    if rotation_bucket(symbol, rotation_size) == trade_day_ordinal % rotation_size:
        reasons.append("rotation_refresh")

    return XdxrRefreshDecision(
        symbol=symbol,
        refresh=bool(reasons),
        reasons=tuple(reasons),
    )
