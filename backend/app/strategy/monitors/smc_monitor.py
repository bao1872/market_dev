"""SMC 日线结构盘中监控插件（M2）。

[CHANGE-20260720-002 §二] 新增 SmcMonitor - 日线 SMC 结构盘中监控。

设计原则（PROMPT.md §二）：
- 三种监控全部以已完成前复权日线为主结构；Node 的 15m 只负责成交量分配；最新已完成 1m 仅检测触发。
- 禁止复制 SMC 公式，调用现有 Canonical SMC Adapter（compute_smc_adapter）。
- 继续排除 FVG（adapter 内已防御性过滤）。
- EQH/EQL 和 Strong/Weak 结构只显示不通知。
- 每个对象生成稳定 smc_entity_id，dedupe 含 instrument/event_type/entity/touch_episode，禁止历史事件重复补发。

输入：MarketDataContext（bars_daily + bars_minute）
  - bars_daily: 已完成 qfq 日线，主结构（BOS/CHoCH/OB 都基于日线）
  - bars_minute: 1m bars，仅用于检测当前价是否触及/穿越日线结构位

输出：MonitorState（smc_confirmed_bos/smc_confirmed_choch/smc_active_obs/
                   smc_current_price/smc_currently_touched/smc_swing_bias/smc_trailing）
      + StrategyEventDraft（smc_bos_retest/smc_choch_retest/smc_order_block_first_touch 事件）

三个 V1 事件类型：
- smc_bos_retest: 1m price 回踩或穿越已确认日线 BOS level
- smc_choch_retest: 1m price 回踩或穿越已确认日线 CHoCH level
- smc_order_block_first_touch: 1m price 第一次进入当前有效未mitigated日线 OB

touch_episode dedupe 机制：
- prev_state.state["smc_episode_tracker"] 保存每个 entity 的 episode 计数和 last_touched 状态
- detect_events 对比 prev/curr 的 touch status：
  - curr touched 且 prev 未 touched（或无 prev）：新 episode = prev_episode+1，触发事件
  - curr touched 且 prev 已 touched：同 episode，不触发（dedupe）
  - curr 未 touched：更新 tracker last_touched=False（保留 episode 计数）
- detect_events 直接 mutate curr_state.state["smc_episode_tracker"]，
  MonitorBatchService 在 detect_events 后保存 curr_state.state 到 DB。

smc_entity_id 设计（稳定标识，跨 bar 一致）：
- BOS:   f"BOS:{anchor_index}:{level}"
- CHoCH: f"CHoCH:{anchor_index}:{level}"
- OB:    f"OB:{anchor_index}:{bar_high}:{bar_low}:{bias}"

dedupe_key: f"{event_type}:{instrument_id_str}:{smc_entity_id}:{touch_episode}"

用法（模块自测）：
    python -m app.strategy.monitors.smc_monitor
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pandas as pd

from app.constants.indicator_contract import NODE_CLUSTER_EVENT_TTL_SECONDS
from app.models.strategy import StrategyVersion
from app.services.canonical_adapters import compute_smc_adapter
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)

logger = logging.getLogger("strategy.monitors.smc_monitor")

# 事件类型常量（V1 SMC 监控事件）
SMC_BOS_RETEST = "smc_bos_retest"
SMC_CHOCH_RETEST = "smc_choch_retest"
SMC_ORDER_BLOCK_FIRST_TOUCH = "smc_order_block_first_touch"

# 事件冷却时间（秒）：与 BB/Node 一致，由 indicator_contract 唯一真源控制
NOTIFY_COOLDOWN_SECONDS = NODE_CLUSTER_EVENT_TTL_SECONDS  # 600

# State 字段键
STATE_SMC_CONFIRMED_BOS = "smc_confirmed_bos"
STATE_SMC_CONFIRMED_CHOCH = "smc_confirmed_choch"
STATE_SMC_ACTIVE_OBS = "smc_active_obs"
STATE_SMC_CURRENT_PRICE = "smc_current_price"
STATE_SMC_CURRENTLY_TOUCHED = "smc_currently_touched"
STATE_SMC_SWING_BIAS = "smc_swing_bias"
STATE_SMC_TRAILING = "smc_trailing"
STATE_SMC_EPISODE_TRACKER = "smc_episode_tracker"
STATE_SMC_AVAILABILITY = "smc_availability"
STATE_SMC_DEGRADED_REASON = "smc_degraded_reason"

# 触发检测的最小 1m bars 数（需要 prev_close + cur_close）
_MIN_MINUTE_BARS_FOR_TRIGGER = 2


def _make_bos_entity_id(anchor_index: int | None, level: float | None) -> str:
    """BOS 稳定 entity_id。

    Args:
        anchor_index: BOS anchor bar 索引（在日线序列中的位置）
        level: BOS level 价格

    Returns:
        "BOS:{anchor_index}:{level}" 字符串
    """
    return f"BOS:{anchor_index}:{level}"


def _make_choch_entity_id(anchor_index: int | None, level: float | None) -> str:
    """CHoCH 稳定 entity_id。"""
    return f"CHoCH:{anchor_index}:{level}"


def _make_ob_entity_id(
    anchor_index: int | None,
    bar_high: float | None,
    bar_low: float | None,
    bias: int | None,
) -> str:
    """OB 稳定 entity_id。"""
    return f"OB:{anchor_index}:{bar_high}:{bar_low}:{bias}"


def _is_bos_touched(prev_close: float, cur_close: float, level: float) -> bool:
    """BOS level 触碰检测：1m close 穿越 level（任一方向）。

    与 BollingerMonitor 穿越检测一致：
    - 从下方穿越：prev_close < level <= cur_close
    - 从上方穿越：prev_close > level >= cur_close
    """
    return (prev_close < level <= cur_close) or (prev_close > level >= cur_close)


def _is_choch_touched(prev_close: float, cur_close: float, level: float) -> bool:
    """CHoCH level 触碰检测（与 BOS 一致，穿越即触碰）。"""
    return _is_bos_touched(prev_close, cur_close, level)


def _is_ob_touched(
    prev_close: float, cur_close: float, bar_high: float, bar_low: float
) -> bool:
    """OB zone 触碰检测：1m close 进入 [bar_low, bar_high] 区间。

    首次进入：prev_close 在 zone 外，cur_close 在 zone 内（含边界）。
    """
    prev_in_zone = bar_low <= prev_close <= bar_high
    cur_in_zone = bar_low <= cur_close <= bar_high
    return (not prev_in_zone) and cur_in_zone


class SmcMonitor(StrategyRuntime):
    """SMC 日线结构盘中监控策略（kind="monitor"）。

    按 1m bar 持续监控价格与日线 SMC 结构位（BOS/CHoCH level + 未mitigated OB zone）
    的触碰关系，输出当前状态（MonitorState）与触碰事件（StrategyEventDraft）。

    生命周期：
    1. StrategyLoader.load(version) 创建实例
    2. initialize(version) 绑定策略版本
    3. calculate_state(context) 每个 bar 计算当前 SMC 结构 + touch status
    4. detect_events(context, prev, curr) 对比 prev/curr touch status 检测触碰事件

    主结构：context.bars_daily（已完成 qfq 日线）
    触发：context.bars_minute（1m bars，仅取最后两根的 close 做穿越检测）
    """

    kind = "monitor"

    def __init__(self) -> None:
        self._strategy_version_id: UUID | None = None
        # SMC 结果缓存：供 detect_events 复用 calculate_state 的计算结果
        self._last_smc_dto: dict[str, Any] | None = None
        self._last_smc_calc_id: str | None = None

    async def initialize(self, version: StrategyVersion) -> None:
        """初始化监控器，绑定策略版本。

        SMC 参数由 smc_pine_core.DEFAULT_PARAMS 唯一控制（已逐项匹配 Pine），
        不从 manifest 覆盖。

        Args:
            version: 策略版本 ORM 对象
        """
        self._strategy_version_id = version.id
        logger.info("SmcMonitor 初始化完成（SMC 参数由 smc_pine_core.DEFAULT_PARAMS 控制）")

    async def execute(self, context: MarketDataContext) -> Any:  # type: ignore[override]
        """selector 执行接口（monitor 不支持）。"""
        raise NotImplementedError(
            "SmcMonitor 是 monitor 策略，不支持 execute（请使用 calculate_state + detect_events）"
        )

    async def calculate_state(self, context: MarketDataContext) -> MonitorState:
        """计算当前 bar 的 SMC 监控状态。

        调用 compute_smc_adapter(context.bars_daily, display_bars=len(daily))
        获取完整 SMC DTO（不裁剪，保留所有已确认事件）。

        从 DTO 提取：
        - smc_confirmed_bos: 已确认 BOS 事件列表
        - smc_confirmed_choch: 已确认 CHoCH 事件列表
        - smc_active_obs: 当前有效未mitigated OB 列表（mitigated_index is None）
        - smc_current_price: 当前 1m close（无 1m 时取日线最后 close）
        - smc_currently_touched: dict[smc_entity_id -> bool]，当前 1m 是否触碰
        - smc_swing_bias: 透传 swing_bias（Strong/Weak 显示用，不通知）
        - smc_trailing: 透传 trailing（显示用，不通知）

        Args:
            context: 市场数据上下文（bars_daily + bars_minute）

        Returns:
            当前 bar 的监控状态

        Raises:
            ValueError: bars_daily 为 None 或数据不足
        """
        bars_daily = context.bars_daily
        if bars_daily is None or bars_daily.empty:
            raise ValueError(
                f"SmcMonitor 需要 daily bars 数据，instrument_id={context.instrument_id}"
            )

        # SMC 需要足够 warmup（ATR200 + swings_length=50），最少 250 根
        if len(bars_daily) < 250:
            raise ValueError(
                f"daily bars 数据不足（需要至少 250 根，实际 {len(bars_daily)}），"
                f"instrument_id={context.instrument_id}"
            )

        # 调用 Canonical SMC Adapter（不裁剪，display_bars=全部）
        try:
            smc_dto = compute_smc_adapter(
                bars_daily,
                display_bars=len(bars_daily),
            )
        except Exception as e:
            raise RuntimeError(
                f"compute_smc_adapter 失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # 缓存 SMC DTO 供 detect_events 复用
        calc_id = (
            f"{context.instrument_id}:"
            f"{context.bar_time.isoformat() if context.bar_time else 'unknown'}"
        )
        self._last_smc_dto = smc_dto
        self._last_smc_calc_id = calc_id

        # 提取已确认 BOS/CHoCH 事件
        all_events = smc_dto.get("events", [])
        confirmed_bos = [e for e in all_events if e.get("type") == "BOS"]
        confirmed_choch = [e for e in all_events if e.get("type") == "CHoCH"]

        # 提取当前有效未mitigated OB（mitigated_index is None）
        all_obs = smc_dto.get("order_blocks", [])
        active_obs = [ob for ob in all_obs if ob.get("mitigated_index") is None]

        # 当前价：优先从 1m bars 取最后一根 close，否则从日线取
        if context.bars_minute is not None and not context.bars_minute.empty:
            current_price = float(context.bars_minute["close"].iloc[-1])
        else:
            current_price = float(bars_daily["close"].iloc[-1])

        # 计算 currently_touched：当前 1m 是否触碰每个 entity
        # 需要 1m bars 的 prev_close + cur_close 做穿越检测
        currently_touched: dict[str, bool] = {}
        prev_close: float | None = None
        cur_close: float | None = None
        if (
            context.bars_minute is not None
            and len(context.bars_minute) >= _MIN_MINUTE_BARS_FOR_TRIGGER
        ):
            prev_close = float(context.bars_minute["close"].iloc[-2])
            cur_close = float(context.bars_minute["close"].iloc[-1])

        if prev_close is not None and cur_close is not None:
            # BOS level 触碰检测
            for bos in confirmed_bos:
                level = bos.get("level")
                anchor_idx = bos.get("anchor_index")
                if level is None or anchor_idx is None:
                    continue
                entity_id = _make_bos_entity_id(anchor_idx, float(level))
                currently_touched[entity_id] = _is_bos_touched(
                    prev_close, cur_close, float(level)
                )

            # CHoCH level 触碰检测
            for choch in confirmed_choch:
                level = choch.get("level")
                anchor_idx = choch.get("anchor_index")
                if level is None or anchor_idx is None:
                    continue
                entity_id = _make_choch_entity_id(anchor_idx, float(level))
                currently_touched[entity_id] = _is_choch_touched(
                    prev_close, cur_close, float(level)
                )

            # OB zone 触碰检测
            for ob in active_obs:
                anchor_idx = ob.get("anchor_index")
                bar_high = ob.get("bar_high")
                bar_low = ob.get("bar_low")
                bias = ob.get("bias")
                if (
                    anchor_idx is None
                    or bar_high is None
                    or bar_low is None
                    or bias is None
                ):
                    continue
                entity_id = _make_ob_entity_id(
                    int(anchor_idx), float(bar_high), float(bar_low), int(bias)
                )
                currently_touched[entity_id] = _is_ob_touched(
                    prev_close, cur_close, float(bar_high), float(bar_low)
                )

        # 构造精简的 state（避免存储完整 DTO，仅存监控必需字段）
        # BOS/CHoCH 只保留监控必需字段：anchor_index/anchor_time/level/bias/internal/bullish
        def _slim_event(e: dict[str, Any]) -> dict[str, Any]:
            return {
                "anchor_index": e.get("anchor_index"),
                "anchor_time": e.get("anchor_time"),
                "confirmed_index": e.get("confirmed_index"),
                "confirmed_time": e.get("confirmed_time"),
                "level": e.get("level"),
                "bias": e.get("bias"),
                "internal": e.get("internal"),
                "bullish": e.get("bullish"),
            }

        # OB 只保留监控必需字段
        def _slim_ob(ob: dict[str, Any]) -> dict[str, Any]:
            return {
                "anchor_index": ob.get("anchor_index"),
                "anchor_time": ob.get("anchor_time"),
                "confirmed_index": ob.get("confirmed_index"),
                "confirmed_time": ob.get("confirmed_time"),
                "bar_high": ob.get("bar_high"),
                "bar_low": ob.get("bar_low"),
                "bias": ob.get("bias"),
                "internal": ob.get("internal"),
                "clipped_left": ob.get("clipped_left", False),
            }

        state: dict[str, Any] = {
            STATE_SMC_CONFIRMED_BOS: [_slim_event(e) for e in confirmed_bos],
            STATE_SMC_CONFIRMED_CHOCH: [_slim_event(e) for e in confirmed_choch],
            STATE_SMC_ACTIVE_OBS: [_slim_ob(ob) for ob in active_obs],
            STATE_SMC_CURRENT_PRICE: round(current_price, 4) if current_price else None,
            STATE_SMC_CURRENTLY_TOUCHED: currently_touched,
            STATE_SMC_SWING_BIAS: smc_dto.get("swing_bias", 0),
            STATE_SMC_TRAILING: smc_dto.get("trailing", {}),
            STATE_SMC_AVAILABILITY: "available",
            STATE_SMC_DEGRADED_REASON: None,
            # smc_episode_tracker 由 detect_events mutate（首次 calculate_state 时为空 dict）
            STATE_SMC_EPISODE_TRACKER: {},
        }

        bar_time = context.bar_time or (
            bars_daily.index[-1].to_pydatetime()
            if isinstance(bars_daily.index, pd.DatetimeIndex)
            else datetime.now(UTC)
        )

        return MonitorState(
            instrument_id=context.instrument_id,
            strategy_version_id=self._strategy_version_id,  # type: ignore[arg-type]
            state=state,
            state_version=1,
            updated_at=bar_time,
        )

    async def detect_events(
        self,
        context: MarketDataContext,
        prev_state: MonitorState | None,
        curr_state: MonitorState,
    ) -> list[StrategyEventDraft]:
        """检测 SMC 触碰事件（touch_episode dedupe）。

        三个事件类型：
        - smc_bos_retest: 1m price 穿越/回踩已确认日线 BOS level（新 episode 触发）
        - smc_choch_retest: 1m price 穿越/回踩已确认日线 CHoCH level（新 episode 触发）
        - smc_order_block_first_touch: 1m price 首次进入当前有效未mitigated日线 OB zone（新 episode 触发）

        touch_episode 机制：
        - prev_state.state["smc_episode_tracker"] 保存每个 entity 的 episode 计数和 last_touched
        - curr touched 且 prev 未 touched（或无 prev）：新 episode = prev_episode+1，触发事件
        - curr touched 且 prev 已 touched：同 episode，不触发（dedupe）
        - curr 未 touched：更新 tracker last_touched=False（保留 episode 计数供未来 episode）
        - detect_events 直接 mutate curr_state.state["smc_episode_tracker"]，
          MonitorBatchService 在 detect_events 后保存 curr_state.state 到 DB。

        Args:
            context: 市场数据上下文
            prev_state: 前一状态（首个 bar 时为 None）
            curr_state: 当前状态（含 smc_currently_touched）

        Returns:
            事件草稿列表
        """
        curr_state_dict = curr_state.state
        currently_touched: dict[str, bool] = curr_state_dict.get(
            STATE_SMC_CURRENTLY_TOUCHED, {}
        )
        if not currently_touched:
            # 无触碰：仍需更新 tracker，确保 last_touched=False
            prev_t = (
                prev_state.state.get(STATE_SMC_EPISODE_TRACKER, {}) if prev_state else {}
            )
            curr_state_dict[STATE_SMC_EPISODE_TRACKER] = (
                self._update_tracker_all_untouched(prev_t)
            )
            return []

        # 加载 prev tracker
        prev_tracker: dict[str, Any] = (
            prev_state.state.get(STATE_SMC_EPISODE_TRACKER, {}) if prev_state else {}
        )

        # 加载 SMC 结构（从 curr_state 提取，用于构造 payload）
        confirmed_bos = curr_state_dict.get(STATE_SMC_CONFIRMED_BOS, [])
        confirmed_choch = curr_state_dict.get(STATE_SMC_CONFIRMED_CHOCH, [])
        active_obs = curr_state_dict.get(STATE_SMC_ACTIVE_OBS, [])
        bos_by_entity = {
            _make_bos_entity_id(e.get("anchor_index"), e.get("level")): e
            for e in confirmed_bos
            if e.get("anchor_index") is not None and e.get("level") is not None
        }
        choch_by_entity = {
            _make_choch_entity_id(e.get("anchor_index"), e.get("level")): e
            for e in confirmed_choch
            if e.get("anchor_index") is not None and e.get("level") is not None
        }
        ob_by_entity = {
            _make_ob_entity_id(
                ob.get("anchor_index"),
                ob.get("bar_high"),
                ob.get("bar_low"),
                ob.get("bias"),
            ): ob
            for ob in active_obs
            if (
                ob.get("anchor_index") is not None
                and ob.get("bar_high") is not None
                and ob.get("bar_low") is not None
                and ob.get("bias") is not None
            )
        }

        bar_time = curr_state.updated_at or datetime.now(UTC)
        # dedupe_key 使用整分钟时间戳（与 BB/Node 一致），同一 1m bar 内多次调用不产生不同 key
        bar_time_key = (
            bar_time.strftime("%Y%m%d%H%M")
            if isinstance(bar_time, datetime)
            else str(bar_time)
        )
        instrument_id_str = str(curr_state.instrument_id)
        current_price = curr_state_dict.get(STATE_SMC_CURRENT_PRICE)

        events: list[StrategyEventDraft] = []
        # 复制 prev_tracker 作为基础，逐项更新
        updated_tracker: dict[str, Any] = {
            entity: dict(info) for entity, info in prev_tracker.items()
        }

        # 遍历所有 curr touched 的 entity，检测新 episode
        for entity_id, is_touched in currently_touched.items():
            if not is_touched:
                # 未触碰：更新 last_touched=False，保留 episode 计数
                if entity_id in updated_tracker:
                    updated_tracker[entity_id]["last_touched"] = False
                else:
                    updated_tracker[entity_id] = {
                        "episode": 0,
                        "last_touched": False,
                    }
                continue

            # 当前触碰：检查 prev 是否已 touched
            prev_info = prev_tracker.get(entity_id, {"episode": 0, "last_touched": False})
            prev_episode = int(prev_info.get("episode", 0))
            prev_last_touched = bool(prev_info.get("last_touched", False))

            if prev_last_touched:
                # 同 episode，不触发（dedupe）
                updated_tracker[entity_id] = {
                    "episode": prev_episode,
                    "last_touched": True,
                }
                continue

            # 新 episode：prev_episode + 1，触发事件
            new_episode = prev_episode + 1
            updated_tracker[entity_id] = {
                "episode": new_episode,
                "last_touched": True,
            }

            # 根据 entity_id 前缀判断事件类型并构造 payload
            event_draft = self._build_event_draft(
                entity_id=entity_id,
                event_type=self._resolve_event_type(entity_id),
                instrument_id_str=instrument_id_str,
                bar_time=bar_time,
                bar_time_key=bar_time_key,
                touch_episode=new_episode,
                current_price=current_price,
                bos_by_entity=bos_by_entity,
                choch_by_entity=choch_by_entity,
                ob_by_entity=ob_by_entity,
            )
            if event_draft is not None:
                events.append(event_draft)

        # Mutate curr_state.state["smc_episode_tracker"]（MonitorBatchService 会保存 curr_state.state）
        curr_state_dict[STATE_SMC_EPISODE_TRACKER] = updated_tracker

        return events

    @staticmethod
    def _resolve_event_type(entity_id: str) -> str:
        """根据 entity_id 前缀解析事件类型。

        Args:
            entity_id: BOS:... / CHoCH:... / OB:...

        Returns:
            smc_bos_retest / smc_choch_retest / smc_order_block_first_touch

        Raises:
            ValueError: entity_id 前缀无法识别
        """
        if entity_id.startswith("BOS:"):
            return SMC_BOS_RETEST
        if entity_id.startswith("CHoCH:"):
            return SMC_CHOCH_RETEST
        if entity_id.startswith("OB:"):
            return SMC_ORDER_BLOCK_FIRST_TOUCH
        raise ValueError(f"无法识别的 smc_entity_id 前缀: {entity_id}")

    @staticmethod
    def _build_event_draft(
        entity_id: str,
        event_type: str,
        instrument_id_str: str,
        bar_time: datetime,
        bar_time_key: str,
        touch_episode: int,
        current_price: float | None,
        bos_by_entity: dict[str, dict[str, Any]],
        choch_by_entity: dict[str, dict[str, Any]],
        ob_by_entity: dict[str, dict[str, Any]],
    ) -> StrategyEventDraft | None:
        """构造 SMC 事件草稿。

        Args:
            entity_id: 稳定标识（BOS:.../CHoCH:.../OB:...）
            event_type: smc_bos_retest/smc_choch_retest/smc_order_block_first_touch
            instrument_id_str: 标的 UUID 字符串
            bar_time: 事件发生时间（bar 时间）
            bar_time_key: 整分钟时间戳（用于 dedupe_key）
            touch_episode: 本次触碰的 episode 编号
            current_price: 当前 1m close
            bos_by_entity/choch_by_entity/ob_by_entity: entity_id → 结构 dict

        Returns:
            StrategyEventDraft 或 None（结构缺失时）
        """
        dedupe_key = f"{event_type}:{instrument_id_str}:{entity_id}:{touch_episode}"
        logical_entity = f"{instrument_id_str}:{entity_id}"

        # 构造 payload（自包含，不依赖外部状态）
        payload: dict[str, Any] = {
            "instrument_id": instrument_id_str,
            "smc_entity_id": entity_id,
            "event_type": event_type,
            "touch_episode": touch_episode,
            "current_price": current_price,
            "bar_time": bar_time_key,
            # [CHANGE-20260720-003 §三] 贯穿全链的 indicator_view
            "indicator_view": "smc",
        }

        if event_type == SMC_BOS_RETEST:
            bos = bos_by_entity.get(entity_id)
            if bos is None:
                return None
            payload["level"] = bos.get("level")
            payload["anchor_index"] = bos.get("anchor_index")
            payload["anchor_time"] = bos.get("anchor_time")
            payload["bias"] = bos.get("bias")
            payload["internal"] = bos.get("internal")
            payload["bullish"] = bos.get("bullish")
        elif event_type == SMC_CHOCH_RETEST:
            choch = choch_by_entity.get(entity_id)
            if choch is None:
                return None
            payload["level"] = choch.get("level")
            payload["anchor_index"] = choch.get("anchor_index")
            payload["anchor_time"] = choch.get("anchor_time")
            payload["bias"] = choch.get("bias")
            payload["internal"] = choch.get("internal")
            payload["bullish"] = choch.get("bullish")
        elif event_type == SMC_ORDER_BLOCK_FIRST_TOUCH:
            ob = ob_by_entity.get(entity_id)
            if ob is None:
                return None
            payload["bar_high"] = ob.get("bar_high")
            payload["bar_low"] = ob.get("bar_low")
            payload["anchor_index"] = ob.get("anchor_index")
            payload["anchor_time"] = ob.get("anchor_time")
            payload["bias"] = ob.get("bias")
            payload["internal"] = ob.get("internal")

        return StrategyEventDraft(
            event_type=event_type,
            event_time=bar_time,
            dedupe_key=dedupe_key,
            logical_entity=logical_entity,
            payload=payload,
            state_ttl_seconds=NOTIFY_COOLDOWN_SECONDS,
        )

    @staticmethod
    def _update_tracker_all_untouched(
        prev_tracker: dict[str, Any],
    ) -> dict[str, Any]:
        """当 curr 无任何触碰时，更新 tracker：所有 entity last_touched=False，保留 episode 计数。

        Args:
            prev_tracker: 前 tracker

        Returns:
            更新后的 tracker
        """
        updated: dict[str, Any] = {}
        for entity_id, info in prev_tracker.items():
            updated[entity_id] = {
                "episode": int(info.get("episode", 0)),
                "last_touched": False,
            }
        return updated

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """计算 SMC 图表指标（供个股详情页面使用）。

        调用 compute_smc_adapter(context.bars_daily, display_bars=len(daily))
        返回完整 SMC DTO（events/order_blocks/equal_highs_lows/trailing/swing_bias/
        pivots/time/params/view）。

        FVG 完全排除（adapter 内已防御性过滤）。

        Args:
            context: 市场数据上下文

        Returns:
            完整 SMC DTO 字典
        """
        bars_daily = context.bars_daily
        if bars_daily is None or len(bars_daily) < 250:
            return {
                "events": [],
                "order_blocks": [],
                "equal_highs_lows": [],
                "trailing": {},
                "swing_bias": 0,
                "pivots": [],
                "time": [],
                "params": {},
                "view": {
                    "total_bars": 0,
                    "display_bars": 0,
                    "offset": 0,
                    "window_start": 0,
                    "window_end": 0,
                },
            }

        try:
            smc_dto = compute_smc_adapter(
                bars_daily,
                display_bars=len(bars_daily),
            )
        except Exception as e:
            raise RuntimeError(
                f"compute_smc_adapter 失败（compute_indicators）"
                f"instrument_id={context.instrument_id}: {e}"
            ) from e

        return smc_dto


if __name__ == "__main__":
    # 自测入口：验证 SmcMonitor 定义与基本行为（无副作用，不写库表）
    print(f"SmcMonitor.kind={SmcMonitor.kind}")
    assert SmcMonitor.kind == "monitor"

    # 验证 ABC 继承
    assert issubclass(SmcMonitor, StrategyRuntime)
    print("SmcMonitor 继承 StrategyRuntime ✓")

    # 验证事件类型常量
    assert SMC_BOS_RETEST == "smc_bos_retest"
    assert SMC_CHOCH_RETEST == "smc_choch_retest"
    assert SMC_ORDER_BLOCK_FIRST_TOUCH == "smc_order_block_first_touch"
    print("事件类型常量 ✓")

    # 验证 entity_id 生成
    assert _make_bos_entity_id(100, 10.5) == "BOS:100:10.5"
    assert _make_choch_entity_id(200, 9.8) == "CHoCH:200:9.8"
    assert _make_ob_entity_id(150, 11.0, 10.0, 1) == "OB:150:11.0:10.0:1"
    print("entity_id 生成 ✓")

    # 验证 touch 检测
    assert _is_bos_touched(9.5, 10.5, 10.0) is True  # 从下方穿越
    assert _is_bos_touched(10.5, 9.5, 10.0) is True  # 从上方穿越
    assert _is_bos_touched(9.5, 9.8, 10.0) is False  # 未穿越
    assert _is_bos_touched(10.5, 10.8, 10.0) is False  # 都在上方
    print("BOS/CHoCH touch 检测 ✓")

    # OB touch 检测
    assert _is_ob_touched(9.0, 10.5, 11.0, 10.0) is True  # 从下方进入 zone
    assert _is_ob_touched(11.5, 10.5, 11.0, 10.0) is True  # 从上方进入 zone
    assert _is_ob_touched(10.5, 10.8, 11.0, 10.0) is False  # 已在 zone 内
    assert _is_ob_touched(9.0, 9.5, 11.0, 10.0) is False  # 都在 zone 外
    print("OB touch 检测 ✓")

    # 验证 _resolve_event_type
    assert SmcMonitor._resolve_event_type("BOS:100:10.5") == SMC_BOS_RETEST
    assert SmcMonitor._resolve_event_type("CHoCH:200:9.8") == SMC_CHOCH_RETEST
    assert SmcMonitor._resolve_event_type("OB:150:11.0:10.0:1") == SMC_ORDER_BLOCK_FIRST_TOUCH
    print("_resolve_event_type ✓")

    # 验证 _update_tracker_all_untouched
    prev_tracker = {
        "BOS:100:10.5": {"episode": 3, "last_touched": True},
        "OB:150:11.0:10.0:1": {"episode": 1, "last_touched": True},
    }
    updated = SmcMonitor._update_tracker_all_untouched(prev_tracker)
    assert updated["BOS:100:10.5"]["episode"] == 3
    assert updated["BOS:100:10.5"]["last_touched"] is False
    assert updated["OB:150:11.0:10.0:1"]["episode"] == 1
    assert updated["OB:150:11.0:10.0:1"]["last_touched"] is False
    print("_update_tracker_all_untouched ✓")

    print("OK")
