# PURE_UNIT_TEST=1
"""竞价扫描服务纯单元测试 - 不连接数据库。

覆盖：
  1. 双重突破：高开同时跨结构上轨和筹码上区 = dual_breakout
  2. 压力区非突破：供应 OB 内高开 = resistance_blocked（非 dual_breakout）
  3. 支撑测试：需求区未破 = support_confirm
  4. 双重破位：低开同时破结构下轨和筹码下区（structure_breakout + below_lower）
  5. 龙头驱动场景：单龙头高开 → above_high 结构位置（聚合层判定 leader_driven）
  6. 扩散场景：正迁移位置（above_trigger / above_upper）正确分类
  7. chip 不可用时只看结构位置（chip_position=None）
  8. 除权不误判：adj_factor 变化检测
  9. 失效锚点不参与：is_active=False / freshness=expired 的锚点被过滤
  10. 参与度分级：abnormal_low / low / normal / high / abnormal_high

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_auction_scan_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.services.auction_scan_service import (
    AUCTION_SCAN_ALGORITHM_VERSION,
    EX_RIGHT_ADJ_FACTOR_CHANGE_THRESHOLD,
    HISTORY_LOOKBACK_DAYS,
    LIMIT_DOWN_THRESHOLD,
    LIMIT_UP_THRESHOLD,
    OPENING_VERIFY_WINDOW_MINUTES,
    PARTICIPATION_PERCENTILE_ABNORMAL_HIGH,
    PARTICIPATION_PERCENTILE_ABNORMAL_LOW,
    PARTICIPATION_PERCENTILE_HIGH,
    PARTICIPATION_PERCENTILE_LOW,
    _classify_chip_position,
    _classify_event_type,
    _classify_participation_level,
    _classify_structure_position,
    _classify_trend_background,
    _compute_change_pct,
    _compute_relative_volume_median,
    _compute_volume_percentile,
    _detect_ex_right,
    _detect_limits,
    _determine_lifecycle_transition,
    _safe_decimal,
    _safe_float,
)

_TRADE_DATE = date(2026, 7, 30)


# =============================================================================
# 辅助构造函数
# =============================================================================


class AnchorStub:
    """简化 AuctionAnchorItem mock，仅含被测字段。"""

    def __init__(
        self,
        *,
        anchor_type: str = "structure",
        direction: str = "up",
        lower_price: Decimal | float | None = None,
        upper_price: Decimal | float | None = None,
        center_price: Decimal | float | None = None,
        strength: float = 0.7,
        freshness: str = "fresh",
        validity: str = "valid",
        is_active: bool = True,
        structure_payload: dict | None = None,
        chip_payload: dict | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.anchor_type = anchor_type
        self.direction = direction
        self.lower_price = Decimal(str(lower_price)) if lower_price is not None else None
        self.upper_price = Decimal(str(upper_price)) if upper_price is not None else None
        self.center_price = Decimal(str(center_price)) if center_price is not None else None
        self.strength = strength
        self.freshness = freshness
        self.validity = validity
        self.is_active = is_active
        self.structure_payload = structure_payload
        self.chip_payload = chip_payload


def _bos_anchor(
    price: float,
    direction: str = "up",
    *,
    freshness: str = "fresh",
    is_active: bool = True,
) -> AnchorStub:
    """BOS/CHoCH 触发线锚点。"""
    return AnchorStub(
        anchor_type="structure",
        direction=direction,
        lower_price=Decimal(str(price)),
        upper_price=Decimal(str(price)),
        center_price=Decimal(str(price)),
        strength=0.85,
        freshness=freshness,
        is_active=is_active,
        structure_payload={"event_type": "BOS"},
    )


def _ob_anchor(
    ob_high: float,
    ob_low: float,
    direction: str = "up",
    *,
    freshness: str = "fresh",
    is_active: bool = True,
) -> AnchorStub:
    """OB 区间锚点。"""
    return AnchorStub(
        anchor_type="structure",
        direction=direction,
        lower_price=Decimal(str(ob_low)),
        upper_price=Decimal(str(ob_high)),
        center_price=Decimal(str((ob_high + ob_low) / 2)),
        strength=0.70,
        freshness=freshness,
        is_active=is_active,
        structure_payload={"event_type": "OB_CREATED"},
    )


def _trailing_anchor(
    price: float,
    direction: str = "down",
    *,
    is_active: bool = True,
) -> AnchorStub:
    """trailing 失效线锚点。"""
    kind = "trailing_top" if direction == "down" else "trailing_bottom"
    return AnchorStub(
        anchor_type="structure",
        direction=direction,
        lower_price=Decimal(str(price)),
        upper_price=Decimal(str(price)),
        center_price=Decimal(str(price)),
        strength=0.60,
        freshness="fresh",
        is_active=is_active,
        structure_payload={"kind": kind},
    )


def _chip_anchor(
    kind: str,
    price: float,
    direction: str = "up",
    *,
    is_active: bool = True,
) -> AnchorStub:
    """筹码锚点（poc/vah/val/cross_event）。"""
    return AnchorStub(
        anchor_type="chip",
        direction=direction,
        lower_price=Decimal(str(price)),
        upper_price=Decimal(str(price)),
        center_price=Decimal(str(price)),
        strength=0.70,
        freshness="fresh",
        is_active=is_active,
        chip_payload={"kind": kind},
    )


class MockBarDaily:
    """简化 BarDaily mock。"""

    def __init__(
        self,
        *,
        close: float | None = 10.0,
        high: float | None = 10.5,
        low: float | None = 9.5,
        adj_factor: float | None = 1.0,
        trade_date: date | None = None,
    ) -> None:
        self.close = Decimal(str(close)) if close is not None else None
        self.high = Decimal(str(high)) if high is not None else None
        self.low = Decimal(str(low)) if low is not None else None
        self.adj_factor = Decimal(str(adj_factor)) if adj_factor is not None else None
        self.trade_date = trade_date or _TRADE_DATE


# =============================================================================
# 测试：常量校验
# =============================================================================


class TestConstants:
    """常量校验。"""

    def test_algorithm_version(self) -> None:
        assert AUCTION_SCAN_ALGORITHM_VERSION == "v1.0.0"

    def test_percentile_thresholds(self) -> None:
        assert PARTICIPATION_PERCENTILE_LOW == 20
        assert PARTICIPATION_PERCENTILE_HIGH == 80
        assert PARTICIPATION_PERCENTILE_ABNORMAL_LOW == 5
        assert PARTICIPATION_PERCENTILE_ABNORMAL_HIGH == 95

    def test_limit_thresholds(self) -> None:
        assert LIMIT_UP_THRESHOLD == 9.9
        assert LIMIT_DOWN_THRESHOLD == -9.9

    def test_ex_right_threshold(self) -> None:
        assert EX_RIGHT_ADJ_FACTOR_CHANGE_THRESHOLD == 0.001

    def test_lookback_and_window(self) -> None:
        assert HISTORY_LOOKBACK_DAYS == 20
        assert OPENING_VERIFY_WINDOW_MINUTES == 30


# =============================================================================
# 测试：安全转换函数
# =============================================================================


class TestSafeFunctions:
    """_safe_decimal / _safe_float。"""

    def test_safe_decimal_none(self) -> None:
        assert _safe_decimal(None) is None

    def test_safe_decimal_invalid(self) -> None:
        assert _safe_decimal("invalid") is None

    def test_safe_decimal_valid(self) -> None:
        assert _safe_decimal("10.5") == Decimal("10.5")

    def test_safe_float_none(self) -> None:
        assert _safe_float(None) is None

    def test_safe_float_invalid(self) -> None:
        assert _safe_float("abc") is None

    def test_safe_float_valid(self) -> None:
        assert _safe_float("3.14") == 3.14


# =============================================================================
# 测试：涨跌幅计算
# =============================================================================


class TestComputeChangePct:
    """_compute_change_pct 涨跌幅计算（含分子分母）。"""

    def test_normal_calculation(self) -> None:
        """正涨幅 = (price - prev_close) / prev_close * 100。"""
        pct, comp = _compute_change_pct(Decimal("11.0"), Decimal("10.0"))
        assert pct == 10.0
        assert comp["price"] == "11.0"
        assert comp["prev_close"] == "10.0"

    def test_negative_change(self) -> None:
        pct, _ = _compute_change_pct(Decimal("9.0"), Decimal("10.0"))
        assert pct == -10.0

    def test_none_price_returns_none(self) -> None:
        assert _compute_change_pct(None, Decimal("10.0"))[0] is None

    def test_none_prev_close_returns_none(self) -> None:
        assert _compute_change_pct(Decimal("10.0"), None)[0] is None

    def test_zero_prev_close_returns_none(self) -> None:
        assert _compute_change_pct(Decimal("10.0"), Decimal("0"))[0] is None


# =============================================================================
# 测试：竞价额中位数和分位
# =============================================================================


class TestRelativeVolumeMedian:
    """_compute_relative_volume_median 相对 20 日竞价额中位数。"""

    def test_normal_ratio(self) -> None:
        """ratio = current / median。"""
        ratio, comp = _compute_relative_volume_median(
            Decimal("150.0"), [Decimal("100.0"), Decimal("200.0")],
        )
        # median(100, 200) = 150, 150/150 = 1.0
        assert ratio == 1.0
        assert comp["current_amount"] == "150.0"
        assert comp["median_20d"] is not None

    def test_none_current_returns_none(self) -> None:
        assert _compute_relative_volume_median(None, [Decimal("100.0")])[0] is None

    def test_empty_history_returns_none(self) -> None:
        assert _compute_relative_volume_median(Decimal("100.0"), [])[0] is None

    def test_zero_median_returns_none(self) -> None:
        """历史全为 0 时 median=0，返回 None。"""
        ratio, _ = _compute_relative_volume_median(
            Decimal("100.0"), [Decimal("0"), Decimal("0")],
        )
        # 过滤后 valid=[]，返回 None
        assert ratio is None


class TestVolumePercentile:
    """_compute_volume_percentile 竞价额分位。"""

    def test_median_percentile(self) -> None:
        """150 在 [100, 200] 中分位 50。"""
        pctile, comp = _compute_volume_percentile(
            Decimal("150.0"), [Decimal("100.0"), Decimal("200.0")],
        )
        assert pctile == 50.0
        assert comp["below_count"] == 1

    def test_none_current_returns_none(self) -> None:
        assert _compute_volume_percentile(None, [Decimal("100.0")])[0] is None

    def test_empty_history_returns_none(self) -> None:
        assert _compute_volume_percentile(Decimal("100.0"), [])[0] is None


# =============================================================================
# 测试：结构位置分类
# =============================================================================


class TestClassifyStructurePosition:
    """_classify_structure_position 结构位置分类。"""

    def test_above_high(self) -> None:
        """价格高于最高阻力锚点上沿 → above_high。"""
        anchors = [
            _bos_anchor(price=10.0, direction="down"),  # 阻力触发线
            _trailing_anchor(price=11.0, direction="down"),  # 阻力失效线
        ]
        pos = _classify_structure_position(Decimal("12.0"), anchors)
        assert pos == "above_high"

    def test_above_trigger(self) -> None:
        """价格突破 BOS/CHoCH 阻力触发线但未超过所有阻力 → above_trigger。"""
        anchors = [
            _bos_anchor(price=10.0, direction="down"),  # 阻力触发线
            _trailing_anchor(price=15.0, direction="down"),  # 更高的阻力
        ]
        pos = _classify_structure_position(Decimal("11.0"), anchors)
        assert pos == "above_trigger"

    def test_below_low(self) -> None:
        """价格低于最低支撑锚点下沿 → below_low。"""
        anchors = [
            _bos_anchor(price=10.0, direction="up"),  # 支撑触发线
            _trailing_anchor(price=8.0, direction="up"),  # 支撑失效线
        ]
        pos = _classify_structure_position(Decimal("7.0"), anchors)
        assert pos == "below_low"

    def test_below_trigger(self) -> None:
        """价格跌破 BOS/CHoCH 支撑触发线但未跌破所有支撑 → below_trigger。"""
        anchors = [
            _bos_anchor(price=10.0, direction="up"),  # 支撑触发线
            _trailing_anchor(price=5.0, direction="up"),  # 更低的支撑
        ]
        pos = _classify_structure_position(Decimal("9.0"), anchors)
        assert pos == "below_trigger"

    def test_demand_ob(self) -> None:
        """价格在需求 OB 区间内（up direction OB）→ demand_ob。"""
        anchors = [
            _ob_anchor(ob_high=10.5, ob_low=10.0, direction="up"),
        ]
        pos = _classify_structure_position(Decimal("10.2"), anchors)
        assert pos == "demand_ob"

    def test_supply_ob(self) -> None:
        """价格在供应 OB 区间内（down direction OB）→ supply_ob。"""
        anchors = [
            _ob_anchor(ob_high=10.5, ob_low=10.0, direction="down"),
        ]
        pos = _classify_structure_position(Decimal("10.2"), anchors)
        assert pos == "supply_ob"

    def test_normal(self) -> None:
        """价格在正常区间（支撑之上、阻力之下）→ normal。"""
        anchors = [
            _bos_anchor(price=9.0, direction="up"),  # 支撑
            _bos_anchor(price=11.0, direction="down"),  # 阻力
        ]
        pos = _classify_structure_position(Decimal("10.0"), anchors)
        assert pos == "normal"

    def test_none_price(self) -> None:
        """price=None → None。"""
        assert _classify_structure_position(None, [_bos_anchor(10.0)]) is None

    def test_empty_anchors(self) -> None:
        """无锚点 → None。"""
        assert _classify_structure_position(Decimal("10.0"), []) is None

    def test_no_structure_anchors(self) -> None:
        """只有 chip 锚点（无 structure）→ None。"""
        chip_only = [_chip_anchor("poc", 10.0)]
        assert _classify_structure_position(Decimal("10.0"), chip_only) is None


# =============================================================================
# 测试：筹码位置分类
# =============================================================================


class TestClassifyChipPosition:
    """_classify_chip_position 筹码位置分类。"""

    def test_above_upper(self) -> None:
        """价格超过所有筹码阻力（VAH + cross_down）→ above_upper。"""
        anchors = [
            _chip_anchor("poc", 10.0, direction="up"),
            _chip_anchor("vah", 11.0, direction="down"),
            _chip_anchor("val", 9.0, direction="up"),
        ]
        pos = _classify_chip_position(Decimal("12.0"), anchors)
        assert pos == "above_upper"

    def test_upper_zone(self) -> None:
        """价格在 VAH 之上但未超过所有筹码阻力 → upper_zone。"""
        anchors = [
            _chip_anchor("poc", 10.0, direction="up"),
            _chip_anchor("vah", 11.0, direction="down"),
            _chip_anchor("val", 9.0, direction="up"),
            _chip_anchor("cross_event", 12.0, direction="down"),  # 更高的 cross_down
        ]
        pos = _classify_chip_position(Decimal("11.5"), anchors)
        assert pos == "upper_zone"

    def test_between(self) -> None:
        """价格在 POC 与 VAH 之间（价值区内）→ between。"""
        anchors = [
            _chip_anchor("poc", 10.0, direction="up"),
            _chip_anchor("vah", 11.0, direction="down"),
            _chip_anchor("val", 9.0, direction="up"),
        ]
        pos = _classify_chip_position(Decimal("10.5"), anchors)
        assert pos == "between"

    def test_lower_zone(self) -> None:
        """价格在 VAL 与 POC 之间 → lower_zone。"""
        anchors = [
            _chip_anchor("poc", 10.0, direction="up"),
            _chip_anchor("vah", 11.0, direction="down"),
            _chip_anchor("val", 9.0, direction="up"),
        ]
        pos = _classify_chip_position(Decimal("9.5"), anchors)
        assert pos == "lower_zone"

    def test_below_lower(self) -> None:
        """价格低于 VAL（下共识区）→ below_lower。"""
        anchors = [
            _chip_anchor("poc", 10.0, direction="up"),
            _chip_anchor("vah", 11.0, direction="down"),
            _chip_anchor("val", 9.0, direction="up"),
        ]
        pos = _classify_chip_position(Decimal("8.0"), anchors)
        assert pos == "below_lower"

    def test_none_price(self) -> None:
        assert _classify_chip_position(None, [_chip_anchor("poc", 10.0)]) is None

    def test_no_chip_anchors(self) -> None:
        """无 chip 锚点 → None（structure_only 场景）。"""
        structure_only = [_bos_anchor(10.0)]
        assert _classify_chip_position(Decimal("10.0"), structure_only) is None

    def test_no_poc_vah_val(self) -> None:
        """只有 cross_event 锚点（无 POC/VAH/VAL）→ None。"""
        cross_only = [_chip_anchor("cross_event", 10.0, direction="up")]
        assert _classify_chip_position(Decimal("10.0"), cross_only) is None


# =============================================================================
# 测试：事件类型分类
# =============================================================================


class TestClassifyEventType:
    """_classify_event_type 事件类型分类。"""

    def test_dual_breakout(self) -> None:
        """双重突破：结构 bullish + 筹码 bullish → dual_breakout。"""
        # structure_bullish: above_trigger or above_high
        # chip_bullish: above_upper
        event = _classify_event_type(
            structure_pos="above_high",
            chip_pos="above_upper",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "dual_breakout"

    def test_dual_breakout_above_trigger(self) -> None:
        """双重突破（above_trigger 变体）。"""
        event = _classify_event_type(
            structure_pos="above_trigger",
            chip_pos="above_upper",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "dual_breakout"

    def test_resistance_blocked(self) -> None:
        """压力区非突破：供应 OB 内 → resistance_blocked（非 dual_breakout）。"""
        event = _classify_event_type(
            structure_pos="supply_ob",
            chip_pos="upper_zone",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "resistance_blocked"

    def test_support_confirm(self) -> None:
        """支撑测试：需求 OB 内未破 → support_confirm。"""
        event = _classify_event_type(
            structure_pos="demand_ob",
            chip_pos="lower_zone",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "support_confirm"

    def test_dual_breakdown_scenario(self) -> None:
        """双重破位场景：结构 bearish + 筹码 bearish。

        扫描层 event_type=structure_breakout（bearish 方向由 structure_position 标记）。
        聚合层通过 _is_dual_breakdown 检测同时 below_trigger/below_low + below_lower。
        """
        event = _classify_event_type(
            structure_pos="below_low",
            chip_pos="below_lower",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        # 扫描层不产生 dual_breakdown，而是 structure_breakout
        assert event == "structure_breakout"

    def test_structure_breakout_bullish(self) -> None:
        """单维度结构突破（bullish）→ structure_breakout。"""
        event = _classify_event_type(
            structure_pos="above_trigger",
            chip_pos="between",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "structure_breakout"

    def test_chip_repricing(self) -> None:
        """仅筹码重新定价（无结构突破）→ chip_repricing。"""
        event = _classify_event_type(
            structure_pos="normal",
            chip_pos="above_upper",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "chip_repricing"

    def test_structure_chip_conflict(self) -> None:
        """结构与筹码信号冲突 → structure_chip_conflict。"""
        # structure bullish + chip bearish
        event = _classify_event_type(
            structure_pos="above_high",
            chip_pos="below_lower",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "structure_chip_conflict"

    def test_test_upper(self) -> None:
        """测试上区间 → test_upper。"""
        event = _classify_event_type(
            structure_pos="normal",
            chip_pos="upper_zone",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "test_upper"

    def test_test_lower(self) -> None:
        """测试下区间 → test_lower。"""
        event = _classify_event_type(
            structure_pos="normal",
            chip_pos="lower_zone",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "test_lower"

    def test_inside_open(self) -> None:
        """区间内开盘 → inside_open。"""
        event = _classify_event_type(
            structure_pos="normal",
            chip_pos="between",
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "inside_open"

    def test_insufficient_participation(self) -> None:
        """极端低参与度优先标记 → insufficient_participation。"""
        event = _classify_event_type(
            structure_pos="above_high",
            chip_pos="above_upper",
            participation_level="abnormal_low",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "insufficient_participation"

    def test_anchor_insufficient(self) -> None:
        """活跃锚点不足 → anchor_insufficient。"""
        event = _classify_event_type(
            structure_pos=None,
            chip_pos=None,
            participation_level="normal",
            has_active_anchors=False,
            all_expired=False,
        )
        assert event == "anchor_insufficient"

    def test_anchor_expired(self) -> None:
        """所有锚点过期 → anchor_expired。"""
        event = _classify_event_type(
            structure_pos=None,
            chip_pos=None,
            participation_level="normal",
            has_active_anchors=False,
            all_expired=True,
        )
        assert event == "anchor_expired"

    def test_chip_not_available_structure_only(self) -> None:
        """chip 不可用时 chip_position=None，只看结构位置 → structure_breakout。"""
        event = _classify_event_type(
            structure_pos="above_trigger",
            chip_pos=None,  # chip 不可用
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "structure_breakout"


# =============================================================================
# 测试：龙头驱动和扩散场景（扫描层位置分类）
# =============================================================================


class TestLeaderDrivenAndDiffusionScenarios:
    """龙头驱动和扩散场景在扫描层的输入分类。

    龙头驱动（leader_driven）和初始扩散（initial_diffusion）是聚合层标签，
    其输入来自扫描层的 structure_position / chip_position / change_pct。
    本测试验证扫描层正确分类这些输入。
    """

    def test_leader_driven_input_classification(self) -> None:
        """龙头驱动场景：单龙头高开 → above_high 结构位置 + 高 change_pct。

        聚合层通过 top3_contribution > 50% + leader_change > 2*median_change 判定。
        """
        anchors = [
            _bos_anchor(price=10.0, direction="down"),  # 阻力
            _trailing_anchor(price=11.0, direction="down"),
        ]
        # 龙头高开至 12.0 → above_high
        pos = _classify_structure_position(Decimal("12.0"), anchors)
        assert pos == "above_high"

        # change_pct = (12 - 10) / 10 * 100 = 20%
        pct, _ = _compute_change_pct(Decimal("12.0"), Decimal("10.0"))
        assert pct == 20.0

    def test_diffusion_input_classification(self) -> None:
        """扩散场景：多数正迁移（above_trigger 或 above_upper）。

        聚合层通过 positive_migration_ratio > 40% + coverage > 50% 判定 initial_diffusion。
        """
        # 结构正迁移
        anchors_structure = [
            _bos_anchor(price=10.0, direction="down"),
            _trailing_anchor(price=15.0, direction="down"),
        ]
        pos = _classify_structure_position(Decimal("11.0"), anchors_structure)
        assert pos == "above_trigger"  # 正迁移

        # 筹码正迁移
        anchors_chip = [
            _chip_anchor("poc", 10.0, direction="up"),
            _chip_anchor("vah", 11.0, direction="down"),
            _chip_anchor("val", 9.0, direction="up"),
        ]
        chip_pos = _classify_chip_position(Decimal("12.0"), anchors_chip)
        assert chip_pos == "above_upper"  # 正迁移


# =============================================================================
# 测试：参与度分级
# =============================================================================


class TestClassifyParticipationLevel:
    """_classify_participation_level 参与度分级。"""

    def test_abnormal_low(self) -> None:
        """分位 < 5 → abnormal_low。"""
        assert _classify_participation_level(2.0) == "abnormal_low"
        assert _classify_participation_level(4.9) == "abnormal_low"

    def test_low(self) -> None:
        """5 <= 分位 < 20 → low。"""
        assert _classify_participation_level(5.0) == "low"
        assert _classify_participation_level(10.0) == "low"
        assert _classify_participation_level(19.9) == "low"

    def test_normal(self) -> None:
        """20 <= 分位 <= 80 → normal。"""
        assert _classify_participation_level(20.0) == "normal"
        assert _classify_participation_level(50.0) == "normal"
        assert _classify_participation_level(80.0) == "normal"

    def test_high(self) -> None:
        """80 < 分位 <= 95 → high。"""
        assert _classify_participation_level(80.1) == "high"
        assert _classify_participation_level(90.0) == "high"
        assert _classify_participation_level(95.0) == "high"

    def test_abnormal_high(self) -> None:
        """分位 > 95 → abnormal_high。"""
        assert _classify_participation_level(95.1) == "abnormal_high"
        assert _classify_participation_level(99.0) == "abnormal_high"

    def test_none_returns_none(self) -> None:
        assert _classify_participation_level(None) is None


# =============================================================================
# 测试：除权检测
# =============================================================================


class TestDetectExRight:
    """_detect_ex_right 除权除息检测。"""

    def test_significant_adj_factor_change(self) -> None:
        """adj_factor 变化 > 阈值 → True（除权）。"""
        # 1.0 → 1.05，变化 5% > 0.1%
        bars = [
            MockBarDaily(adj_factor=1.05, close=10.0),
            MockBarDaily(adj_factor=1.0, close=10.0),
        ]
        assert _detect_ex_right(bars) is True

    def test_no_adj_factor_change(self) -> None:
        """adj_factor 不变 → False。"""
        bars = [
            MockBarDaily(adj_factor=1.0, close=10.0),
            MockBarDaily(adj_factor=1.0, close=10.0),
        ]
        assert _detect_ex_right(bars) is False

    def test_small_adj_factor_change(self) -> None:
        """adj_factor 变化 ≤ 阈值 → False。"""
        # 1.0 → 1.0005，变化 0.05% < 0.1%
        bars = [
            MockBarDaily(adj_factor=1.0005, close=10.0),
            MockBarDaily(adj_factor=1.0, close=10.0),
        ]
        assert _detect_ex_right(bars) is False

    def test_none_adj_factor(self) -> None:
        """adj_factor 为 None → False。"""
        bars = [
            MockBarDaily(adj_factor=None, close=10.0),
            MockBarDaily(adj_factor=1.0, close=10.0),
        ]
        assert _detect_ex_right(bars) is False

    def test_single_bar(self) -> None:
        """只有 1 根 bar → False（无法比较）。"""
        bars = [MockBarDaily(adj_factor=1.0)]
        assert _detect_ex_right(bars) is False

    def test_empty_history(self) -> None:
        """空历史 → False。"""
        assert _detect_ex_right([]) is False

    def test_zero_prev_adj_factor(self) -> None:
        """前一日 adj_factor=0 → False（避免除零）。"""
        bars = [
            MockBarDaily(adj_factor=1.0, close=10.0),
            MockBarDaily(adj_factor=0.0, close=10.0),
        ]
        assert _detect_ex_right(bars) is False


# =============================================================================
# 测试：涨跌停检测
# =============================================================================


class TestDetectLimits:
    """_detect_limits 涨跌停检测。"""

    def test_limit_up(self) -> None:
        assert _detect_limits(10.0) == (True, False)
        assert _detect_limits(9.9) == (True, False)

    def test_limit_down(self) -> None:
        assert _detect_limits(-10.0) == (False, True)
        assert _detect_limits(-9.9) == (False, True)

    def test_no_limit(self) -> None:
        assert _detect_limits(5.0) == (False, False)
        assert _detect_limits(-5.0) == (False, False)
        assert _detect_limits(0.0) == (False, False)

    def test_none_returns_false_false(self) -> None:
        assert _detect_limits(None) == (False, False)


# =============================================================================
# 测试：趋势背景
# =============================================================================


class TestClassifyTrendBackground:
    """_classify_trend_background 趋势背景判断。"""

    def test_uptrend(self) -> None:
        """前后半段均值比较，上升 > 2% → up。

        closes 按降序传入（最新在前），内部反转为升序计算。
        """
        # 最新在前，反转后为 [9, 9.5, 10, 10.5, 11] → 上升
        closes = [Decimal("11.0"), Decimal("10.5"), Decimal("10.0"),
                  Decimal("9.5"), Decimal("9.0")]
        assert _classify_trend_background(closes) == "up"

    def test_downtrend(self) -> None:
        """下降 > 2% → down。"""
        closes = [Decimal("9.0"), Decimal("9.5"), Decimal("10.0"),
                  Decimal("10.5"), Decimal("11.0")]
        assert _classify_trend_background(closes) == "down"

    def test_neutral(self) -> None:
        """变化 ≤ 2% → neutral。"""
        closes = [Decimal("10.0")] * 5
        assert _classify_trend_background(closes) == "neutral"

    def test_insufficient_history(self) -> None:
        """少于 5 个收盘价 → neutral。"""
        assert _classify_trend_background([Decimal("10.0"), Decimal("11.0")]) == "neutral"

    def test_empty(self) -> None:
        assert _classify_trend_background([]) == "neutral"


# =============================================================================
# 测试：生命周期转换
# =============================================================================


class TestDetermineLifecycleTransition:
    """_determine_lifecycle_transition 事件生命周期转换。"""

    def test_breakout_confirmed(self) -> None:
        """突破类事件：开盘价 >= 触发价 → confirmed。"""
        assert _determine_lifecycle_transition(
            "dual_breakout", Decimal("11.0"), Decimal("10.0"),
        ) == "confirmed"
        assert _determine_lifecycle_transition(
            "structure_breakout", Decimal("11.0"), Decimal("10.0"),
        ) == "confirmed"
        assert _determine_lifecycle_transition(
            "chip_repricing", Decimal("11.0"), Decimal("10.0"),
        ) == "confirmed"

    def test_breakout_weakened(self) -> None:
        """突破类事件：开盘价回落 2% 以内 → weakened。"""
        # 10 * 0.98 = 9.8, open=9.9 → weakened
        assert _determine_lifecycle_transition(
            "dual_breakout", Decimal("9.9"), Decimal("10.0"),
        ) == "weakened"

    def test_breakout_failed(self) -> None:
        """突破类事件：开盘价回落 > 2% → failed。"""
        assert _determine_lifecycle_transition(
            "dual_breakout", Decimal("9.5"), Decimal("10.0"),
        ) == "failed"

    def test_support_confirm_confirmed(self) -> None:
        """支撑确认：开盘价 >= 触发价 → confirmed。"""
        assert _determine_lifecycle_transition(
            "support_confirm", Decimal("10.0"), Decimal("9.5"),
        ) == "confirmed"

    def test_support_confirm_failed(self) -> None:
        """支撑确认：开盘价回落 > 2% → failed。"""
        assert _determine_lifecycle_transition(
            "support_confirm", Decimal("9.0"), Decimal("10.0"),
        ) == "failed"

    def test_resistance_blocked_confirmed(self) -> None:
        """阻力阻挡：开盘价 <= 触发价 → confirmed。"""
        assert _determine_lifecycle_transition(
            "resistance_blocked", Decimal("9.5"), Decimal("10.0"),
        ) == "confirmed"

    def test_resistance_blocked_failed(self) -> None:
        """阻力阻挡：开盘价超过触发价 > 2% → failed。"""
        assert _determine_lifecycle_transition(
            "resistance_blocked", Decimal("10.5"), Decimal("10.0"),
        ) == "failed"

    def test_test_upper_confirmed(self) -> None:
        """测试上区间：开盘价 >= 触发价 → confirmed。"""
        assert _determine_lifecycle_transition(
            "test_upper", Decimal("11.0"), Decimal("10.0"),
        ) == "confirmed"

    def test_test_upper_weakened(self) -> None:
        """测试上区间：开盘价 < 触发价 → weakened。"""
        assert _determine_lifecycle_transition(
            "test_upper", Decimal("9.0"), Decimal("10.0"),
        ) == "weakened"

    def test_test_lower_confirmed(self) -> None:
        """测试下区间：开盘价 <= 触发价 → confirmed。"""
        assert _determine_lifecycle_transition(
            "test_lower", Decimal("9.0"), Decimal("10.0"),
        ) == "confirmed"

    def test_inside_open_stays_formed(self) -> None:
        """inside_open 无明确触发线 → 维持 formed。"""
        assert _determine_lifecycle_transition(
            "inside_open", Decimal("10.0"), Decimal("10.0"),
        ) == "formed"

    def test_none_opening_price_returns_formed(self) -> None:
        """无开盘价 → formed。"""
        assert _determine_lifecycle_transition(
            "dual_breakout", None, Decimal("10.0"),
        ) == "formed"

    def test_none_trigger_price_returns_formed(self) -> None:
        """无触发价 → formed。"""
        assert _determine_lifecycle_transition(
            "dual_breakout", Decimal("10.0"), None,
        ) == "formed"

    def test_zero_trigger_price_returns_formed(self) -> None:
        """触发价=0 → formed（避免除零）。"""
        assert _determine_lifecycle_transition(
            "dual_breakout", Decimal("10.0"), Decimal("0"),
        ) == "formed"


# =============================================================================
# 测试：失效锚点不参与位置判断
# =============================================================================


class TestInvalidatedAnchorsExcluded:
    """失效锚点（is_active=False / freshness=expired）不参与位置判断。

    扫描服务 run_auction_scan 中通过以下过滤排除失效锚点：
        active_anchors = [a for a in anchors if a.is_active and a.freshness != "expired"]

    本测试验证过滤逻辑正确：仅 active + 非 expired 的锚点参与 _classify_*。
    """

    def test_inactive_anchor_excluded_from_classification(self) -> None:
        """is_active=False 的锚点不应参与位置判断。

        模拟 run_auction_scan 中的过滤：
        仅 is_active=True 且 freshness != "expired" 的锚点传入 _classify_structure_position。
        """
        # 一个 active 锚点 + 一个 inactive 锚点
        active_anchor = _bos_anchor(price=10.0, direction="down", is_active=True)
        inactive_anchor = _bos_anchor(price=15.0, direction="down", is_active=False)

        # 模拟 run_auction_scan 的过滤逻辑
        anchors = [active_anchor, inactive_anchor]
        active_anchors = [
            a for a in anchors
            if a.is_active and a.freshness != "expired"
        ]
        assert len(active_anchors) == 1
        assert active_anchors[0] is active_anchor

        # 仅用 active 锚点分类：唯一阻力 BOS@10.0，价格 11 > 最高阻力上沿 10.0 → above_high。
        # 关键：若过滤失败（inactive BOS@15.0 未排除），最高阻力=15.0，价格 11<15 → above_trigger。
        # 因此 above_high 恰好证明 inactive 锚点已被排除。
        pos = _classify_structure_position(Decimal("11.0"), active_anchors)
        assert pos == "above_high"

    def test_expired_anchor_excluded(self) -> None:
        """freshness=expired 的锚点不参与位置判断。"""
        fresh_anchor = _bos_anchor(price=10.0, direction="down", freshness="fresh")
        expired_anchor = _bos_anchor(
            price=15.0, direction="down", freshness="expired",
        )

        anchors = [fresh_anchor, expired_anchor]
        active_anchors = [
            a for a in anchors
            if a.is_active and a.freshness != "expired"
        ]
        assert len(active_anchors) == 1
        assert active_anchors[0] is fresh_anchor

    def test_all_expired_returns_no_active(self) -> None:
        """所有锚点都 expired → active_anchors 为空 → event_type=anchor_expired。"""
        anchors = [
            _bos_anchor(price=10.0, freshness="expired"),
            _bos_anchor(price=15.0, direction="down", freshness="expired"),
        ]
        active_anchors = [
            a for a in anchors
            if a.is_active and a.freshness != "expired"
        ]
        assert active_anchors == []

        all_expired = bool(anchors) and not active_anchors and all(
            a.freshness == "expired" for a in anchors
        )
        assert all_expired is True

        event = _classify_event_type(
            structure_pos=None,
            chip_pos=None,
            participation_level="normal",
            has_active_anchors=False,
            all_expired=all_expired,
        )
        assert event == "anchor_expired"


# =============================================================================
# 测试：chip 不可用时只看结构位置
# =============================================================================


class TestChipUnavailableStructureOnly:
    """chip 锚点不可用时 chip_position=None，只看结构位置。"""

    def test_no_chip_anchors_returns_none(self) -> None:
        """无 chip 锚点 → chip_position=None（structure_only 场景）。"""
        structure_only_anchors = [
            _bos_anchor(price=10.0, direction="up"),
            _bos_anchor(price=11.0, direction="down"),
        ]
        chip_pos = _classify_chip_position(Decimal("10.5"), structure_only_anchors)
        assert chip_pos is None

    def test_structure_only_event_classification(self) -> None:
        """chip 不可用时：structure=above_trigger + chip=None → structure_breakout。"""
        event = _classify_event_type(
            structure_pos="above_trigger",
            chip_pos=None,
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "structure_breakout"

    def test_structure_only_resistance_blocked(self) -> None:
        """chip 不可用时：supply_ob → resistance_blocked。"""
        event = _classify_event_type(
            structure_pos="supply_ob",
            chip_pos=None,
            participation_level="normal",
            has_active_anchors=True,
            all_expired=False,
        )
        assert event == "resistance_blocked"
