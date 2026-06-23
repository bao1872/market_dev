"""CompositeMonitorEvent / CompositeEventEvidence ORM 模型 - 组合监控事件与证据（C8）。

[LEGACY] 本模块已从主业务流程中移除，仅保留 ORM 映射以兼容现有数据库表。

对应迁移 008_monitoring_plans 中的两张表：
- composite_monitor_events: 组合监控事件（composite_event_key 唯一，幂等写入）
- composite_event_evidence: 组合事件证据链（引用 StrategyEvent ID，冻结策略版本/事件类型/事件时间/摘要）

字段说明：
- composite_monitor_events.composite_event_key: 唯一键（hash(revision_id + instrument_id + event_type + event_time + member_ids)）
- composite_monitor_events.event_type: 组合事件类型（如 composite_confirmed/composite_vetoed）
- composite_monitor_events.event_time: 组合事件时间（由 event_time 驱动，非墙钟）
- composite_monitor_events.payload: 组合事件负载 JSONB（含 state/member_count/计算时间等）
- composite_event_evidence.summary: 证据摘要 JSONB（冻结策略版本/事件类型/事件时间/摘要字段）

设计说明：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段不在 ORM 中映射。
- composite_event_key UNIQUE 约束保证幂等：相同 key 的组合事件不重复写入。
- Evidence 引用 StrategyEvent ID，并冻结关键信息（即使原事件后续修改，证据不变）。
- 索引 ix_composite_event_user_time 支持按用户 + 时间倒序查询。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CompositeMonitorEvent(Base):
    """组合监控事件 - 多策略成员事件关联后生成的组合事件。

    composite_event_key 唯一约束保证幂等：
    相同 (revision_id + instrument_id + event_type + event_time + member_ids) 的组合事件不重复写入。

    event_type 取值：
    - composite_confirmed: ALL 模式下所有 required 成员确认
    - composite_triggered_any: ANY 模式下首个成员触发
    - composite_triggered_independent: INDEPENDENT 模式下单成员触发
    - composite_vetoed: 被 VETO 事件否决

    event_time 由 event_time 驱动（非墙钟），对齐 V1.1 状态机约束。
    payload 含 state/member_count/计算时间等可解释性信息。
    """

    __tablename__ = "composite_monitor_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="组合事件 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="用户 ID",
    )
    monitoring_plan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitoring_plans.id"),
        nullable=False,
        comment="方案 ID",
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitoring_plan_revisions.id"),
        nullable=False,
        comment="方案版本 ID",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="股票 ID",
    )
    event_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="组合事件类型"
    )
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="组合事件时间（由 event_time 驱动，非墙钟）",
    )
    composite_event_key: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        unique=True,
        comment="组合事件唯一键（幂等去重）",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="组合事件负载 JSONB（state/member_count/计算时间等）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )

    __table_args__ = (
        Index(
            "ix_composite_event_user_time",
            "user_id",
            func.text("event_time DESC"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CompositeMonitorEvent(event_type={self.event_type!r}, "
            f"event_time={self.event_time!r}, key={self.composite_event_key[:16]!r})>"
        )


class CompositeEventEvidence(Base):
    """组合事件证据链 - 每个成员确认引用 StrategyEvent ID，并冻结关键信息。

    复合主键 (composite_event_id, member_id, strategy_event_id)：
    同一组合事件下同一成员同一原始事件只记录一条证据。

    冻结字段（即使原 StrategyEvent 后续修改，证据不变）：
    - summary JSONB 含 strategy_version_id/event_type/event_time/摘要字段

    设计目的：
    - 证据回溯：通过 strategy_event_id 可定位原始事件
    - 证据冻结：summary 冻结关键信息，原事件修改不影响历史证据
    - 可解释性：API 返回组合事件详情时含 evidence 列表
    """

    __tablename__ = "composite_event_evidence"

    composite_event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("composite_monitor_events.id", ondelete="CASCADE"),
        primary_key=True,
        comment="组合事件 ID（复合主键之一）",
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitoring_plan_members.id"),
        primary_key=True,
        comment="成员 ID（复合主键之一）",
    )
    strategy_event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_events.id"),
        primary_key=True,
        comment="原始策略事件 ID（复合主键之一）",
    )
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="证据摘要 JSONB（冻结策略版本/事件类型/事件时间/摘要）",
    )

    def __repr__(self) -> str:
        return (
            f"<CompositeEventEvidence(composite_event_id={self.composite_event_id!r}, "
            f"member_id={self.member_id!r}, strategy_event_id={self.strategy_event_id!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    for cls in (CompositeMonitorEvent, CompositeEventEvidence):
        cols = [c.name for c in cls.__table__.columns]
        print(f"{cls.__name__} table={cls.__tablename__} columns={cols}")

    # 验证 CompositeMonitorEvent 关键列
    ev_cols = [c.name for c in CompositeMonitorEvent.__table__.columns]
    for required in ["id", "user_id", "monitoring_plan_id", "revision_id", "instrument_id",
                     "event_type", "event_time", "composite_event_key", "payload", "created_at"]:
        assert required in ev_cols, f"CompositeMonitorEvent 缺少列: {required}"

    # 验证 composite_event_key 唯一约束
    ev_key_col = CompositeMonitorEvent.__table__.columns["composite_event_key"]
    assert ev_key_col.unique, "composite_event_key 应有 UNIQUE 约束"

    # 验证索引
    ev_indexes = [idx.name for idx in CompositeMonitorEvent.__table__.indexes]
    print(f"CompositeMonitorEvent indexes={ev_indexes}")
    assert "ix_composite_event_user_time" in ev_indexes

    # 验证 CompositeEventEvidence 复合主键
    ev_pk_cols = [c.name for c in CompositeEventEvidence.__table__.primary_key.columns]
    print(f"CompositeEventEvidence primary_key={ev_pk_cols}")
    assert ev_pk_cols == ["composite_event_id", "member_id", "strategy_event_id"], \
        f"复合主键不匹配: {ev_pk_cols}"

    print("OK")
