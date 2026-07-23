"""五类盘中 SMC 监控永久回归测试。

验证 PROMPT.md §三 / PRD V2.0 §3.2 要求：SmcMonitor 五类事件
（BOS / CHoCH / EQH / EQL / 订单块触碰）的：
1. 方向（bullish/bearish）与结构级别（internal/swing）在 event_id 与 payload 中保留。
2. 最新已完成 1m 的 high-low 与日线 level/zone 相交才触发。
3. 同 touch_episode 不重复触发（dedupe）。
4. EQH/EQL payload 保留 eqhl_type 与 second_pivot_time。

测试策略：
- 不使用生产 DB / Token / Secret，不写 /tmp 脚本。
- monkeypatch smc_monitor.compute_smc_adapter 返回可控 SMC DTO（含已知方向/级别的
  BOS/CHoCH/EQH/EQL/OB），避免依赖真实 SMC 算法产线。
- 构造 MarketDataContext（bars_daily >= 250 + bars_minute 1m）。
- 真实调用 SmcMonitor.calculate_state + detect_events，断言事件 payload 与 dedupe。

运行：
    APP_ENV=test pytest tests/test_smc_monitor_five_event_types.py -v
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.strategy.monitors import smc_monitor
from app.strategy.monitors.smc_monitor import SmcMonitor
from app.strategy.runtime import MarketDataContext

TEST_INSTRUMENT = uuid.UUID("00000000-0000-0000-0000-068850600000")
STRATEGY_VERSION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# =============================================================================
# 合成行情数据
# =============================================================================
def _build_daily_bars(n_bars: int = 260, seed: int = 42) -> pd.DataFrame:
    """生成确定性日线 bars（>= 250 满足 SmcMonitor 数据门槛）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end="2026-07-18", periods=n_bars, freq="B")
    close = np.array([20.0 + i * 0.01 for i in range(n_bars)])
    df = pd.DataFrame({
        "open": close - 0.05,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": rng.uniform(1_000_000, 5_000_000, size=n_bars),
        "amount": close * 1_000_000,
        "adj_factor": [1.0] * n_bars,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _build_minute_bars(high: float, low: float, close: float) -> pd.DataFrame:
    """构造 1m bars（最后一根为指定 high/low/close 的触发 bar）。"""
    dates = pd.date_range(end="2026-07-18 14:30", periods=2, freq="1min")
    df = pd.DataFrame({
        "open": [close - 0.1, close - 0.05],
        "high": [high - 0.5, high],
        "low": [low + 0.5, low],
        "close": [close - 0.1, close],
        "volume": [50000, 80000],
        "amount": [close * 50000, close * 80000],
    }, index=dates)
    df.index.name = "datetime"
    return df


# =============================================================================
# 可控 SMC DTO
# =============================================================================
def _build_controlled_smc_dto() -> dict:
    """构造覆盖 bull/bear × internal/swing + EQH/EQL 的可控 SMC DTO。

    levels/zones 设计（便于 1m high-low 相交测试）：
    - BOS bullish internal:  level=10.0
    - BOS bearish swing:     level=20.0
    - CHoCH bullish swing:   level=15.0
    - CHoCH bearish internal:level=25.0
    - EQH:                   level=12.0
    - EQL:                   level=18.0
    - OB bullish internal:   zone [9.0, 11.0]
    - OB bearish swing:      zone [19.0, 22.0]
    """
    return {
        "events": [
            {
                "type": "BOS", "bullish": True, "internal": True, "bias": 1,
                "anchor_index": 100, "anchor_time": "2026-05-01",
                "confirmed_index": 105, "confirmed_time": "2026-05-08",
                "level": 10.0,
            },
            {
                "type": "BOS", "bullish": False, "internal": False, "bias": -1,
                "anchor_index": 110, "anchor_time": "2026-05-10",
                "confirmed_index": 115, "confirmed_time": "2026-05-17",
                "level": 20.0,
            },
            {
                "type": "CHoCH", "bullish": True, "internal": False, "bias": 1,
                "anchor_index": 120, "anchor_time": "2026-05-20",
                "confirmed_index": 125, "confirmed_time": "2026-05-27",
                "level": 15.0,
            },
            {
                "type": "CHoCH", "bullish": False, "internal": True, "bias": -1,
                "anchor_index": 130, "anchor_time": "2026-06-01",
                "confirmed_index": 135, "confirmed_time": "2026-06-08",
                "level": 25.0,
            },
        ],
        "equal_highs_lows": [
            {
                "type": "EQH",
                "anchor_index": 140, "anchor_time": "2026-06-10",
                "second_pivot_index": 145,
                "second_pivot_time": "2026-07-18 14:30",
                "confirmed_index": 148, "confirmed_time": "2026-07-18 14:45",
                "level": 12.0, "prev_level": 11.95,
            },
            {
                "type": "EQL",
                "anchor_index": 150, "anchor_time": "2026-06-15",
                "second_pivot_index": 155,
                "second_pivot_time": "2026-07-18 14:15",
                "confirmed_index": 158, "confirmed_time": "2026-07-18 14:30",
                "level": 18.0, "prev_level": 18.05,
            },
        ],
        "order_blocks": [
            {
                "anchor_index": 160, "anchor_time": "2026-06-20",
                "confirmed_index": 165, "confirmed_time": "2026-06-27",
                "bar_high": 11.0, "bar_low": 9.0,
                "bias": 1, "internal": True, "mitigated_index": None,
            },
            {
                "anchor_index": 170, "anchor_time": "2026-07-01",
                "confirmed_index": 175, "confirmed_time": "2026-07-08",
                "bar_high": 22.0, "bar_low": 19.0,
                "bias": -1, "internal": False, "mitigated_index": None,
            },
        ],
        "trailing": {},
        "swing_bias": 0,
        "pivots": [],
    }


@pytest.fixture
def controlled_smc(monkeypatch: pytest.MonkeyPatch) -> dict:
    """monkeypatch compute_smc_adapter 返回可控 DTO。"""
    dto = _build_controlled_smc_dto()
    monkeypatch.setattr(smc_monitor, "compute_smc_adapter", lambda *a, **k: dto)
    return dto


@pytest.fixture
def monitor() -> SmcMonitor:
    """构造 SmcMonitor 实例（直接绑定 strategy_version_id，跳过 initialize）。"""
    m = SmcMonitor()
    m._strategy_version_id = STRATEGY_VERSION_ID
    return m


def _make_context(bars_minute_high: float, bars_minute_low: float,
                  bars_minute_close: float = 13.0) -> MarketDataContext:
    """构造 MarketDataContext。1m 末根 high/low 由参数控制（决定是否相交）。"""
    return MarketDataContext(
        instrument_id=TEST_INSTRUMENT,
        symbol="688506",
        bars_daily=_build_daily_bars(260),
        bars_minute=_build_minute_bars(bars_minute_high, bars_minute_low, bars_minute_close),
        bar_time=datetime(2026, 7, 18, 14, 30, tzinfo=UTC),
    )


# =============================================================================
# 测试 1: 五类事件全部触发 + 方向/级别保留
# =============================================================================
@pytest.mark.asyncio
async def test_all_five_event_types_fire_with_direction_and_level(
    controlled_smc: dict, monitor: SmcMonitor,
) -> None:
    """1m high-low 覆盖 [8, 26] 相交所有 level/zone → 五类事件全部触发。

    断言：
    - 五类 event_type 各至少一个事件。
    - BOS/CHoCH/OB payload 保留 bullish/bias（方向）与 internal（级别）。
    - BOS/CHoCH 同时覆盖 bullish/bearish 与 internal/swing。
    - OB 同时覆盖 bullish/bearish 与 internal/swing。
    - EQH/EQL payload 保留 eqhl_type 与 second_pivot_time。
    - 每个 event 有稳定 dedupe_key 和 logical_entity。
    """
    # 1m 末根 high=26, low=8 → 覆盖所有 level(10,12,15,18,20,25) 和 zone([9,11],[19,22])
    ctx = _make_context(bars_minute_high=26.0, bars_minute_low=8.0, bars_minute_close=13.0)
    curr_state = await monitor.calculate_state(ctx)

    events = await monitor.detect_events(ctx, prev_state=None, curr_state=curr_state)

    # 五类事件各至少一个
    event_types = {e.event_type for e in events}
    expected_types = {
        "smc_bos_retest", "smc_choch_retest",
        "smc_equal_highs_retest", "smc_equal_lows_retest",
        "smc_order_block_first_touch",
    }
    assert expected_types.issubset(event_types), (
        f"缺失事件类型: {expected_types - event_types}; 实际 {event_types}"
    )

    # 按 event_type 分组校验 payload
    by_type: dict[str, list] = {}
    for e in events:
        by_type.setdefault(e.event_type, []).append(e)

    # --- BOS: 覆盖 bullish/bearish × internal/swing ---
    bos_payloads = [e.payload for e in by_type["smc_bos_retest"]]
    bos_dirs = {(p.get("bullish"), p.get("internal")) for p in bos_payloads}
    assert (True, True) in bos_dirs, f"BOS 缺少 bullish+internal: {bos_dirs}"
    assert (False, False) in bos_dirs, f"BOS 缺少 bearish+swing: {bos_dirs}"
    for p in bos_payloads:
        # bias 与 bullish 一致（bullish→1, bearish→-1）
        assert p["bias"] == (1 if p["bullish"] else -1), (
            f"BOS bias 与 bullish 不一致: bullish={p['bullish']} bias={p['bias']}"
        )
        assert p["level"] is not None and p["anchor_index"] is not None

    # --- CHoCH: 覆盖 bullish/bearish × internal/swing ---
    choch_payloads = [e.payload for e in by_type["smc_choch_retest"]]
    choch_dirs = {(p.get("bullish"), p.get("internal")) for p in choch_payloads}
    assert (True, False) in choch_dirs, f"CHoCH 缺少 bullish+swing: {choch_dirs}"
    assert (False, True) in choch_dirs, f"CHoCH 缺少 bearish+internal: {choch_dirs}"
    for p in choch_payloads:
        assert p["bias"] == (1 if p["bullish"] else -1)

    # --- OB: 覆盖 bullish/bearish × internal/swing ---
    ob_payloads = [e.payload for e in by_type["smc_order_block_first_touch"]]
    ob_dirs = {(p.get("bias"), p.get("internal")) for p in ob_payloads}
    assert (1, True) in ob_dirs, f"OB 缺少 bullish(bias=1)+internal: {ob_dirs}"
    assert (-1, False) in ob_dirs, f"OB 缺少 bearish(bias=-1)+swing: {ob_dirs}"
    for p in ob_payloads:
        assert p["bar_high"] is not None and p["bar_low"] is not None

    # --- EQH/EQL: eqhl_type + second_pivot_time ---
    eqh_payloads = [e.payload for e in by_type["smc_equal_highs_retest"]]
    assert len(eqh_payloads) >= 1
    for p in eqh_payloads:
        assert p["eqhl_type"] == "EQH", f"EQH eqhl_type 应为 'EQH', 实际 {p['eqhl_type']}"
        assert p["second_pivot_time"] == "2026-07-18 14:30", (
            f"EQH second_pivot_time 不匹配: {p['second_pivot_time']}"
        )
        assert p["second_pivot_index"] == 145

    eql_payloads = [e.payload for e in by_type["smc_equal_lows_retest"]]
    assert len(eql_payloads) >= 1
    for p in eql_payloads:
        assert p["eqhl_type"] == "EQL", f"EQL eqhl_type 应为 'EQL', 实际 {p['eqhl_type']}"
        assert p["second_pivot_time"] == "2026-07-18 14:15"
        assert p["second_pivot_index"] == 155

    # --- 每个 event 有稳定 dedupe_key 和 logical_entity ---
    for e in events:
        assert e.dedupe_key, f"{e.event_type} dedupe_key 为空"
        assert e.logical_entity, f"{e.event_type} logical_entity 为空"
        assert ":" in e.dedupe_key, f"{e.event_type} dedupe_key 应含 touch_episode 分隔符"
        # payload 含 event_type 与 touch_episode
        assert e.payload["event_type"] == e.event_type
        assert e.payload["touch_episode"] == 1  # 首次触发 episode=1


# =============================================================================
# 测试 2: 1m high-low 不相交 → 无事件
# =============================================================================
@pytest.mark.asyncio
async def test_no_intersection_no_events(
    controlled_smc: dict, monitor: SmcMonitor,
) -> None:
    """1m high-low=[28,30] 不与任何 level(<=25)/zone(<=22) 相交 → 无事件触发。"""
    ctx = _make_context(bars_minute_high=30.0, bars_minute_low=28.0, bars_minute_close=29.0)
    curr_state = await monitor.calculate_state(ctx)

    # currently_touched 应全为 False（或空）
    touched = curr_state.state["smc_currently_touched"]
    assert not any(touched.values()), (
        f"无相交时不应有 touched=True，实际 {touched}"
    )

    events = await monitor.detect_events(ctx, prev_state=None, curr_state=curr_state)
    assert events == [], f"无相交时不应产生事件，实际 {[e.event_type for e in events]}"


# =============================================================================
# 测试 3: 同 touch_episode 不重复触发（dedupe）
# =============================================================================
@pytest.mark.asyncio
async def test_same_touch_episode_no_repeat(
    controlled_smc: dict, monitor: SmcMonitor,
) -> None:
    """prev_state 已 touched 的 entity，curr 仍 touched → 同 episode 不重复触发。

    构造：第一次 detect_events(prev=None) 触发 episode=1 事件；
         第二次 detect_events(prev=第一次 curr) 同 entity 仍 touched → 不产生新事件。
    """
    ctx = _make_context(bars_minute_high=26.0, bars_minute_low=8.0, bars_minute_close=13.0)

    # 第一次：prev=None → 触发
    curr1 = await monitor.calculate_state(ctx)
    events1 = await monitor.detect_events(ctx, prev_state=None, curr_state=curr1)
    first_types = sorted(e.event_type for e in events1)
    assert len(events1) >= 5, f"首次应触发>=5事件，实际 {len(events1)}: {first_types}"

    # 第二次：prev=curr1，相同 1m 仍 touched → 同 episode 不重复
    # 重新 calculate_state（相同 1m → 相同 touched）
    curr2 = await monitor.calculate_state(ctx)
    events2 = await monitor.detect_events(ctx, prev_state=curr1, curr_state=curr2)
    assert events2 == [], (
        f"同 touch_episode 不应重复触发，实际产生 {[e.event_type for e in events2]}"
    )

    # 校验 tracker episode 计数未递增（仍为 1）
    tracker = curr2.state["smc_episode_tracker"]
    for entity_id, info in tracker.items():
        if info.get("last_touched"):
            assert info["episode"] == 1, (
                f"同 episode 重复触碰，episode 不应递增: {entity_id} episode={info['episode']}"
            )


# =============================================================================
# 测试 4: 新 episode 触发（离开后再次触碰）
# =============================================================================
@pytest.mark.asyncio
async def test_new_episode_after_leaving(
    controlled_smc: dict, monitor: SmcMonitor,
) -> None:
    """prev touched → 中间未 touched → 再次 touched → 新 episode=2 触发。

    链路：
      T1: 1m 相交 → episode=1 触发
      T2: 1m 不相交 → last_touched=False（保留 episode=1）
      T3: 1m 再次相交 → 新 episode=2 触发
    """
    # T1: 相交
    ctx_t1 = _make_context(bars_minute_high=26.0, bars_minute_low=8.0, bars_minute_close=13.0)
    curr1 = await monitor.calculate_state(ctx_t1)
    events1 = await monitor.detect_events(ctx_t1, prev_state=None, curr_state=curr1)
    assert len(events1) >= 1

    # T2: 不相交
    ctx_t2 = _make_context(bars_minute_high=30.0, bars_minute_low=28.0, bars_minute_close=29.0)
    curr2 = await monitor.calculate_state(ctx_t2)
    events2 = await monitor.detect_events(ctx_t2, prev_state=curr1, curr_state=curr2)
    assert events2 == [], "T2 不相交不应触发事件"
    # tracker 中所有 entity last_touched=False
    tracker2 = curr2.state["smc_episode_tracker"]
    assert all(not info.get("last_touched") for info in tracker2.values()), (
        f"T2 后所有 entity 应 last_touched=False: {tracker2}"
    )

    # T3: 再次相交 → 新 episode=2
    ctx_t3 = _make_context(bars_minute_high=26.0, bars_minute_low=8.0, bars_minute_close=13.0)
    curr3 = await monitor.calculate_state(ctx_t3)
    events3 = await monitor.detect_events(ctx_t3, prev_state=curr2, curr_state=curr3)
    assert len(events3) >= 5, (
        f"T3 再次相交应触发>=5新 episode 事件，实际 {len(events3)}: "
        f"{[e.event_type for e in events3]}"
    )
    # 所有事件 touch_episode 应为 2（新 episode）
    for e in events3:
        assert e.payload["touch_episode"] == 2, (
            f"{e.event_type} 新 episode 应为 2，实际 {e.payload['touch_episode']}"
        )


# =============================================================================
# 测试 5: event_id 与 payload 一致性（entity_id 唯一标识方向/级别）
# =============================================================================
@pytest.mark.asyncio
async def test_event_id_and_payload_consistency(
    controlled_smc: dict, monitor: SmcMonitor,
) -> None:
    """每个事件的 logical_entity 与 payload.smc_entity_id 一致，且唯一。"""
    ctx = _make_context(bars_minute_high=26.0, bars_minute_low=8.0, bars_minute_close=13.0)
    curr_state = await monitor.calculate_state(ctx)
    events = await monitor.detect_events(ctx, prev_state=None, curr_state=curr_state)

    entity_ids = set()
    for e in events:
        # logical_entity = "{instrument_id}:{entity_id}"
        assert e.logical_entity.startswith(f"{TEST_INSTRUMENT}:"), (
            f"logical_entity 应以 instrument_id 开头: {e.logical_entity}"
        )
        payload_entity = e.payload["smc_entity_id"]
        # logical_entity 末尾应是 payload 中的 smc_entity_id
        assert e.logical_entity.endswith(payload_entity), (
            f"logical_entity 末尾应等于 payload.smc_entity_id: "
            f"{e.logical_entity} vs {payload_entity}"
        )
        # entity_id 唯一（每个事件对应不同结构对象）
        assert payload_entity not in entity_ids, (
            f"重复 entity_id: {payload_entity}"
        )
        entity_ids.add(payload_entity)

    # 应有 8 个不同 entity（2 BOS + 2 CHoCH + 1 EQH + 1 EQL + 2 OB）
    assert len(entity_ids) == 8, (
        f"应有 8 个唯一 entity_id，实际 {len(entity_ids)}: {entity_ids}"
    )
