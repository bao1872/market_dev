"""Consumption-boundary adapter for First Pyramid read-model semantics.

The read model contains a mixture of current Chinese display values, historical
English aliases and (for new internal call sites) canonical enum values.  This
adapter normalizes all of them to the dependency-free canonical types in
``app.domain.first_pyramid_semantics``.  Producer algorithms must not import
this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from app.domain.first_pyramid_semantics import (
    Direction,
    MomentumChange,
    MomentumDirection,
    RegimeDirection,
    SqueezeState,
    StructureAlignment,
    VolumeBadge,
    direction_from_regime,
)
from app.schemas.first_pyramid import (
    normalize_direction,
    normalize_structure_level,
)

T = TypeVar("T")


def _token(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized or None
    return str(value).strip().lower() or None


_DIRECTION_ALIASES: dict[str, Direction] = {
    "up": Direction.UP,
    "bullish": Direction.UP,
    "positive": Direction.UP,
    "上行": Direction.UP,
    "向上": Direction.UP,
    "上涨": Direction.UP,
    "多头": Direction.UP,
    "1": Direction.UP,
    "down": Direction.DOWN,
    "bearish": Direction.DOWN,
    "negative": Direction.DOWN,
    "下行": Direction.DOWN,
    "向下": Direction.DOWN,
    "下跌": Direction.DOWN,
    "空头": Direction.DOWN,
    "-1": Direction.DOWN,
    "sideways": Direction.SIDEWAYS,
    "neutral": Direction.SIDEWAYS,
    "flat": Direction.SIDEWAYS,
    "震荡": Direction.SIDEWAYS,
    "盘整": Direction.SIDEWAYS,
    "0": Direction.SIDEWAYS,
}
_ALIGNMENT_ALIASES: dict[str, StructureAlignment] = {
    "aligned": StructureAlignment.ALIGNED,
    "alignment": StructureAlignment.ALIGNED,
    "resonant": StructureAlignment.ALIGNED,
    "共振": StructureAlignment.ALIGNED,
    "一致": StructureAlignment.ALIGNED,
    "divergent": StructureAlignment.DIVERGENT,
    "divergence": StructureAlignment.DIVERGENT,
    "misaligned": StructureAlignment.DIVERGENT,
    "背离": StructureAlignment.DIVERGENT,
    "分歧": StructureAlignment.DIVERGENT,
}
_MOMENTUM_DIRECTION_ALIASES: dict[str, MomentumDirection] = {
    "expanding": MomentumDirection.EXPANDING,
    "expansion": MomentumDirection.EXPANDING,
    "up": MomentumDirection.EXPANDING,
    "positive": MomentumDirection.EXPANDING,
    "扩张": MomentumDirection.EXPANDING,
    "增强": MomentumDirection.EXPANDING,
    "contracting": MomentumDirection.CONTRACTING,
    "contraction": MomentumDirection.CONTRACTING,
    "down": MomentumDirection.CONTRACTING,
    "negative": MomentumDirection.CONTRACTING,
    "收缩": MomentumDirection.CONTRACTING,
    "减弱": MomentumDirection.CONTRACTING,
    "flat": MomentumDirection.FLAT,
    "neutral": MomentumDirection.FLAT,
    "平缓": MomentumDirection.FLAT,
    "0": MomentumDirection.FLAT,
}
_MOMENTUM_CHANGE_ALIASES: dict[str, MomentumChange] = {
    "enhancing": MomentumChange.ENHANCING,
    "increasing": MomentumChange.ENHANCING,
    "strengthening": MomentumChange.ENHANCING,
    "增强": MomentumChange.ENHANCING,
    "走强": MomentumChange.ENHANCING,
    "weakening": MomentumChange.WEAKENING,
    "decreasing": MomentumChange.WEAKENING,
    "fading": MomentumChange.WEAKENING,
    "减弱": MomentumChange.WEAKENING,
    "走弱": MomentumChange.WEAKENING,
    "flat": MomentumChange.FLAT,
    "unchanged": MomentumChange.FLAT,
    "持平": MomentumChange.FLAT,
    "平缓": MomentumChange.FLAT,
}
_SQUEEZE_ALIASES: dict[str, SqueezeState] = {
    "squeeze": SqueezeState.SQUEEZE,
    "squeeze_on": SqueezeState.SQUEEZE,
    "sqz_on": SqueezeState.SQUEEZE,
    "挤压中": SqueezeState.SQUEEZE,
    "挤压": SqueezeState.SQUEEZE,
    "released": SqueezeState.RELEASED,
    "squeeze_off": SqueezeState.RELEASED,
    "sqz_off": SqueezeState.RELEASED,
    "已释放": SqueezeState.RELEASED,
    "释放": SqueezeState.RELEASED,
    "normal": SqueezeState.NORMAL,
    "no_squeeze": SqueezeState.NORMAL,
    "无挤压": SqueezeState.NORMAL,
}
_VOLUME_ALIASES: dict[str, VolumeBadge] = {
    "high": VolumeBadge.HIGH,
    "surge": VolumeBadge.HIGH,
    "expanding": VolumeBadge.HIGH,
    "放量": VolumeBadge.HIGH,
    "low": VolumeBadge.LOW,
    "shrink": VolumeBadge.LOW,
    "contracting": VolumeBadge.LOW,
    "缩量": VolumeBadge.LOW,
    "normal": VolumeBadge.NORMAL,
    "正常": VolumeBadge.NORMAL,
    "unknown": VolumeBadge.UNKNOWN,
    "未知": VolumeBadge.UNKNOWN,
}


def _alias(value: Any, enum_type: type[T], aliases: Mapping[str, T]) -> T | None:
    if isinstance(value, enum_type):
        return value
    token = _token(value)
    return aliases.get(token) if token is not None else None


class FirstPyramidSemanticAdapter:
    """Normalize one flat/read-model payload at a consumption boundary."""

    def __init__(self, source: Mapping[str, Any] | None = None) -> None:
        self.source = source or {}

    @staticmethod
    def direction(value: Any) -> Direction | None:
        if isinstance(value, RegimeDirection):
            return direction_from_regime(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return direction_from_regime(value)
        return _alias(value, Direction, _DIRECTION_ALIASES)

    @staticmethod
    def alignment(value: Any) -> StructureAlignment | None:
        return _alias(value, StructureAlignment, _ALIGNMENT_ALIASES)

    @staticmethod
    def momentum_direction_value(value: Any) -> MomentumDirection | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 0:
                return MomentumDirection.EXPANDING
            if value < 0:
                return MomentumDirection.CONTRACTING
            return MomentumDirection.FLAT
        return _alias(value, MomentumDirection, _MOMENTUM_DIRECTION_ALIASES)

    @staticmethod
    def momentum_change_value(value: Any) -> MomentumChange | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 0:
                return MomentumChange.ENHANCING
            if value < 0:
                return MomentumChange.WEAKENING
            return MomentumChange.FLAT
        return _alias(value, MomentumChange, _MOMENTUM_CHANGE_ALIASES)

    @staticmethod
    def squeeze(value: Any) -> SqueezeState | None:
        return _alias(value, SqueezeState, _SQUEEZE_ALIASES)

    @staticmethod
    def volume_badge_value(value: Any) -> VolumeBadge | None:
        return _alias(value, VolumeBadge, _VOLUME_ALIASES)

    @property
    def trend(self) -> Direction | None:
        return self.direction(self.source.get("fp_trend_direction"))

    @property
    def swing(self) -> Direction | None:
        return self.direction(self.source.get("fp_swing_direction"))

    @property
    def internal(self) -> Direction | None:
        return self.direction(self.source.get("fp_internal_direction"))

    @property
    def structure_alignment(self) -> StructureAlignment | None:
        return self.alignment(self.source.get("fp_structure_alignment"))

    @property
    def momentum_direction(self) -> MomentumDirection | None:
        return self.momentum_direction_value(self.source.get("fp_momentum_direction"))

    @property
    def momentum_change(self) -> MomentumChange | None:
        return self.momentum_change_value(self.source.get("fp_momentum_change"))

    @property
    def squeeze_state(self) -> SqueezeState | None:
        return self.squeeze(self.source.get("fp_squeeze_state"))

    @property
    def volume_badge(self) -> VolumeBadge | None:
        return self.volume_badge_value(self.source.get("fp_volume_badge"))

    def event_direction(self, field: str = "fp_structure_event_direction") -> Direction | None:
        return self.direction(self.source.get(field))


def adapt_legacy_pyramid_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """把旧事件 extra 字段提升到正式合同，并保留矛盾诊断。

    [QM-63 canonical 2026-08-04] 兼容 adapter 是**唯一**允许读取
    `extra.structure_level` / `extra.bias` 的位置。新 producer
    （build_pyramid_event）不依赖此函数。

    方向归一同时接受旧值（up/down）与正式值（bullish/bearish），
    统一输出正式值；缺方向保持 None（不默认 bearish），
    缺级别保持 None（不默认 swing）。
    """
    adapted = dict(event)
    raw_extra = event.get("extra")
    extra: Mapping[str, Any] = raw_extra if isinstance(raw_extra, Mapping) else {}
    diagnostics = list(adapted.get("diagnostics") or [])

    # 级别：正式字段优先，回退 extra（旧合同）
    level = normalize_structure_level(adapted.get("structureLevel"))
    if level is None:
        legacy_level = normalize_structure_level(extra.get("structure_level"))
        if legacy_level is not None:
            level = legacy_level
            diagnostics.append("STRUCTURE_LEVEL_FROM_LEGACY_EXTRA")

    # 方向：归一 up/down → bullish/bearish
    direction = normalize_direction(adapted.get("direction"))

    # bias：正式字段优先，回退 extra
    raw_bias = adapted.get("bias")
    if raw_bias is None:
        raw_bias = extra.get("bias")
    bias_direction = normalize_direction(raw_bias)

    if direction is None and bias_direction is not None:
        # 仅 bias 可用：据此推导方向
        direction = bias_direction
    elif (
        direction is not None
        and bias_direction is not None
        and direction != bias_direction
    ):
        # [QM-63] 冲突必须输出 diagnostic，以 direction 为准
        diagnostics.append("EVENT_DIRECTION_BIAS_CONFLICT")

    adapted["direction"] = direction
    adapted["structureLevel"] = level
    # bias 由最终 direction 派生，保证二者永远一致
    adapted["bias"] = 1 if direction == "bullish" else -1 if direction == "bearish" else None
    adapted["diagnostics"] = diagnostics
    return adapted
