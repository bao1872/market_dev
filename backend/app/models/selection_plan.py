"""选股组合方案 ORM 模型 - 方案/版本/成员/条件（C1）。

对应迁移 007_selection_plans 中的方案侧四张表：
- selection_plans: 选股方案主表（user_id 隔离，status 状态机，current_revision 当前版本号）
- selection_plan_revisions: 方案版本（revision 递增，operator=ALL/ANY，
  missing_member_policy=FAIL_CLOSED/IGNORE_MEMBER，universe/sort_spec/notification_config 为 JSONB）
- selection_plan_members: 方案成员（绑定 strategy_definition_id + 可选 strategy_version_id，
  version_policy=PINNED/STABLE_TRACK，position 决定成员顺序，enabled 控制启用，params 为 JSONB）
- selection_member_conditions: 成员条件（线性 AND，position 决定条件顺序，
  operator=gt/gte/lt/lte/eq/between，value1/value2 为 JSONB 支持 BETWEEN）

字段映射说明（任务描述 → 迁移 DDL，以迁移为准）：
- SelectionPlan.is_active → status(text, default 'draft') + current_revision(int)
- SelectionPlanRevision.version_policy/is_current → 迁移无此字段（version_policy 下沉到 member）
- SelectionPlanRevision.revision_number → revision
- SelectionPlanMember.member_name/weight/parameters → strategy_definition_id/position/enabled/params
- SelectionPlanCondition.value/logical_op → value1/value2（支持 BETWEEN），无 logical_op（恒为 AND）

枚举值（与 schema 文件 + 迁移 CHECK 约束一致）：
- operator: ALL / ANY
- missing_member_policy: FAIL_CLOSED / IGNORE_MEMBER
- version_policy: PINNED / STABLE_TRACK
- condition operator: gt / gte / lt / lte / eq / between
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class SelectionPlan(Base):
    """选股方案主表 - 用户拥有的选股组合方案。

    对应迁移 007 selection_plans 表。
    user_id 隔离：私有资源，user_id 由认证上下文注入（不接受客户端传入）。
    status 状态机：draft / active / archived（由业务层控制，迁移未加 CHECK 约束）。
    current_revision：当前生效版本号，更新方案时递增并创建新 revision。

    字段映射（任务描述 → 迁移 DDL）：
    - is_active(bool) → status(text) + current_revision(int)
    """

    __tablename__ = "selection_plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="方案 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="所属用户 ID（认证上下文注入）",
    )
    name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="方案名称（最长 80）"
    )
    description: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="方案描述（最长 500）"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default="'draft'",
        comment="方案状态：draft/active/archived",
    )
    current_revision: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
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
            f"<SelectionPlan(name={self.name!r}, "
            f"status={self.status!r}, current_revision={self.current_revision})>"
        )


class SelectionPlanRevision(Base):
    """方案版本 - 不可变快照，每次更新方案创建新 revision。

    对应迁移 007 selection_plan_revisions 表。
    revision 与 selection_plan_id 构成唯一约束。
    operator/missing_member_policy 由迁移 CHECK 约束校验枚举值。
    universe/sort_spec/notification_config 为 JSONB，存储方案级配置。

    字段映射（任务描述 → 迁移 DDL）：
    - version_policy/is_current → 迁移无此字段（version_policy 下沉到 member 级别）
    - revision_number → revision
    """

    __tablename__ = "selection_plan_revisions"
    __table_args__ = (
        UniqueConstraint("selection_plan_id", "revision", name="selection_plan_revisions_uniq"),
        CheckConstraint(
            "operator IN ('ALL','ANY')",
            name="selection_plan_revisions_operator_check",
        ),
        CheckConstraint(
            "missing_member_policy IN ('FAIL_CLOSED','IGNORE_MEMBER')",
            name="selection_plan_revisions_missing_member_policy_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="版本 ID",
    )
    selection_plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plans.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属方案 ID",
    )
    revision: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="版本号（递增）"
    )
    operator: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="集合运算：ALL(交集)/ANY(并集)"
    )
    missing_member_policy: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default="'FAIL_CLOSED'",
        comment="成员缺失策略：FAIL_CLOSED(失败)/IGNORE_MEMBER(忽略)",
    )
    universe: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="标的范围配置（markets/exclude_st 等）",
    )
    sort_spec: Mapped[list[Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'[]'"),
        comment="排名规格（白名单表达式列表）",
    )
    notification_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="通知配置",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
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

    # 关系：版本 -> 成员列表（一对多，按 position 排序由调用方处理）
    members: Mapped[list[SelectionPlanMember]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="SelectionPlanMember.position",
    )

    def __repr__(self) -> str:
        return (
            f"<SelectionPlanRevision(revision={self.revision}, "
            f"operator={self.operator!r})>"
        )


class SelectionPlanMember(Base):
    """方案成员 - 绑定一个策略定义/版本，参与组合运算。

    对应迁移 007 selection_plan_members 表。
    strategy_definition_id 必填，strategy_version_id 可选（PINNED 时必填）。
    version_policy: PINNED(锁定指定版本) / STABLE_TRACK(跟踪最新 released 版本)。
    position 决定成员顺序，enabled 控制是否参与运算。

    字段映射（任务描述 → 迁移 DDL）：
    - member_name/weight/parameters → strategy_definition_id/position/enabled/params
    """

    __tablename__ = "selection_plan_members"
    __table_args__ = (
        UniqueConstraint("revision_id", "position", name="selection_plan_members_uniq"),
        CheckConstraint(
            "version_policy IN ('PINNED','STABLE_TRACK')",
            name="selection_plan_members_version_policy_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="成员 ID",
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plan_revisions.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属版本 ID",
    )
    strategy_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_definitions.id"),
        nullable=False,
        comment="策略定义 ID",
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        nullable=True,
        comment="策略版本 ID（PINNED 必填，STABLE_TRACK 可空）",
    )
    version_policy: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="版本策略：PINNED(锁定版本)/STABLE_TRACK(跟踪最新)",
    )
    position: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="成员顺序（同 revision 内唯一）"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default=func.text("true"),
        comment="是否启用",
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="成员参数（JSONB）",
    )

    # 关系：成员 -> 所属版本（多对一）+ 条件列表（一对多）
    revision: Mapped[SelectionPlanRevision] = relationship(back_populates="members")
    conditions: Mapped[list[SelectionMemberCondition]] = relationship(
        back_populates="member",
        cascade="all, delete-orphan",
        order_by="SelectionMemberCondition.position",
    )

    def __repr__(self) -> str:
        return (
            f"<SelectionPlanMember(position={self.position}, "
            f"version_policy={self.version_policy!r}, enabled={self.enabled})>"
        )


class SelectionMemberCondition(Base):
    """成员条件 - 线性 AND 筛选条件。

    对应迁移 007 selection_member_conditions 表（注意：DDL 表名无 plan 前缀）。
    条件之间恒为 AND 关系（迁移未提供 logical_op 字段）。
    operator 枚举：gt/gte/lt/lte/eq/between（与 schema 文件一致）。
    value1 为主值，value2 为 BETWEEN 上界（仅 between 操作使用）。

    字段映射（任务描述 → 迁移 DDL）：
    - value(numeric)/logical_op(AND) → value1(JSONB)/value2(JSONB)，无 logical_op（恒 AND）
    - operator(GT/LT/.../BETWEEN) → operator(gt/gte/lt/lte/eq/between)（小写）
    """

    __tablename__ = "selection_member_conditions"
    __table_args__ = (
        UniqueConstraint("member_id", "position", name="selection_member_conditions_uniq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="条件 ID",
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plan_members.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属成员 ID",
    )
    position: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="条件顺序（同 member 内唯一）"
    )
    metric_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="指标名（如 dsa_dir_bars）"
    )
    operator: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="比较操作：gt/gte/lt/lte/eq/between",
    )
    value1: Mapped[Any] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        comment="主值（JSONB，支持数值/字符串）",
    )
    value2: Mapped[Any | None] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=True,
        comment="上界值（仅 between 操作使用）",
    )

    # 关系：条件 -> 所属成员（多对一）
    member: Mapped[SelectionPlanMember] = relationship(back_populates="conditions")

    def __repr__(self) -> str:
        return (
            f"<SelectionMemberCondition(metric_key={self.metric_key!r}, "
            f"operator={self.operator!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"SelectionPlan.__tablename__={SelectionPlan.__tablename__}")
    plan_cols = [c.name for c in SelectionPlan.__table__.columns]
    print(f"SelectionPlan columns={plan_cols}")
    for required in ["id", "user_id", "name", "description", "status", "current_revision", "created_at", "updated_at"]:
        assert required in plan_cols, f"SelectionPlan 缺少列: {required}"

    print(f"SelectionPlanRevision.__tablename__={SelectionPlanRevision.__tablename__}")
    rev_cols = [c.name for c in SelectionPlanRevision.__table__.columns]
    print(f"SelectionPlanRevision columns={rev_cols}")
    for required in ["id", "selection_plan_id", "revision", "operator", "missing_member_policy", "universe", "sort_spec", "notification_config", "created_by", "created_at"]:
        assert required in rev_cols, f"SelectionPlanRevision 缺少列: {required}"

    print(f"SelectionPlanMember.__tablename__={SelectionPlanMember.__tablename__}")
    mem_cols = [c.name for c in SelectionPlanMember.__table__.columns]
    print(f"SelectionPlanMember columns={mem_cols}")
    for required in ["id", "revision_id", "strategy_definition_id", "strategy_version_id", "version_policy", "position", "enabled", "params"]:
        assert required in mem_cols, f"SelectionPlanMember 缺少列: {required}"

    print(f"SelectionMemberCondition.__tablename__={SelectionMemberCondition.__tablename__}")
    cond_cols = [c.name for c in SelectionMemberCondition.__table__.columns]
    print(f"SelectionMemberCondition columns={cond_cols}")
    for required in ["id", "member_id", "position", "metric_key", "operator", "value1", "value2"]:
        assert required in cond_cols, f"SelectionMemberCondition 缺少列: {required}"

    # 验证 CHECK 约束存在
    rev_constraints = [c.name for c in SelectionPlanRevision.__table__.constraints if hasattr(c, "name") and c.name]
    print(f"Revision constraints={rev_constraints}")
    assert "selection_plan_revisions_operator_check" in rev_constraints
    assert "selection_plan_revisions_missing_member_policy_check" in rev_constraints

    mem_constraints = [c.name for c in SelectionPlanMember.__table__.constraints if hasattr(c, "name") and c.name]
    print(f"Member constraints={mem_constraints}")
    assert "selection_plan_members_version_policy_check" in mem_constraints

    print("OK")
