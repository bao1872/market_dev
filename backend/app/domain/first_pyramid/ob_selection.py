"""OB lifecycle event canonical ordering / latest-OB selection (最小共享 helper)。

[REVIEW-FACT-PARITY-02] latest-OB SAME-INPUT parity 唯一 SSOT。

背景（真实 defect）：
    SMC producer（smc_pine_core）为每个 OB lifecycle 事件输出**不同的时点字段**：
        OB_CREATED   → confirmed_index / confirmed_time
        OB_ENTERED   → enter_index     / enter_time
        OB_MITIGATED → mitigated_index / mitigated_time
    并且 OB_MITIGATED 事件同时携带 `enter_index`/`enter_time`（若 mitigation 前
    曾 entered，用于 `entered_before_mitigation` 语义），二者**不是**同一根 bar。

    snapshot 路径此前用 `enter_index or mitigated_index or confirmed_index`
    的 `or` 链定位事件 bar，导致 OB_MITIGATED 被错误盖上 **enter bar**；
    history 路径用 type-switch 取 `mitigated_index`（正确）。
    同一 SAME-INPUT 事件集合因此得到不同 bar_index → freshness / 选择结果分叉
    （实测 603897：snapshot freshness=173 vs history freshness=15）。

Canonical 语义（复用 SMC producer 已有语义，无新增产品定义）：
    OB lifecycle 事件的**发生 bar** = 该生命周期状态转移实际发生的那根 bar。
    latest OB = 发生 bar 最大者；同 bar 多事件时取 producer 追加顺序的最后一个
    （producer 单次触发且按 bar 递增追加，保证确定性）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "OB_LIFECYCLE_TYPES",
    "ob_event_bar_index",
    "ob_event_time",
    "select_latest_ob",
]

# `OB_ENTRY` 为已废弃的旧派生事件，仅为历史快照读取兼容保留。
OB_LIFECYCLE_TYPES: frozenset[str] = frozenset(
    {"OB_CREATED", "OB_ENTERED", "OB_MITIGATED", "OB_ENTRY"}
)

# 事件类型 → (index 字段, time 字段)；canonical 定位字段，禁止 `or` 链回退。
_OB_TIME_KEYS: dict[str, tuple[str, str]] = {
    "OB_CREATED": ("confirmed_index", "confirmed_time"),
    "OB_ENTERED": ("enter_index", "enter_time"),
    "OB_MITIGATED": ("mitigated_index", "mitigated_time"),
}


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ob_event_bar_index(ob_event: dict[str, Any]) -> int | None:
    """返回 OB lifecycle 事件的 canonical 发生 bar index。

    未知/废弃事件类型（如旧 OB_ENTRY）回退到 confirmed_index，
    因为旧快照只持久化了该字段。
    """
    keys = _OB_TIME_KEYS.get(str(ob_event.get("type", "")))
    if keys is None:
        return _coerce_int(ob_event.get("confirmed_index"))
    return _coerce_int(ob_event.get(keys[0]))


def ob_event_time(ob_event: dict[str, Any]) -> Any:
    """返回 OB lifecycle 事件的 canonical 发生时间（与 bar index 同一根 bar）。"""
    keys = _OB_TIME_KEYS.get(str(ob_event.get("type", "")))
    if keys is None:
        return ob_event.get("confirmed_time")
    return ob_event.get(keys[1])


def select_latest_ob(
    ob_events: list[dict[str, Any]],
    *,
    bar_index_getter: Any = None,
) -> dict[str, Any] | None:
    """从 OB lifecycle 事件列表中选出 canonical latest OB。

    选择规则（history / snapshot flatten 共用）：
        1. 丢弃无法定位发生 bar 的事件；
        2. 取发生 bar index 最大者；
        3. 同 bar 多事件时取列表中最后出现的一个（producer 追加顺序）。

    输入列表顺序不影响结果（只要 canonical chronology 相同）。

    Args:
        ob_events: OB lifecycle 事件列表（producer 原始 dict，或已扁平化事件 dict）。
        bar_index_getter: 可选，自定义 bar index 提取函数；默认使用
            :func:`ob_event_bar_index`。用于已扁平化事件（bar_index 已写定）。
    """
    getter = bar_index_getter or ob_event_bar_index
    best: dict[str, Any] | None = None
    best_idx: int | None = None
    for evt in ob_events:
        idx = getter(evt)
        if idx is None:
            continue
        if best_idx is None or idx >= best_idx:
            best_idx = idx
            best = evt
    return best
