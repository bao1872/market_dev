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
