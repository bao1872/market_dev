"""MonitoringPlanState ORM 模型 - 监控组合状态仓储（C6）。

[LEGACY] 本模块已从主业务流程中移除，仅保留 ORM 映射以兼容现有数据库表。

对应迁移 008_monitoring_plans 中的 monitoring_plan_states 表：
- id: UUID 主键
- user_id + revision_id + instrument_id: 唯一约束（同一用户同一方案版本同一股票只有一条状态）
- status: 状态机字段（WAITING_TRIGGER/WAITING_CONFIRM/CONFIRMED/EXPIRED/VETOED/COOLDOWN）
- lock_version: 乐观锁版本号（每次更新 +1，冲突时重读重放）
- window_started_at / window_deadline_at: 窗口起止时间（ALL 模式下使用）
- cooldown_until: 冷却截止时间（CONFIRMED 后进入冷却）
- confirmed_member_ids: 已确认成员 ID 列表
- vetoed_by_member_id: 否决该状态的成员 ID
- state_payload: 状态附加信息 JSONB（pending_events 等）
- updated_at: 更新时间

设计说明：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段不在 ORM 中映射。
- UNIQUE(user_id, revision_id, instrument_id) 保证状态唯一性。
- lock_version 乐观锁：每次更新 +1，冲突时重读重放，禁止最后写覆盖。
- 状态机不依赖墙钟：所有时间字段由 event_time 驱动（不使用 datetime.now()）。
- 索引 ix_monitor_state_expiry 支持 watermark 超时扫描。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MonitoringPlanState(Base):
    """监控组合状态 - 每个 (用户, 方案版本, 股票) 组合的当前状态。

    状态机字段 status：
    - WAITING_TRIGGER: 等待 TRIGGER 事件（ALL 模式初始状态）
    - WAITING_CONFIRM: 已收到 TRIGGER，等待 CONFIRM 事件（窗口已打开）
    - CONFIRMED: 所有 required 成员已确认（终态，进入冷却）
    - EXPIRED: 窗口超时未确认（终态）
    - VETOED: 被 VETO 事件否决（终态）
    - COOLDOWN: 冷却中（CONFIRMED 后进入，cooldown_until 后回到 WAITING_TRIGGER）

    lock_version 乐观锁：
    - 每次更新 +1
    - 更新时 WHERE lock_version = ? 不匹配则冲突
    - 冲突时重读最新状态并重放事件（禁止最后写覆盖）

    时间字段全部由 event_time 驱动，不使用 datetime.now()（V1.1 状态机约束）。
    """

    __tablename__ = "monitoring_plan_states"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="状态 ID",
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
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="WAITING_TRIGGER/WAITING_CONFIRM/CONFIRMED/EXPIRED/VETOED/COOLDOWN",
    )
    window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="窗口开始时间（TRIGGER 事件时间）",
    )
    window_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="窗口截止时间（window_started_at + confirmation_window_seconds）",
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="冷却截止时间（CONFIRMED 后进入冷却）",
    )
    confirmed_member_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="已确认成员 ID 列表",
    )
    vetoed_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="否决该状态的成员 ID",
    )
    state_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="状态附加信息 JSONB（pending_events/last_event_at 等）",
    )
    lock_version: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=0,
        server_default="0",
        comment="乐观锁版本号（每次更新 +1）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="更新时间",
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "revision_id", "instrument_id",
            name="monitoring_plan_states_user_revision_instrument_key",
        ),
        Index(
            "ix_monitor_state_expiry",
            "status", "window_deadline_at", "cooldown_until",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MonitoringPlanState(revision_id={self.revision_id!r}, "
            f"instrument_id={self.instrument_id!r}, status={self.status!r}, "
            f"lock_version={self.lock_version})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"MonitoringPlanState.__tablename__={MonitoringPlanState.__tablename__}")
    cols = [c.name for c in MonitoringPlanState.__table__.columns]
    print(f"columns={cols}")

    # 验证关键列存在
    for required in ["id", "user_id", "monitoring_plan_id", "revision_id", "instrument_id",
                     "status", "window_started_at", "window_deadline_at", "cooldown_until",
                     "confirmed_member_ids", "vetoed_by_member_id", "state_payload",
                     "lock_version", "updated_at"]:
        assert required in cols, f"缺少列: {required}"

    # 验证唯一约束 (user_id, revision_id, instrument_id)
    uq_constraints = [
        c for c in MonitoringPlanState.__table__.constraints
        if hasattr(c, "columns") and len(c.columns) == 3
    ]
    print(f"unique_constraints_count={len(uq_constraints)}")
    assert any(
        {col.name for col in c.columns} == {"user_id", "revision_id", "instrument_id"}
        for c in uq_constraints
    ), "缺少 (user_id, revision_id, instrument_id) 唯一约束"

    # 验证索引存在
    indexes = [idx.name for idx in MonitoringPlanState.__table__.indexes]
    print(f"indexes={indexes}")
    assert "ix_monitor_state_expiry" in indexes

    print("OK")
