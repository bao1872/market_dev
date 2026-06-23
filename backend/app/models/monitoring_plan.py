"""MonitoringPlan / MonitoringPlanRevision / MonitoringPlanMember ORM 模型 - 监控组合方案（C5）。

[LEGACY] 本模块已从主业务流程中移除，仅保留 ORM 映射以兼容现有数据库表。

对应迁移 008_monitoring_plans 中的三张表：
- monitoring_plans: 监控方案主表（user_id 由认证上下文注入）
- monitoring_plan_revisions: 方案版本（mode/窗口/冷却配置，每次修改创建新 revision）
- monitoring_plan_members: 方案成员（role: TRIGGER/CONFIRM/VETO/OBSERVE）

字段说明：
- monitoring_plans.status: draft/active/paused/archived（方案状态机）
- monitoring_plans.current_revision: 当前生效版本号（每次 PUT 创建新 revision）
- monitoring_plan_revisions.mode: INDEPENDENT/ANY/ALL（组合模式）
- monitoring_plan_revisions.confirmation_window_seconds: 确认窗口（秒）
- monitoring_plan_revisions.ordered: 是否按 position 顺序确认
- monitoring_plan_revisions.cooldown_seconds: 冷却时间（秒）
- monitoring_plan_members.role: TRIGGER/CONFIRM/VETO/OBSERVE
- monitoring_plan_members.version_policy: PINNED/STABLE_TRACK
- monitoring_plan_members.position: 成员顺序（用于 ordered 模式）

设计说明：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段不在 ORM 中映射。
- user_id 由认证上下文注入，不接受客户端传入（V1.1 安全约束）。
- 每次修改方案创建新 revision，current_revision 指向当前生效版本。
- (revision_id, position) 唯一约束：同一 revision 内成员位置不重复。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MonitoringPlan(Base):
    """监控方案主表 - 用户私有的多策略监控组合。

    status 状态机：
    - draft: 草稿（默认）
    - active: 已激活（参与监控管线）
    - paused: 已暂停（不参与监控，但保留状态）
    - archived: 已归档（不再使用）

    user_id 由认证上下文注入，不接受客户端传入。
    每次修改方案创建新 revision，current_revision 指向当前生效版本。
    """

    __tablename__ = "monitoring_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="方案 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="用户 ID（由认证上下文注入）",
    )
    name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="方案名称（1-80 字符）"
    )
    description: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="方案描述"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="draft",
        server_default="'draft'",
        comment="draft/active/paused/archived",
    )
    current_revision: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=1,
        server_default="1",
        comment="当前生效版本号",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="更新时间",
    )

    def __repr__(self) -> str:
        return (
            f"<MonitoringPlan(name={self.name!r}, status={self.status!r}, "
            f"current_revision={self.current_revision})>"
        )


class MonitoringPlanRevision(Base):
    """监控方案版本 - 每次修改方案创建新 revision。

    mode 组合模式：
    - INDEPENDENT: 每个成员独立触发，不关联
    - ANY: 第一个符合事件确认即触发
    - ALL: 所有 required 成员事件确认才触发

    confirmation_window_seconds: ALL 模式下确认窗口（秒），TRIGGER 事件打开窗口后
        在此时间内需收到所有 required CONFIRM 成员事件。
    ordered: 是否按成员 position 顺序确认（ALL 模式）。
    cooldown_seconds: 组合事件触发后的冷却时间（秒），期间不重复触发。
    process_event_policy: 事件处理策略 NONE/IN_APP_ONLY/ALL_CHANNELS。
    notification_config: 通知配置 JSONB（channels/template_key 等）。
    """

    __tablename__ = "monitoring_plan_revisions"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="版本 ID",
    )
    monitoring_plan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitoring_plans.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属方案 ID",
    )
    revision: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="版本号（从 1 开始递增）"
    )
    mode: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="INDEPENDENT/ANY/ALL"
    )
    confirmation_window_seconds: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=0,
        server_default="0",
        comment="确认窗口（秒），ALL 模式下使用",
    )
    ordered: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=func.false(),
        comment="是否按 position 顺序确认",
    )
    cooldown_seconds: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=600,
        server_default="600",
        comment="冷却时间（秒）",
    )
    process_event_policy: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="IN_APP_ONLY",
        server_default="'IN_APP_ONLY'",
        comment="NONE/IN_APP_ONLY/ALL_CHANNELS",
    )
    notification_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="通知配置 JSONB",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="创建者用户 ID",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )

    __table_args__ = (
        UniqueConstraint(
            "monitoring_plan_id", "revision", name="monitoring_plan_revisions_plan_revision_key"
        ),
        CheckConstraint(
            "mode IN ('INDEPENDENT','ANY','ALL')",
            name="monitoring_plan_revisions_mode_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MonitoringPlanRevision(revision={self.revision}, "
            f"mode={self.mode!r}, window={self.confirmation_window_seconds}s)>"
        )


class MonitoringPlanMember(Base):
    """监控方案成员 - 方案版本下的策略事件成员。

    role 角色：
    - TRIGGER: 触发成员（ALL 模式下打开窗口）
    - CONFIRM: 确认成员（在窗口内确认）
    - VETO: 否决成员（在终态前出现则取消窗口）
    - OBSERVE: 观察成员（仅记录，不影响状态机）

    version_policy:
    - PINNED: 固定版本（strategy_version_id 必填）
    - STABLE_TRACK: 跟踪稳定版本（strategy_version_id 可空，运行时解析）

    position: 成员顺序（用于 ordered=true 的 ALL 模式）。
    required: 是否必需（ALL 模式下 required=true 的成员必须全部确认）。
    enabled: 是否启用（false 则该成员不参与状态机）。
    params: 成员参数 JSONB（覆盖策略默认参数）。
    conditions: 成员条件 JSONB（线性 AND 条件，对齐 selection_plan 设计）。
    """

    __tablename__ = "monitoring_plan_members"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="成员 ID",
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("monitoring_plan_revisions.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属版本 ID",
    )
    strategy_definition_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_definitions.id"),
        nullable=False,
        comment="策略定义 ID",
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        nullable=True,
        comment="策略版本 ID（version_policy=PINNED 时必填）",
    )
    version_policy: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="PINNED/STABLE_TRACK"
    )
    event_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="事件类型（如 evt_dsa_dir_flip_up）"
    )
    role: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="TRIGGER/CONFIRM/VETO/OBSERVE"
    )
    position: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="成员顺序（用于 ordered 模式）"
    )
    required: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=True,
        server_default=func.true(),
        comment="是否必需（ALL 模式下使用）",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=True,
        server_default=func.true(),
        comment="是否启用",
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="成员参数 JSONB",
    )
    conditions: Mapped[list[Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'[]'"),
        comment="成员条件 JSONB（线性 AND 条件）",
    )

    __table_args__ = (
        UniqueConstraint("revision_id", "position", name="monitoring_plan_members_revision_position_key"),
        CheckConstraint(
            "version_policy IN ('PINNED','STABLE_TRACK')",
            name="monitoring_plan_members_version_policy_check",
        ),
        CheckConstraint(
            "role IN ('TRIGGER','CONFIRM','VETO','OBSERVE')",
            name="monitoring_plan_members_role_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MonitoringPlanMember(event_type={self.event_type!r}, "
            f"role={self.role!r}, position={self.position})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    for cls in (MonitoringPlan, MonitoringPlanRevision, MonitoringPlanMember):
        cols = [c.name for c in cls.__table__.columns]
        print(f"{cls.__name__} table={cls.__tablename__} columns={cols}")

    # 验证 MonitoringPlan 关键列
    plan_cols = [c.name for c in MonitoringPlan.__table__.columns]
    for required in ["id", "user_id", "name", "status", "current_revision", "created_at", "updated_at"]:
        assert required in plan_cols, f"MonitoringPlan 缺少列: {required}"

    # 验证 MonitoringPlanRevision 关键列与约束
    rev_cols = [c.name for c in MonitoringPlanRevision.__table__.columns]
    for required in ["id", "monitoring_plan_id", "revision", "mode", "confirmation_window_seconds",
                     "ordered", "cooldown_seconds", "process_event_policy", "notification_config",
                     "created_by", "created_at"]:
        assert required in rev_cols, f"MonitoringPlanRevision 缺少列: {required}"

    # 验证 mode check 约束存在
    rev_checks = [c for c in MonitoringPlanRevision.__table__.constraints
                  if isinstance(c, CheckConstraint)]
    assert any("mode IN" in str(c.sqltext) for c in rev_checks), "mode check 约束缺失"

    # 验证 MonitoringPlanMember 关键列与约束
    mem_cols = [c.name for c in MonitoringPlanMember.__table__.columns]
    for required in ["id", "revision_id", "strategy_definition_id", "strategy_version_id",
                     "version_policy", "event_type", "role", "position", "required",
                     "enabled", "params", "conditions"]:
        assert required in mem_cols, f"MonitoringPlanMember 缺少列: {required}"

    # 验证 role check 约束存在
    mem_checks = [c for c in MonitoringPlanMember.__table__.constraints
                  if isinstance(c, CheckConstraint)]
    assert any("role IN" in str(c.sqltext) for c in mem_checks), "role check 约束缺失"
    assert any("version_policy IN" in str(c.sqltext) for c in mem_checks), \
        "version_policy check 约束缺失"

    print("OK")
