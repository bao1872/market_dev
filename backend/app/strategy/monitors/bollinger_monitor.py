"""Bollinger Band 穿越监控插件（M2）。

从 ref/交易/app/monitoring.py detect_bb_signals() 迁移核心算法，重构为持续监控逻辑：
- 日线计算布林带参考线（upper/mid/lower），取上一根已完成 bar 的值
- 1 分钟 K 线检测穿越事件（上轨/中轨/下轨穿越）

调用 features/ 算法（严格不修改 features/）：
- bollinger: 计算布林带 mid/upper/lower 序列

输入：MarketDataContext（bars_daily 日线 + bars_minute 1m OHLCV bars）
输出：MonitorState（bb_upper/bb_mid/bb_lower/current_price/prev_close/bb_width/bb_pos）
      + StrategyEventDraft（bb_upper_touch/bb_mid_touch/bb_lower_touch 事件）

事件检测：
- bb_upper_touch: prev_close < ref_upper <= cur_close（从下方穿越上轨）
- bb_mid_touch: prev_close 和 cur_close 分列中轨两侧
- bb_lower_touch: prev_close > ref_lower >= cur_close（从上方穿越下轨）
- dedupe: {event_type}:{instrument_id}:{boundary}:{bar_time_key}
- state_ttl=600s: 冷却时间 10 分钟

用法（模块自测）：
    python -m app.strategy.monitors.bollinger_monitor
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.models.strategy import StrategyVersion
from app.strategy._plotly_mock import ensure_plotly_mock
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)

logger = logging.getLogger("strategy.monitors.bollinger_monitor")

# 导入 features/ 算法（从包内 app.strategy_assets.algorithms.features，Docker 兼容）
ensure_plotly_mock()
from app.strategy_assets.algorithms.features.bollinger_features_plotly import bollinger as _bollinger_func

# BB 标准参数（与 monitoring.py 一致）
BB_WIN_DEFAULT = 20
BB_K_DEFAULT = 2.0

# 事件类型常量
BB_UPPER_TOUCH = "bb_upper_touch"
BB_MID_TOUCH = "bb_mid_touch"
BB_LOWER_TOUCH = "bb_lower_touch"

# 冷却时间（秒）
NOTIFY_COOLDOWN_SECONDS = 600


def _get_completed_bar_index(df: pd.DataFrame, freq: str = "d") -> int:
    """判断 DataFrame 中最后一根已完成 bar 的 iloc 索引。

    对于日线：如果最后一根 bar 是今天且未收盘(15:00)，则取 iloc[-2]；
    否则取 iloc[-1]。

    Args:
        df: K线 DataFrame（index 为 datetime）
        freq: 周期，目前仅支持 "d"

    Returns:
        最后一根已完成 bar 的 iloc 索引（-1 或 -2）
    """
    if df.empty:
        return -1

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    last_ts = df.index[-1]

    if freq == "d":
        if last_ts.date() == now.date():
            if last_ts.hour == 15 and last_ts.minute == 0:
                return -1
            elif now.time() < time(15, 5):
                return -2 if len(df) >= 2 else -1
        elif last_ts.date() < now.date():
            return -1

    return -1


def _calc_deviation_pct(price: float, boundary: float) -> float | None:
    """计算价格偏离边界的百分比。

    正值=价格在边界上方，负值=价格在边界下方。
    """
    if boundary is None or boundary == 0:
        return None
    return round((price - boundary) / boundary * 100, 2)


class BollingerMonitor(StrategyRuntime):
    """Bollinger Band 穿越分钟监控策略（kind="monitor"）。

    按 1m bar 持续监控价格与布林带参考线的穿越关系，
    输出当前状态（MonitorState）与穿越事件（StrategyEventDraft）。

    生命周期：
    1. StrategyLoader.load(version) 创建实例
    2. initialize(version) 从 manifest 提取参数 + 懒加载 features 模块
    3. calculate_state(context) 每个 bar 计算当前状态
    4. detect_events(context, prev, curr) 对比前后状态检测穿越事件
    """

    kind = "monitor"

    def __init__(self) -> None:
        self._bb_win: int = BB_WIN_DEFAULT
        self._bb_k: float = BB_K_DEFAULT
        self._strategy_version_id: UUID | None = None

    async def initialize(self, version: StrategyVersion) -> None:
        """从 manifest 提取参数。

        Args:
            version: 策略版本 ORM 对象（manifest 含 parameters/outputs/event_types）
        """
        self._strategy_version_id = version.id
        manifest = version.manifest

        # 提取 BB 参数
        for param in manifest.get("parameters", []):
            key = param.get("key")
            if key == "bb_win":
                self._bb_win = int(param.get("default", BB_WIN_DEFAULT))
            elif key == "bb_k":
                self._bb_k = float(param.get("default", BB_K_DEFAULT))

        logger.info(
            "BollingerMonitor 初始化: bb_win=%d, bb_k=%.1f",
            self._bb_win, self._bb_k,
        )

    async def execute(self, context: MarketDataContext) -> Any:  # type: ignore[override]
        """selector 执行接口（monitor 不支持）。"""
        raise NotImplementedError(
            "BollingerMonitor 是 monitor 策略，不支持 execute（请使用 calculate_state + detect_events）"
        )

    async def calculate_state(self, context: MarketDataContext) -> MonitorState:
        """计算当前 bar 的布林带监控状态。

        从日线 bars 计算 BB 参考线，取上一根已完成 bar 的值；
        从 1m bars 获取 prev_close 和 current_price。

        Args:
            context: 市场数据上下文（bars_daily + bars_minute）

        Returns:
            当前 bar 的监控状态

        Raises:
            ValueError: 数据不足
            RuntimeError: features 模块未加载或 BB 计算失败
        """
        if context.bars_daily is None or len(context.bars_daily) < self._bb_win + 5:
            raise ValueError(
                f"日线数据不足（需要至少 {self._bb_win + 5} 根），"
                f"instrument_id={context.instrument_id}"
            )

        daily_df = context.bars_daily

        # 计算 BB 参考线
        try:
            bb_mid, bb_upper, bb_lower = _bollinger_func(
                daily_df, self._bb_win, self._bb_k
            )
        except Exception as e:
            raise RuntimeError(
                f"bollinger 计算失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # 附加 BB 列到临时 DataFrame
        tmp = daily_df.copy()
        tmp["bb_mid"] = bb_mid
        tmp["bb_upper"] = bb_upper
        tmp["bb_lower"] = bb_lower

        # 取上一根已完成 bar 的参考线值
        ref_idx = _get_completed_bar_index(tmp, "d")
        ref = tmp.iloc[ref_idx]

        ref_upper = float(ref["bb_upper"]) if pd.notna(ref["bb_upper"]) else None
        ref_mid = float(ref["bb_mid"]) if pd.notna(ref["bb_mid"]) else None
        ref_lower = float(ref["bb_lower"]) if pd.notna(ref["bb_lower"]) else None

        # 从 1m bars 获取价格
        current_price = None
        prev_close = None
        bb_width = None
        bb_pos = None

        if context.bars_minute is not None and len(context.bars_minute) >= 1:
            current_price = round(float(context.bars_minute["close"].iloc[-1]), 4)
            if len(context.bars_minute) >= 2:
                prev_close = round(float(context.bars_minute["close"].iloc[-2]), 4)

        # 计算 bb_width 和 bb_pos
        if ref_mid is not None and ref_upper is not None and ref_lower is not None:
            bb_width = round((ref_upper - ref_lower) / ref_mid, 6) if ref_mid != 0 else None
            if current_price is not None and (ref_upper - ref_lower) != 0:
                bb_pos = round((current_price - ref_lower) / (ref_upper - ref_lower), 4)
            else:
                bb_pos = None

        state: dict[str, Any] = {
            "bb_upper": ref_upper,
            "bb_mid": ref_mid,
            "bb_lower": ref_lower,
            "current_price": current_price,
            "prev_close": prev_close,
            "bb_width": bb_width,
            "bb_pos": bb_pos,
        }

        bar_time = context.bar_time or (
            context.bars_minute.index[-1].to_pydatetime()
            if context.bars_minute is not None
            and isinstance(context.bars_minute.index, pd.DatetimeIndex)
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
        """检测布林带穿越事件。

        三种穿越类型：
        - bb_upper_touch: prev_close < ref_upper <= cur_close
        - bb_mid_touch: prev_close 和 cur_close 分列中轨两侧
        - bb_lower_touch: prev_close > ref_lower >= cur_close

        Args:
            context: 市场数据上下文
            prev_state: 前一状态（首个 bar 时为 None）
            curr_state: 当前状态

        Returns:
            事件草稿列表
        """
        state = curr_state.state
        ref_upper = state.get("bb_upper")
        ref_mid = state.get("bb_mid")
        ref_lower = state.get("bb_lower")
        current_price = state.get("current_price")
        prev_close = state.get("prev_close")

        # 数据不足时无法检测穿越
        if prev_close is None or current_price is None:
            return []

        events: list[StrategyEventDraft] = []
        bar_time = curr_state.updated_at or datetime.now(UTC)
        # [bollinger_monitor] - dedupe_key 使用整分钟时间戳（而非微秒精度），
        # 同一 1m bar 内多次调用不会产生不同 dedupe_key
        bar_time_key = bar_time.strftime("%Y%m%d%H%M") if isinstance(bar_time, datetime) else str(bar_time)
        instrument_id_str = str(curr_state.instrument_id)

        # bb_snapshot 公共字段
        bb_snapshot: dict[str, Any] = {
            "bb_upper": ref_upper,
            "bb_mid": ref_mid,
            "bb_lower": ref_lower,
            "prev_close": prev_close,
            "current_price": current_price,
        }

        # 上轨穿越：prev_close < ref_upper <= current_price
        if ref_upper is not None and prev_close < ref_upper <= current_price:
            dev_pct = _calc_deviation_pct(current_price, ref_upper)
            dedupe_key = f"{BB_UPPER_TOUCH}:{instrument_id_str}:{ref_upper}:{bar_time_key}"
            events.append(StrategyEventDraft(
                event_type=BB_UPPER_TOUCH,
                event_time=bar_time,
                dedupe_key=dedupe_key,
                logical_entity=f"{instrument_id_str}:{ref_upper}",
                payload={
                    "trigger_type": BB_UPPER_TOUCH,
                    "price": current_price,
                    "boundary": ref_upper,
                    "dev_pct": dev_pct,
                    "bb_snapshot": bb_snapshot,
                },
                state_ttl_seconds=NOTIFY_COOLDOWN_SECONDS,
            ))

        # 中轨穿越：prev_close 和 current_price 分列中轨两侧
        if ref_mid is not None:
            mid_cross = (prev_close <= ref_mid < current_price) or (current_price <= ref_mid < prev_close)
            if mid_cross:
                dev_pct = _calc_deviation_pct(current_price, ref_mid)
                dedupe_key = f"{BB_MID_TOUCH}:{instrument_id_str}:{ref_mid}:{bar_time_key}"
                events.append(StrategyEventDraft(
                    event_type=BB_MID_TOUCH,
                    event_time=bar_time,
                    dedupe_key=dedupe_key,
                    logical_entity=f"{instrument_id_str}:{ref_mid}",
                    payload={
                        "trigger_type": BB_MID_TOUCH,
                        "price": current_price,
                        "boundary": ref_mid,
                        "dev_pct": dev_pct,
                        "bb_snapshot": bb_snapshot,
                    },
                    state_ttl_seconds=NOTIFY_COOLDOWN_SECONDS,
                ))

        # 下轨穿越：prev_close > ref_lower >= current_price
        if ref_lower is not None and prev_close > ref_lower >= current_price:
            dev_pct = _calc_deviation_pct(current_price, ref_lower)
            dedupe_key = f"{BB_LOWER_TOUCH}:{instrument_id_str}:{ref_lower}:{bar_time_key}"
            events.append(StrategyEventDraft(
                event_type=BB_LOWER_TOUCH,
                event_time=bar_time,
                dedupe_key=dedupe_key,
                logical_entity=f"{instrument_id_str}:{ref_lower}",
                payload={
                    "trigger_type": BB_LOWER_TOUCH,
                    "price": current_price,
                    "boundary": ref_lower,
                    "dev_pct": dev_pct,
                    "bb_snapshot": bb_snapshot,
                },
                state_ttl_seconds=NOTIFY_COOLDOWN_SECONDS,
            ))

        return events

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """计算布林带图表指标（供个股详情页面使用）。

        从日线 bars 计算 BB 参考线序列，返回每根 bar 的 upper/mid/lower 值。

        Returns:
            {"bb_upper": [...], "bb_mid": [...], "bb_lower": [...],
             "bb_width": [...], "bb_pos": [...]}
        """
        bars = context.bars_daily
        if bars is None or len(bars) < self._bb_win + 5:
            return {"bb_upper": [], "bb_mid": [], "bb_lower": [],
                    "bb_width": [], "bb_pos": []}

        try:
            bb_mid, bb_upper, bb_lower = _bollinger_func(
                bars, self._bb_win, self._bb_k
            )
        except Exception as e:
            raise RuntimeError(
                f"bollinger 计算失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # bb_width = (upper - lower) / mid
        bb_width = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)

        # bb_pos = (close - lower) / (upper - lower)
        denom = (bb_upper - bb_lower).replace(0, np.nan)
        bb_pos = (bars["close"] - bb_lower) / denom

        return {
            "bb_upper": bb_upper.round(4).tolist(),
            "bb_mid": bb_mid.round(4).tolist(),
            "bb_lower": bb_lower.round(4).tolist(),
            "bb_width": bb_width.round(6).tolist(),
            "bb_pos": bb_pos.round(4).tolist(),
        }


if __name__ == "__main__":
    # 自测入口：验证 bollinger 模块加载与信号检测（无副作用，不写库表）
    import asyncio
    import sys
    from uuid import uuid4

    print(f"BollingerMonitor.kind={BollingerMonitor.kind}")
    assert BollingerMonitor.kind == "monitor"

    # 验证 bollinger 函数可调用
    try:
        print(f"bollinger features 模块已通过包内导入加载")
        assert callable(_bollinger_func)
        print("bollinger() 函数可用 ✓")
    except ImportError as e:
        print(f"bollinger features 模块不可用（跳过后续测试）: {e}")
        print("OK（部分跳过）")
        sys.exit(0)

    # 构造合成数据测试信号检测
    np.random.seed(42)
    n_daily = 60
    dates = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=n_daily)
    # 生成带趋势的日线数据
    close_prices = 100.0 + np.cumsum(np.random.randn(n_daily) * 0.5)
    daily_df = pd.DataFrame(
        {
            "open": close_prices - np.random.rand(n_daily) * 0.3,
            "high": close_prices + np.abs(np.random.randn(n_daily)) * 0.5,
            "low": close_prices - np.abs(np.random.randn(n_daily)) * 0.5,
            "close": close_prices,
            "volume": np.random.randint(1000, 10000, n_daily).astype(float),
        },
        index=dates,
    )

    # 计算 BB 参考线
    bb_mid, bb_upper, bb_lower = _bollinger_func(daily_df, BB_WIN_DEFAULT, BB_K_DEFAULT)
    print(f"BB 参考线计算成功: mid[-1]={bb_mid.iloc[-1]:.4f}, "
          f"upper[-1]={bb_upper.iloc[-1]:.4f}, lower[-1]={bb_lower.iloc[-1]:.4f}")

    # 构造 1m bars（模拟穿越上轨场景）
    ref_upper_val = float(bb_upper.iloc[-2])  # 取上一根已完成 bar
    ref_mid_val = float(bb_mid.iloc[-2])
    ref_lower_val = float(bb_lower.iloc[-2])

    # prev_close 在上轨下方，cur_close 在上轨上方 → 触发 bb_upper_touch
    m1_times = pd.date_range(end=pd.Timestamp.now(), periods=2, freq="1min")
    m1_df = pd.DataFrame(
        {
            "open": [ref_upper_val - 0.5, ref_upper_val + 0.1],
            "high": [ref_upper_val - 0.3, ref_upper_val + 0.3],
            "low": [ref_upper_val - 0.8, ref_upper_val - 0.1],
            "close": [ref_upper_val - 0.5, ref_upper_val + 0.1],  # prev < upper <= cur
            "volume": [1000.0, 1200.0],
        },
        index=m1_times,
    )

    # 构造 MarketDataContext
    from datetime import date as date_type

    ctx = MarketDataContext(
        instrument_id=uuid4(),
        symbol="TEST01",
        bars_daily=daily_df,
        bars_minute=m1_df,
        trade_date=date_type.today(),
        bar_time=m1_times[-1].to_pydatetime(),
    )

    # 创建 monitor 实例（跳过 initialize，bollinger 函数已通过包内导入可用）
    monitor = BollingerMonitor()
    monitor._strategy_version_id = uuid4()

    # 测试 calculate_state
    state_result = asyncio.run(monitor.calculate_state(ctx))
    print(f"calculate_state 成功: bb_upper={state_result.state['bb_upper']:.4f}, "
          f"bb_mid={state_result.state['bb_mid']:.4f}, "
          f"bb_lower={state_result.state['bb_lower']:.4f}, "
          f"current_price={state_result.state['current_price']}, "
          f"prev_close={state_result.state['prev_close']}")

    # 测试 detect_events
    events = asyncio.run(monitor.detect_events(ctx, None, state_result))
    print(f"detect_events 检测到 {len(events)} 个事件")
    for ev in events:
        print(f"  event_type={ev.event_type}, boundary={ev.payload.get('boundary'):.4f}, "
              f"price={ev.payload.get('price')}, dev_pct={ev.payload.get('dev_pct')}")

    # 验证上轨穿越事件
    assert len(events) >= 1, f"期望至少 1 个事件，实际 {len(events)}"
    upper_events = [e for e in events if e.event_type == BB_UPPER_TOUCH]
    assert len(upper_events) == 1, f"期望 1 个 bb_upper_touch 事件，实际 {len(upper_events)}"
    print("bb_upper_touch 事件检测 ✓")

    # 测试中轨穿越场景
    m1_df_mid = pd.DataFrame(
        {
            "open": [ref_mid_val - 0.5, ref_mid_val + 0.1],
            "high": [ref_mid_val - 0.3, ref_mid_val + 0.3],
            "low": [ref_mid_val - 0.8, ref_mid_val - 0.1],
            "close": [ref_mid_val - 0.5, ref_mid_val + 0.1],  # prev < mid < cur
            "volume": [1000.0, 1200.0],
        },
        index=m1_times,
    )
    ctx_mid = MarketDataContext(
        instrument_id=uuid4(),
        symbol="TEST02",
        bars_daily=daily_df,
        bars_minute=m1_df_mid,
        trade_date=date_type.today(),
        bar_time=m1_times[-1].to_pydatetime(),
    )
    state_mid = asyncio.run(monitor.calculate_state(ctx_mid))
    events_mid = asyncio.run(monitor.detect_events(ctx_mid, None, state_mid))
    mid_events = [e for e in events_mid if e.event_type == BB_MID_TOUCH]
    assert len(mid_events) == 1, f"期望 1 个 bb_mid_touch 事件，实际 {len(mid_events)}"
    print("bb_mid_touch 事件检测 ✓")

    # 测试下轨穿越场景
    m1_df_lower = pd.DataFrame(
        {
            "open": [ref_lower_val + 0.5, ref_lower_val - 0.1],
            "high": [ref_lower_val + 0.8, ref_lower_val + 0.1],
            "low": [ref_lower_val + 0.3, ref_lower_val - 0.3],
            "close": [ref_lower_val + 0.5, ref_lower_val - 0.1],  # prev > lower >= cur
            "volume": [1000.0, 1200.0],
        },
        index=m1_times,
    )
    ctx_lower = MarketDataContext(
        instrument_id=uuid4(),
        symbol="TEST03",
        bars_daily=daily_df,
        bars_minute=m1_df_lower,
        trade_date=date_type.today(),
        bar_time=m1_times[-1].to_pydatetime(),
    )
    state_lower = asyncio.run(monitor.calculate_state(ctx_lower))
    events_lower = asyncio.run(monitor.detect_events(ctx_lower, None, state_lower))
    lower_events = [e for e in events_lower if e.event_type == BB_LOWER_TOUCH]
    assert len(lower_events) == 1, f"期望 1 个 bb_lower_touch 事件，实际 {len(lower_events)}"
    print("bb_lower_touch 事件检测 ✓")

    # 测试无穿越场景
    m1_df_none = pd.DataFrame(
        {
            "open": [ref_mid_val + 1.0, ref_mid_val + 1.2],
            "high": [ref_mid_val + 1.5, ref_mid_val + 1.5],
            "low": [ref_mid_val + 0.8, ref_mid_val + 1.0],
            "close": [ref_mid_val + 1.0, ref_mid_val + 1.2],  # 都在中轨上方，无穿越
            "volume": [1000.0, 1200.0],
        },
        index=m1_times,
    )
    ctx_none = MarketDataContext(
        instrument_id=uuid4(),
        symbol="TEST04",
        bars_daily=daily_df,
        bars_minute=m1_df_none,
        trade_date=date_type.today(),
        bar_time=m1_times[-1].to_pydatetime(),
    )
    state_none = asyncio.run(monitor.calculate_state(ctx_none))
    events_none = asyncio.run(monitor.detect_events(ctx_none, None, state_none))
    assert len(events_none) == 0, f"期望 0 个事件，实际 {len(events_none)}"
    print("无穿越场景 ✓")

    # 验证 ABC 继承
    assert issubclass(BollingerMonitor, StrategyRuntime)
    print("BollingerMonitor 继承 StrategyRuntime ✓")

    print("OK")
