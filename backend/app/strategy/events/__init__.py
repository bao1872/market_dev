"""V1.1 事件库包 - 从 ref/交易/event_lib/ 升级为 V1.1 monitor 事件格式。

核心组件：
- base.StrategyEventDraft: 事件草稿 dataclass（自包含 payload + state_ttl + allowed_roles）
- base.EventRole: 事件角色常量（TRIGGER/CONFIRM/VETO/OBSERVE）
- registry: 升级版注册表（支持 state_ttl_seconds 和 allowed_roles）
- detectors: 14 个检测器（从 event_lib/detectors/ 迁移）

Usage:
    from app.strategy.events import (
        StrategyEventDraft, EventRole,
        register_event, detect_to_drafts, detect_panel, list_all,
    )

    # 检测事件并生成草稿
    drafts = detect_to_drafts(
        factors_df,
        strategy_version_id="v1",
        instrument_id="600519",
    )
"""

from __future__ import annotations

from app.strategy.events.base import (
    EventRole,
    StrategyEventDraft,
    build_dedupe_key,
)

# 自动导入并注册所有事件检测器（触发注册）
from app.strategy.events.detectors import (  # noqa: F401
    breakout_events,
    composite_events,
    fundamental_events,
    momentum_events,
    sr_resistance_events,
    sr_support_events,
    stage_cost_zone_events,
    stage_failure_events,
    stage_repair_events,
    stage_shake_events,
    stage_wash_events,
    structural_events,
    trend_events,
    volume_events,
)
from app.strategy.events.registry import (
    EVENT_REGISTRY,
    detect_panel,
    detect_to_drafts,
    get_event,
    list_all,
    list_by_category,
    register_event,
)

__all__ = [
    "EVENT_REGISTRY",
    "EventRole",
    "StrategyEventDraft",
    "build_dedupe_key",
    "detect_panel",
    "detect_to_drafts",
    "get_event",
    "list_all",
    "list_by_category",
    "register_event",
]
