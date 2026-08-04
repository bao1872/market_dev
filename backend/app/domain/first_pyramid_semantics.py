"""Canonical semantic types shared by First Pyramid producers and consumers.

This module is deliberately dependency-free.  Algorithm producers may depend on
these enums, while read-model adapters and presentation code may also depend on
them.  The adapter layer must never be imported by DSA/SMC/momentum producers.
"""
from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class RegimeDirection(IntEnum):
    """Numeric DSA/SMC direction used inside calculation producers."""

    DOWN = -1
    SIDEWAYS = 0
    UP = 1

    @classmethod
    def from_value(cls, value: Any) -> RegimeDirection | None:
        if isinstance(value, cls):
            return value
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number > 0:
            return cls.UP
        if number < 0:
            return cls.DOWN
        return cls.SIDEWAYS


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class StructureAlignment(StrEnum):
    ALIGNED = "aligned"
    DIVERGENT = "divergent"


class MomentumDirection(StrEnum):
    EXPANDING = "expanding"
    CONTRACTING = "contracting"
    FLAT = "flat"


class MomentumChange(StrEnum):
    ENHANCING = "enhancing"
    WEAKENING = "weakening"
    FLAT = "flat"


class SqueezeState(StrEnum):
    SQUEEZE = "squeeze"
    RELEASED = "released"
    NORMAL = "normal"


class VolumeBadge(StrEnum):
    HIGH = "high"
    LOW = "low"
    NORMAL = "normal"
    UNKNOWN = "unknown"


_DIRECTION_DISPLAY = {
    Direction.UP: "上行",
    Direction.DOWN: "下行",
    Direction.SIDEWAYS: "震荡",
}
_ALIGNMENT_DISPLAY = {
    StructureAlignment.ALIGNED: "共振",
    StructureAlignment.DIVERGENT: "背离",
}
_MOMENTUM_DIRECTION_DISPLAY = {
    MomentumDirection.EXPANDING: "扩张",
    MomentumDirection.CONTRACTING: "收缩",
    MomentumDirection.FLAT: "平缓",
}
_SQUEEZE_DISPLAY = {
    SqueezeState.SQUEEZE: "挤压中",
    SqueezeState.RELEASED: "已释放",
    SqueezeState.NORMAL: "无挤压",
}
_VOLUME_DISPLAY = {
    VolumeBadge.HIGH: "放量",
    VolumeBadge.LOW: "缩量",
    VolumeBadge.NORMAL: "正常",
    VolumeBadge.UNKNOWN: "未知",
}


def direction_from_regime(value: Any) -> Direction | None:
    regime = RegimeDirection.from_value(value)
    if regime is RegimeDirection.UP:
        return Direction.UP
    if regime is RegimeDirection.DOWN:
        return Direction.DOWN
    if regime is RegimeDirection.SIDEWAYS:
        return Direction.SIDEWAYS
    return None


def direction_display(value: Direction | None) -> str | None:
    return _DIRECTION_DISPLAY.get(value) if value is not None else None


def alignment_display(value: StructureAlignment | None) -> str | None:
    return _ALIGNMENT_DISPLAY.get(value) if value is not None else None


def momentum_direction_display(value: MomentumDirection | None) -> str | None:
    return _MOMENTUM_DIRECTION_DISPLAY.get(value) if value is not None else None


def squeeze_display(value: SqueezeState | None) -> str | None:
    return _SQUEEZE_DISPLAY.get(value) if value is not None else None


def volume_badge_display(value: VolumeBadge | None) -> str | None:
    return _VOLUME_DISPLAY.get(value) if value is not None else None


# =============================================================================
# [QM-63 canonical 2026-08-04] SMC 事件唯一语义 formatter
# -----------------------------------------------------------------------------
# 与前端 src/components/smcLabels.ts 文案矩阵保持完全一致（单一事实来源）。
# 后端消费者（Review 证据、Detail API 事件列表、飞书卡片、监控证据）
# 必须统一调用本函数，禁止在各自模块重写中文文案。
#
# 正式方向值为 bullish/bearish；历史 up/down 仍接受并归一，但不再作为新契约。
# 缺方向 → "方向未知"；缺级别 → "级别未知"；两者皆缺 → "结构未知"（不猜测）。
# =============================================================================

# 正式 SMC 事件类型（与 PyramidEvent.type 对齐，不改底层 key）
class SmcStructureEventType(StrEnum):
    BOS = "BOS"
    CHOCH = "CHoCH"


class SmcEqType(StrEnum):
    EQH = "EQH"
    EQL = "EQL"


# 正式结构级别
class SmcStructureLevel(StrEnum):
    SWING = "swing"
    INTERNAL = "internal"


# 正式事件方向
class SmcEventDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


# 方向归一表：接受正式值/历史值/数值 bias。无法识别一律 None（不默认空头）。
_SMC_DIRECTION_ALIASES: dict[Any, SmcEventDirection] = {
    "bullish": SmcEventDirection.BULLISH,
    "up": SmcEventDirection.BULLISH,
    1: SmcEventDirection.BULLISH,
    "bearish": SmcEventDirection.BEARISH,
    "down": SmcEventDirection.BEARISH,
    -1: SmcEventDirection.BEARISH,
}


def normalize_smc_direction(raw: Any) -> SmcEventDirection | None:
    """归一 SMC 事件方向。接受 bullish/bearish/up/down/±1；其余（含 None、0、bool）返回 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, SmcEventDirection):
        return raw
    return _SMC_DIRECTION_ALIASES.get(raw)


def normalize_smc_structure_level(raw: Any) -> SmcStructureLevel | None:
    """归一结构级别。只接受 swing/internal，其余一律 None（不默认 swing）。"""
    if raw is None:
        return None
    if isinstance(raw, SmcStructureLevel):
        return raw
    return SmcStructureLevel.SWING if raw == "swing" else (
        SmcStructureLevel.INTERNAL if raw == "internal" else None
    )


# 文案矩阵（与前端 smcLabels.ts 完全一致）
_SMC_LEVEL_LABEL: dict[SmcStructureLevel, str] = {
    SmcStructureLevel.SWING: "主要",
    SmcStructureLevel.INTERNAL: "短线",
}
_SMC_EVENT_ACTION: dict[SmcStructureEventType, dict[SmcEventDirection, str]] = {
    SmcStructureEventType.BOS: {
        SmcEventDirection.BULLISH: "多头突破",
        SmcEventDirection.BEARISH: "空头跌破",
    },
    SmcStructureEventType.CHOCH: {
        SmcEventDirection.BULLISH: "转强拐点",
        SmcEventDirection.BEARISH: "转弱拐点",
    },
}
_SMC_OB_ACTION: dict[SmcEventDirection, str] = {
    SmcEventDirection.BULLISH: "多头承接区",
    SmcEventDirection.BEARISH: "空头压制区",
}
_SMC_EQ_LABEL: dict[SmcEqType, str] = {
    SmcEqType.EQH: "双顶压力",
    SmcEqType.EQL: "双底支撑",
}


class SmcSemantic:
    """SMC 事件格式化结果（字段语义与前端 SmcSemantic 对齐）。"""

    __slots__ = ("label", "direction", "structure_level", "arrow", "inconsistent", "diagnostic")

    def __init__(
        self,
        label: str,
        direction: SmcEventDirection | None,
        structure_level: SmcStructureLevel | None,
        arrow: str,
        inconsistent: bool,
        diagnostic: str | None,
    ) -> None:
        self.label = label
        self.direction = direction
        self.structure_level = structure_level
        self.arrow = arrow
        self.inconsistent = inconsistent
        self.diagnostic = diagnostic

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "direction": self.direction.value if self.direction is not None else None,
            "structureLevel": self.structure_level.value if self.structure_level is not None else None,
            "arrow": self.arrow,
            "inconsistent": self.inconsistent,
            "diagnostic": self.diagnostic,
        }


def _smc_arrow(direction: SmcEventDirection | None) -> str:
    if direction is SmcEventDirection.BULLISH:
        return "↑"
    if direction is SmcEventDirection.BEARISH:
        return "↓"
    return ""


def _smc_unknown_label(
    direction: SmcEventDirection | None,
    level: SmcStructureLevel | None,
) -> str:
    """缺字段时的显式文案：不猜测方向或级别。"""
    if direction is None and level is None:
        return "结构未知"
    if direction is None:
        return "方向未知"
    return "级别未知"


def _resolve_smc_direction(
    direction: Any,
    bias: Any,
) -> tuple[SmcEventDirection | None, bool, str | None]:
    """direction 优先；缺失时用 bias 推导。两者冲突输出 diagnostic，不静默择一。"""
    from_direction = normalize_smc_direction(direction)
    from_bias = normalize_smc_direction(bias)
    normalized = from_direction if from_direction is not None else from_bias
    inconsistent = (
        from_direction is not None
        and from_bias is not None
        and from_direction is not from_bias
    )
    diagnostic = (
        f"direction={from_direction.value if from_direction else None} "
        f"与 bias={bias} 不一致"
        if inconsistent
        else None
    )
    return normalized, inconsistent, diagnostic


def format_smc_event(
    *,
    event_type: Any,
    structure_level: Any = None,
    direction: Any = None,
    bias: Any = None,
) -> SmcSemantic:
    """BOS/CHoCH 八组合统一格式化；未知值不猜测为多头或空头。

    与前端 formatSmcEvent 语义一致。未知一律显式表达。
    """
    normalized_dir, inconsistent, diagnostic = _resolve_smc_direction(direction, bias)
    level = normalize_smc_structure_level(structure_level)
    event_kind = (
        SmcStructureEventType(event_type)
        if event_type in (SmcStructureEventType.BOS.value, SmcStructureEventType.CHOCH.value)
        else None
    )
    if event_kind is None:
        return SmcSemantic(
            label="结构未知",
            direction=normalized_dir,
            structure_level=level,
            arrow=_smc_arrow(normalized_dir),
            inconsistent=inconsistent,
            diagnostic=diagnostic,
        )
    if level is None or normalized_dir is None:
        return SmcSemantic(
            label=_smc_unknown_label(normalized_dir, level),
            direction=normalized_dir,
            structure_level=level,
            arrow=_smc_arrow(normalized_dir),
            inconsistent=inconsistent,
            diagnostic=diagnostic,
        )
    return SmcSemantic(
        label=f"{_SMC_LEVEL_LABEL[level]}·{_SMC_EVENT_ACTION[event_kind][normalized_dir]}",
        direction=normalized_dir,
        structure_level=level,
        arrow=_smc_arrow(normalized_dir),
        inconsistent=inconsistent,
        diagnostic=diagnostic,
    )


def format_smc_order_block(
    *,
    structure_level: Any = None,
    direction: Any = None,
    bias: Any = None,
) -> SmcSemantic:
    """Order Block 四组合统一格式化；未知值不猜测为多头或空头。"""
    normalized_dir, inconsistent, diagnostic = _resolve_smc_direction(direction, bias)
    level = normalize_smc_structure_level(structure_level)
    if level is None or normalized_dir is None:
        return SmcSemantic(
            label=_smc_unknown_label(normalized_dir, level),
            direction=normalized_dir,
            structure_level=level,
            arrow=_smc_arrow(normalized_dir),
            inconsistent=inconsistent,
            diagnostic=diagnostic,
        )
    return SmcSemantic(
        label=f"{_SMC_LEVEL_LABEL[level]}·{_SMC_OB_ACTION[normalized_dir]}",
        direction=normalized_dir,
        structure_level=level,
        arrow=_smc_arrow(normalized_dir),
        inconsistent=inconsistent,
        diagnostic=diagnostic,
    )


def get_smc_eq_label(eq_type: Any) -> str:
    """EQH/EQL 没有结构级别，不虚构主要/短线语义。"""
    kind = SmcEqType(eq_type) if eq_type in (SmcEqType.EQH.value, SmcEqType.EQL.value) else None
    if kind is None:
        return "结构未知"
    return _SMC_EQ_LABEL[kind]
