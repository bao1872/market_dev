"""Plan ORM 模型 - 套餐定义表（plans）。

对应迁移：
- 048_plans_table: 创建 plans 表 + 初始化 observe_20/research_50 两条记录

表结构：
- plans: 套餐定义表（plan_code 唯一，记录 monitor_limit/notification_channel_limit/
  message_retention_days/features 等套餐契约字段）

设计要点：
- plans 表是套餐定义的唯一真源（替代旧 app/constants/plan_contract.py 的 PLAN_CONTRACTS 字典）
- observe_20: 观察版，monitor_limit=20，6 个 features
- research_50: 研究版，monitor_limit=50，7 个 features（含 advanced_export）
- plan_code 字符串常量 DEFAULT_PLAN_CODE 在 app/constants/plan_codes.py 中定义（管理员无套餐）
- 业务代码通过 app.services.plan_service 查询 plans 表，禁止硬编码套餐字段
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Plan(Base):
    """套餐定义表 - 套餐契约的唯一真源。

    字段语义：
    - plan_code: 套餐代码（observe_20/research_50），业务代码引用此标识
    - display_name: 展示名称（如"观察版"/"研究版"），用于 UI 显示
    - monitor_limit: 监控数量上限（observe_20=20, research_50=50）
    - notification_channel_limit: 通知渠道数量上限
    - message_retention_days: 消息保留天数
    - features: 功能特性列表（JSONB 数组，如 ["trend_selection", ...]）
    - status: 状态（active/inactive），仅 active 套餐可用于新邀请码

    设计说明：
    - 业务代码通过 plan_service.get_plan 查询，未知 plan_code 抛 ValueError
    - monitor_limit 字面量 20/50 只允许在此表与初始化迁移中出现
    """

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    plan_code: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        unique=True,
        comment="套餐代码 observe_20/research_50",
    )
    display_name: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="套餐展示名称（观察版/研究版）",
    )
    monitor_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="监控数量上限",
    )
    notification_channel_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=sa_text("1"),
        comment="通知渠道数量上限",
    )
    message_retention_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        server_default=sa_text("30"),
        comment="消息保留天数",
    )
    features: Mapped[list[Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=sa_text("'[]'"),
        comment="功能特性列表 JSONB 数组",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default=sa_text("'active'"),
        comment="状态 active/inactive",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
        comment="更新时间（每次 UPDATE 自动刷新）",
    )

    def __repr__(self) -> str:
        return (
            f"<Plan(plan_code={self.plan_code!r}, display_name={self.display_name!r}, "
            f"monitor_limit={self.monitor_limit!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    # [Plan] - 描述: 自测入口，验证 Plan 模型表结构定义（不连接数据库）
    assert Plan.__tablename__ == "plans"
    columns = {c.name for c in Plan.__table__.columns}
    expected = {
        "id",
        "plan_code",
        "display_name",
        "monitor_limit",
        "notification_channel_limit",
        "message_retention_days",
        "features",
        "status",
        "created_at",
        "updated_at",
    }
    assert columns == expected, f"Plan 列不匹配: {columns ^ expected}"
    # plan_code 必须唯一
    assert Plan.__table__.c.plan_code.unique is True
    # monitor_limit/notification_channel_limit/message_retention_days 不可空
    assert Plan.__table__.c.monitor_limit.nullable is False
    assert Plan.__table__.c.notification_channel_limit.nullable is False
    assert Plan.__table__.c.message_retention_days.nullable is False
    print(f"Plan columns={sorted(columns)}")
    print("OK: Plan 模型表结构验证通过")
