"""StrategyEventDraft - V1.1 监控事件草稿基类（M4 升级）。

从 ref/交易/event_lib/base.py 迁移 Event ABC，升级为 V1.1 monitor 事件格式。

核心升级：
1. 从 ABC 类升级为 dataclass：StrategyEventDraft 是检测到的事件实例（非检测器）
2. payload 自包含：不依赖外部状态，事件发生时冻结所有必要上下文
3. state_ttl_seconds：状态有效期（秒），超时后状态机窗口过期

Usage:
    from app.strategy.events.base import StrategyEventDraft

    draft = StrategyEventDraft(
        event_type="evt_dsa_dir_flip_up",
        event_time=pd.Timestamp("2026-06-18 10:30:00"),
        dedupe_key="dsa_selector_v1.2|600519|2026-06-18T10:30:00|evt_dsa_dir_flip_up",
        logical_entity="600519",
        payload={"direction": "up", "strength": 0.85},
        state_ttl_seconds=3600,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd


@dataclass
class StrategyEventDraft:
    """策略事件草稿 - 检测器检测到事件时生成的自包含实例。

    草稿写入 DB 后成为 StrategyEvent（event_key 唯一，含 snapshot 快照）。

    Attributes:
        event_type: 事件类型（如 evt_dsa_dir_flip_up，对齐注册表 name）
        event_time: 事件发生时间（bar 时间，非消费时间）
        dedupe_key: 去重键（写入 DB 时映射为 event_key，保证幂等）
        logical_entity: 逻辑实体（如 instrument_id 字符串）
        payload: 事件负载（自包含，不依赖外部状态）
        snapshot: 事件发生时上下文快照（冻结因子值，用于证据回溯）
        state_ttl_seconds: 状态有效期（秒），超时后状态机窗口过期
    """

    event_type: str
    event_time: datetime
    dedupe_key: str
    logical_entity: str
    payload: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    state_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        """校验草稿字段合法性。"""
        if not self.event_type:
            raise ValueError("event_type 不能为空")
        if not self.dedupe_key:
            raise ValueError("dedupe_key 不能为空（用于幂等去重）")
        if self.state_ttl_seconds < 0:
            raise ValueError(f"state_ttl_seconds 不能为负: {self.state_ttl_seconds}")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（用于日志/调试）。"""
        return {
            "event_type": self.event_type,
            "event_time": self.event_time.isoformat() if isinstance(self.event_time, (datetime, pd.Timestamp)) else str(self.event_time),
            "dedupe_key": self.dedupe_key,
            "logical_entity": self.logical_entity,
            "payload": self.payload,
            "snapshot": self.snapshot,
            "state_ttl_seconds": self.state_ttl_seconds,
        }


def build_dedupe_key(
    strategy_version_id: str,
    instrument_id: str,
    event_time: datetime | pd.Timestamp,
    event_type: str,
) -> str:
    """构建标准化去重键。

    格式: {strategy_version_id}|{instrument_id}|{event_time_iso}|{event_type}
    相同键的事件不重复写入 DB（event_key UNIQUE）。

    Args:
        strategy_version_id: 策略版本 ID
        instrument_id: 股票 ID
        event_time: 事件发生时间
        event_type: 事件类型

    Returns:
        去重键字符串
    """
    if isinstance(event_time, pd.Timestamp):
        time_str = event_time.isoformat()
    elif isinstance(event_time, datetime):
        time_str = event_time.isoformat()
    else:
        time_str = str(event_time)
    return f"{strategy_version_id}|{instrument_id}|{time_str}|{event_type}"


if __name__ == "__main__":
    # 自测入口：验证 StrategyEventDraft 创建与校验（无副作用）
    import uuid

    # 1. 正常创建
    draft = StrategyEventDraft(
        event_type="evt_dsa_dir_flip_up",
        event_time=datetime(2026, 6, 18, 10, 30, 0),
        dedupe_key="v1|600519|2026-06-18T10:30:00|evt_dsa_dir_flip_up",
        logical_entity="600519",
        payload={"direction": "up", "strength": 0.85},
        state_ttl_seconds=3600,
    )
    print(f"draft.event_type={draft.event_type}")
    print(f"draft.to_dict()={draft.to_dict()}")

    # 2. 校验空 dedupe_key
    try:
        StrategyEventDraft(
            event_type="test",
            event_time=datetime(2026, 6, 18),
            dedupe_key="",
            logical_entity="x",
        )
        raise AssertionError("应拒绝空 dedupe_key")
    except ValueError as e:
        print(f"空 dedupe_key 校验 ✓: {e}")

    # 3. 构建去重键
    key = build_dedupe_key(
        str(uuid.uuid4()), "600519", pd.Timestamp("2026-06-18 10:30:00"), "evt_test"
    )
    print(f"build_dedupe_key={key}")
    assert "600519" in key and "evt_test" in key
    print("OK")
