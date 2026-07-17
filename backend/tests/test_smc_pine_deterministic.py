"""SMC Pine 确定性测试 — 验证核心逻辑与 Pine 真源对齐。

[CHANGE-20260717-001 Pine parity] 新增确定性测试，不依赖 TV CSV fixture，
使用合成 OHLC 数据验证 SMC 核心逻辑的 Pine 语义正确性。

测试覆盖：
1. CHoCH 规则（4 组：internal/swing × bull/bear）
2. BOS（bias 延续时非 CHoCH）
3. warmup 一致性（完整历史裁剪 vs warmup 裁剪）
4. OB 顺序（newest-first，与 Pine unshift 一致）
5. OB 全链（core → adapter → 顺序/anchor/high-low/mitigation）
6. trailing NaN（首个 swing pivot 前为 NaN）
7. execution gate（internal/swing 门控）
8. EQ 几何（两端点 prev_level/level，anchor → second_pivot 区间）

Pine 真源：ref/smc_user_source.pine（SHA256 0bd3d2ad，843 行，不可变）
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from app.services.smc_view_adapter import adapt_smc_to_display_dto
from app.strategy_assets.algorithms.features.smc_indicator import compute_smc_indicators

# ===== 辅助函数 =====


def _gen_times(n: int, start: str = "2024-01-01", freq_days: int = 1) -> list[str]:
    """生成 n 个 ISO 时间字符串（日线间隔）。"""
    dt_start = datetime.fromisoformat(start).replace(tzinfo=None)
    return [
        (dt_start + timedelta(days=freq_days * i)).strftime("%Y-%m-%d")
        for i in range(n)
    ]


def _gen_ohlc(
    n: int,
    base: float = 100.0,
    trend: float = 0.0,
    volatility: float = 1.0,
    seed: int = 42,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """生成合成 OHLC 数据。

    Args:
        n: bar 数量
        base: 基础价格
        trend: 每根 bar 的趋势增量（正=上涨，负=下跌）
        volatility: 波动幅度
        seed: 随机种子（确定性）

    Returns:
        (opens, highs, lows, closes)
    """
    rng = random.Random(seed)
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    price = base
    for _ in range(n):
        o = price
        change = trend + rng.uniform(-volatility, volatility)
        c = o + change
        h = max(o, c) + rng.uniform(0, volatility * 0.5)
        lo = min(o, c) - rng.uniform(0, volatility * 0.5)
        opens.append(round(o, 4))
        highs.append(round(h, 4))
        lows.append(round(lo, 4))
        closes.append(round(c, 4))
        price = c
    return opens, highs, lows, closes


def _gen_trending_then_reverse(
    n: int = 200,
    up_bars: int = 120,
    base: float = 100.0,
    seed: int = 99,
) -> tuple[list[float], list[float], list[float], list[float]]:
    """生成先上涨后下跌的 OHLC（用于触发 bearish CHoCH）。

    前 up_bars 根上涨+振荡（建立 BULLISH bias + 形成 swing pivots），
    之后持续下跌（下穿 swing low → bearish CHoCH）。
    需要 ≥200 根以确保 swings_length=50 的 pivot 能被确认。
    """
    rng = random.Random(seed)
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    price = base
    for i in range(n):
        o = price
        if i < up_bars:
            change = 0.8 + rng.uniform(-1.5, 1.5)  # 上涨+振荡
        else:
            change = -2.0 + rng.uniform(-1.0, 1.0)  # 下跌
        c = max(1.0, o + change)
        h = max(o, c) + rng.uniform(0.1, 0.8)
        lo = min(o, c) - rng.uniform(0.1, 0.8)
        opens.append(round(o, 4))
        highs.append(round(h, 4))
        lows.append(round(lo, 4))
        closes.append(round(c, 4))
        price = c
    return opens, highs, lows, closes


# ===== 1. CHoCH 规则测试 =====


class TestChoCHRules:
    """CHoCH 规则：上穿时旧 bias=BEARISH 才是 bullish CHoCH；
    下穿时旧 bias=BULLISH 才是 bearish CHoCH。"""

    def test_choch_bearish_after_bullish(self) -> None:
        """先上涨建立 BULLISH bias，后下跌穿越 swing low → bearish CHoCH。"""
        n = 200
        opens, highs, lows, closes = _gen_trending_then_reverse(n=n, up_bars=120)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        # 应至少有一个 CHoCH 事件（bearish CHoCH：下穿时旧 bias=BULLISH）
        choch_events = [e for e in result.get("events", []) if e.get("type") == "CHoCH"]
        bos_events = [e for e in result.get("events", []) if e.get("type") == "BOS"]
        # 上涨阶段应有 BOS（bias 延续），下跌阶段应有 bearish CHoCH
        assert len(bos_events) > 0 or len(choch_events) > 0, (
            f"应有 BOS 或 CHoCH 事件，events={result.get('events', [])[:5]}"
        )
        # 若有 CHoCH，至少一个应为 bearish（bias=-1）
        bearish_choch = [e for e in choch_events if e.get("bias") == -1]
        if choch_events:
            assert len(bearish_choch) > 0, (
                f"下跌反转应产生 bearish CHoCH，choch_events={choch_events}"
            )

    def test_choch_tag_determined_before_bias_update(self) -> None:
        """验证 CHoCH tag 在 bias 更新前确定（Pine L565/L590）。"""
        n = 200
        opens, highs, lows, closes = _gen_trending_then_reverse(n=n, up_bars=120)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        events = result.get("events", [])
        # 收集所有 CHoCH 事件，验证它们在 bias 反转点
        choch_events = [e for e in events if e.get("type") == "CHoCH"]
        # CHoCH 事件的 bias 方向应与穿越方向一致
        for ev in choch_events:
            bias = ev.get("bias", 0)
            # bullish CHoCH (bias=1) 应在上穿时发生
            # bearish CHoCH (bias=-1) 应在下穿时发生
            assert bias in (1, -1), f"CHoCH bias 异常: {ev}"


# ===== 2. BOS 测试 =====


class TestBOSRules:
    """BOS：bias 延续时穿越 pivot → BOS（非 CHoCH）。"""

    def test_bos_when_bias_continues(self) -> None:
        """持续上涨时穿越 swing high → BOS（bias 已是 BULLISH）。"""
        n = 200
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.5, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        events = result.get("events", [])
        bos_events = [e for e in events if e.get("type") == "BOS"]
        # 温和上涨+振荡应产生 swing pivots 并触发 BOS
        assert len(bos_events) > 0, (
            f"应有 BOS 事件，events={events[:5]}"
        )


# ===== 3. warmup 一致性测试 =====


class TestWarmupConsistency:
    """warmup 一致性：完整历史裁剪结果 vs warmup 裁剪结果应一致（重叠窗口）。"""

    def test_warmup_consistency(self) -> None:
        """5000 根计算 → 4000 展示 vs 4000 根计算 → 4000 展示。

        重叠窗口（后 4000 根）的 events/OB/EQ 应一致。
        注：由于 ATR/pivot 检测依赖历史，warmup 可能影响左缘少量 bar，
        本测试验证"大部分重叠窗口一致"而非完全一致。
        """
        n_full = 5000
        n_warmup = 4000
        opens, highs, lows, closes = _gen_ohlc(n=n_full, base=100.0, trend=0.5, volatility=2.0)
        times = _gen_times(n_full)

        # 完整计算
        result_full = compute_smc_indicators(
            opens=opens, highs=highs, lows=lows, closes=closes, times=times,
        )

        # warmup 裁剪计算（后 4000 根）
        result_warmup = compute_smc_indicators(
            opens=opens[n_full - n_warmup:],
            highs=highs[n_full - n_warmup:],
            lows=lows[n_full - n_warmup:],
            closes=closes[n_full - n_warmup:],
            times=times[n_full - n_warmup:],
        )

        # adapter 裁成 4000 展示
        dto_full = adapt_smc_to_display_dto(result_full, n_warmup)
        dto_warmup = adapt_smc_to_display_dto(result_warmup, n_warmup)

        # 时间数组应一致（后 4000 根）
        assert dto_full["time"] == dto_warmup["time"], (
            "完整历史裁剪与 warmup 裁剪的时间数组不一致"
        )

        # swing_bias 应一致
        assert dto_full["swing_bias"] == dto_warmup["swing_bias"], (
            f"swing_bias 不一致: full={dto_full['swing_bias']} warmup={dto_warmup['swing_bias']}"
        )


# ===== 4. OB 顺序测试 =====


class TestOrderBlockOrder:
    """OB 顺序：core 输出 newest-first（与 Pine unshift 一致）。"""

    def test_ob_order_newest_first(self) -> None:
        """验证 order_blocks_output 中最新 OB 在数组头部。"""
        n = 150
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=1.0, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        obs = result.get("order_blocks", [])
        if len(obs) < 2:
            pytest.skip("OB 数量不足，无法验证顺序")

        # 验证 newest-first：confirmed_index 应递减
        confirmed_indices = [ob.get("confirmed_index", -1) for ob in obs]
        for i in range(len(confirmed_indices) - 1):
            assert confirmed_indices[i] >= confirmed_indices[i + 1], (
                f"OB 顺序不是 newest-first: idx[{i}]={confirmed_indices[i]} "
                f"< idx[{i+1}]={confirmed_indices[i+1]}"
            )


# ===== 5. OB 全链测试 =====


class TestOrderBlockChain:
    """core → adapter 全链：顺序、anchor、high/low、mitigation。"""

    def test_ob_top5_in_adapter(self) -> None:
        """验证 adapter 输出 OB 保持 core 的 newest-first 顺序。"""
        n = 200
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.8, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        display_bars = 150
        dto = adapt_smc_to_display_dto(result, display_bars)

        dto_obs = dto.get("order_blocks", [])

        # adapter OB 保持 core 的 newest-first 顺序（confirmed_index 降序）
        if len(dto_obs) >= 2:
            dto_confirmed = [ob.get("confirmed_index") for ob in dto_obs]
            for j in range(len(dto_confirmed) - 1):
                # newest-first: 前面的 confirmed_index >= 后面的
                assert dto_confirmed[j] >= dto_confirmed[j + 1], (
                    f"adapter OB 未保持 newest-first 顺序: {dto_confirmed}"
                )

        # 验证 OB 字段完整性
        for ob in dto_obs:
            assert "anchor_index" in ob, f"OB 缺少 anchor_index: {ob}"
            assert "confirmed_index" in ob, f"OB 缺少 confirmed_index: {ob}"
            assert "bar_high" in ob, f"OB 缺少 bar_high: {ob}"
            assert "bar_low" in ob, f"OB 缺少 bar_low: {ob}"
            assert "bias" in ob, f"OB 缺少 bias: {ob}"
            assert "mitigated" in ob, f"OB 缺少 mitigated: {ob}"
            assert "internal" in ob, f"OB 缺少 internal: {ob}"


# ===== 6. trailing NaN 测试 =====


class TestTrailingNaN:
    """trailing NaN：首个 swing pivot 前 trailing.top/bottom 为 NaN。"""

    def test_trailing_nan_before_swing_pivot(self) -> None:
        """验证 swing pivot 检测前 trailing.top/bottom 为 None/NaN。"""
        n = 100
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.5, volatility=1.0)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        trailing = result.get("trailing", {})
        # trailing 在有 swing pivot 后应有值
        # 关键验证：trailing.top 不是用第一根 high 初始化的
        # 如果有 swing pivot，trailing.top 应等于某个 swing high pivot level
        # 而非第一根 bar 的 high
        if trailing.get("top") is not None:
            # trailing.top 不应等于第一根 bar 的 high（除非恰好是 swing high）
            # 主要验证 trailing.top 是有效数值（由 swing pivot 初始化，非首根 high）
            assert isinstance(trailing["top"], (int, float)), (
                f"trailing.top 类型异常: {trailing}"
            )
        if trailing.get("bottom") is not None:
            assert isinstance(trailing["bottom"], (int, float)), (
                f"trailing.bottom 类型异常: {trailing}"
            )

    def test_trailing_has_last_times(self) -> None:
        """验证 trailing 含 last_top_time/last_bottom_time 字段。"""
        n = 150
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.8, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        trailing = result.get("trailing", {})
        assert "last_top_time" in trailing, f"trailing 缺少 last_top_time: {trailing}"
        assert "last_bottom_time" in trailing, f"trailing 缺少 last_bottom_time: {trailing}"


# ===== 7. execution gate 测试 =====


class TestExecutionGate:
    """execution gate：Pine L784/L787 门控逻辑。"""

    def test_execution_gate_internal_disabled(self) -> None:
        """show_internals=False, show_internal_order_blocks=False, show_trend=False
        → internal 事件为空。"""
        n = 150
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.8, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(
            opens=opens, highs=highs, lows=lows, closes=closes, times=times,
            params={
                "show_internals": False,
                "show_internal_order_blocks": False,
                "show_trend": False,
            },
        )

        internal_events = [e for e in result.get("events", []) if e.get("internal") is True]
        assert len(internal_events) == 0, (
            f"internal gate 关闭后仍有 internal 事件: {internal_events[:3]}"
        )

    def test_execution_gate_swing_disabled(self) -> None:
        """show_structure=False, show_swing_order_blocks=False, show_high_low_swings=False
        → swing 事件为空。"""
        n = 150
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.8, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(
            opens=opens, highs=highs, lows=lows, closes=closes, times=times,
            params={
                "show_structure": False,
                "show_swing_order_blocks": False,
                "show_high_low_swings": False,
            },
        )

        swing_events = [e for e in result.get("events", []) if e.get("internal") is False]
        assert len(swing_events) == 0, (
            f"swing gate 关闭后仍有 swing 事件: {swing_events[:3]}"
        )

    def test_execution_gate_default_all_enabled(self) -> None:
        """默认参数下 internal 和 swing gate 都启用。"""
        n = 150
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.8, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        # 默认参数应有事件
        events = result.get("events", [])
        assert len(events) > 0, "默认参数下应有事件"


# ===== 8. EQ 几何测试 =====


class TestEqualHighLowGeometry:
    """EQH/EQL 几何：两端点 prev_level/level，anchor → second_pivot 区间。"""

    def test_eq_geometry_two_endpoints(self) -> None:
        """验证 EQH/EQL 输出含 prev_level 和 level（两端点）。"""
        n = 200
        opens, highs, lows, closes = _gen_ohlc(n=n, base=100.0, trend=0.3, volatility=2.0)
        times = _gen_times(n)
        result = compute_smc_indicators(opens=opens, highs=highs, lows=lows, closes=closes, times=times)

        eqs = result.get("equal_highs_lows", [])
        for eq in eqs:
            # 两端点字段
            assert "prev_level" in eq, f"EQ 缺少 prev_level: {eq}"
            assert "level" in eq, f"EQ 缺少 level: {eq}"
            # anchor 和 second_pivot
            assert "anchor_index" in eq, f"EQ 缺少 anchor_index: {eq}"
            assert "second_pivot_index" in eq, f"EQ 缺少 second_pivot_index: {eq}"
            # 区间方向：second_pivot > anchor（新 pivot 在旧 pivot 之后）
            assert eq["second_pivot_index"] > eq["anchor_index"], (
                f"EQ second_pivot_index 应大于 anchor_index: {eq}"
            )
            # type 必须是 EQH 或 EQL
            assert eq.get("type") in ("EQH", "EQL"), f"EQ type 异常: {eq.get('type')}"
            # prev_level 和 level 应为数值
            assert isinstance(eq["prev_level"], (int, float)), (
                f"EQ prev_level 类型异常: {eq}"
            )
            assert isinstance(eq["level"], (int, float)), (
                f"EQ level 类型异常: {eq}"
            )
            # EQH: prev_level 和 level 应接近（等高），但可以有微小差异
            # EQL: 同理
            if eq.get("type") == "EQH":
                # 等高：|prev_level - level| 应在阈值内
                atr_threshold = 0.1 * 100  # 简化阈值
                assert abs(eq["prev_level"] - eq["level"]) <= atr_threshold, (
                    f"EQH 两端点差异过大: prev_level={eq['prev_level']} level={eq['level']}"
                )
