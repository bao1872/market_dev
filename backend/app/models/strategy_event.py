"""StrategyEvent ORM 模型 - 原始策略事件与快照（M4）。

对应迁移 006_monitor_states 中的 strategy_events 表：
- id: UUID 主键（数据库 gen_random_uuid() 生成）
- event_key: 事件唯一键（UNIQUE 约束，防止重复事件写入）
- strategy_version_id: 策略版本 FK
- instrument_id: 股票 ID
- event_type: 事件类型（如 evt_dsa_dir_flip_up）
- event_time: 事件发生时间（按 bar 时间，非消费时间）
- logical_entity_id: 逻辑实体（如 instrument_id 字符串，可空）
- schema_version: 事件 envelope schema 版本
- payload: 事件负载 JSONB（自包含，不依赖外部状态）
- snapshot: 事件发生时的完整上下文快照 JSONB（bars/state/metrics 冻结）
- created_at: 创建时间

设计说明：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段（如 dedupe_key）不在 ORM 中映射。
- event_key UNIQUE 约束保证幂等：相同 event_key 的事件不重复写入（ON CONFLICT DO NOTHING）。
- snapshot 冻结事件发生时的完整上下文，便于后续证据回溯与审计。
- 索引 ix_strategy_event_symbol_time 支持按股票 + 时间倒序查询。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StrategyEvent(Base):
    """策略事件 - 监控管线检测到的原始事件，含快照冻结。

    event_key 唯一约束保证幂等写入：相同 event_key 的事件不重复入库。
    snapshot 冻结事件发生时的完整上下文（bars/state/metrics），用于证据回溯。
    """

    __tablename__ = "strategy_events"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="事件 ID",
    )
    event_key: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="事件唯一键（幂等去重）"
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        nullable=False,
        comment="策略版本 ID",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="股票 ID"
    )
    event_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="事件类型（如 evt_dsa_dir_flip_up）"
    )
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="事件发生时间（bar 时间）"
    )
    logical_entity_id: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="逻辑实体（如 instrument_id 字符串）"
    )
    schema_version: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="事件 envelope schema 版本"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="事件负载 JSONB（自包含）"
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="事件发生时上下文快照 JSONB",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyEvent(event_key={self.event_key!r}, "
            f"event_type={self.event_type!r}, event_time={self.event_time!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"StrategyEvent.__tablename__={StrategyEvent.__tablename__}")
    cols = [c.name for c in StrategyEvent.__table__.columns]
    print(f"StrategyEvent columns={cols}")
    # 验证 event_key 唯一约束
    event_key_col = StrategyEvent.__table__.columns["event_key"]
    assert event_key_col.unique, "event_key 应有 UNIQUE 约束"
    print("event_key UNIQUE ✓")
    # 验证必需列存在
    for required in [
        "id", "event_key", "strategy_version_id", "instrument_id",
        "event_type", "event_time", "logical_entity_id", "schema_version",
        "payload", "snapshot", "created_at",
    ]:
        assert required in cols, f"缺少列: {required}"
    print("OK")
