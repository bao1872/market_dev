"""AccessAuditLog ORM 模型 - 访问审计日志表（记录 admin 关键操作）。

对应迁移：
- 052_access_audit_logs: 创建 access_audit_logs 表 + 2 个索引

表结构：
- access_audit_logs: 审计日志主表，记录 admin 关键操作的完整上下文

设计要点：
- actor_user_id: 操作者（admin）user_id，FK users.id（不用 ondelete CASCADE，
  审计日志需保留历史，用户删除时不联动删除日志）
- action: 操作类型字符串，约定格式 "<target_type>.<verb>"，如 invite_code.create
- target_type / target_id: 目标对象类型与 ID；target_id 用 String 兼容 UUID/其他
- before_data / after_data: 操作前后状态快照（JSONB），便于审计追溯
- request_id: 请求追踪 ID（可选，用于关联请求链路）
- ip_hash: IP 哈希（不存明文 IP，符合 docs/安全规范.md 隐私要求）
- created_at: 操作时间（带时区，server_default now()）
- 索引：(actor_user_id, created_at) + (target_type, target_id, created_at)

接入位置：
- app.services.access_audit_service.write_audit_log / query_audit_logs 统一接口
- admin 端点（admin_subscription.py 等）在业务操作后调用 write_audit_log
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AccessAuditLog(Base):
    """访问审计日志 - 记录 admin 关键操作的完整上下文。

    字段语义：
    - actor_user_id: 操作者 user_id（admin），FK users.id（不级联删除）
    - action: 操作类型，约定格式 "<target_type>.<verb>"
      （如 invite_code.create / invite_code.revoke / user.disable）
    - target_type: 目标对象类型（如 invite_code / user / subscription）
    - target_id: 目标对象 ID（字符串，兼容 UUID/其他）
    - before_data: 操作前状态快照（JSONB，可空）
    - after_data: 操作后状态快照（JSONB，可空）
    - request_id: 请求追踪 ID（可空，用于关联请求链路）
    - ip_hash: IP 哈希（可空，不存明文 IP）
    - created_at: 操作时间（带时区，server_default now()）

    写入约束：
    - 通过 app.services.access_audit_service.write_audit_log 写入
    - write_audit_log 不 commit，由调用方控制事务（保证与业务操作同事务原子性）
    """

    __tablename__ = "access_audit_logs"
    __table_args__ = (
        Index(
            "idx_access_audit_logs_actor_created",
            "actor_user_id",
            "created_at",
        ),
        Index(
            "idx_access_audit_logs_target",
            "target_type",
            "target_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="操作者 user_id（admin）",
    )
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="操作类型 invite_code.create/invite_code.revoke/user.disable 等",
    )
    target_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="目标对象类型 invite_code/user/subscription 等",
    )
    target_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="目标对象 ID（字符串，兼容 UUID/其他）",
    )
    before_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=String(), none_as_null=True),
        nullable=True,
        comment="操作前状态快照",
    )
    after_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=String(), none_as_null=True),
        nullable=True,
        comment="操作后状态快照",
    )
    request_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="请求追踪 ID",
    )
    ip_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="IP 哈希（不存明文 IP）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="操作时间",
    )

    def __repr__(self) -> str:
        return (
            f"<AccessAuditLog(actor_user_id={self.actor_user_id!r}, "
            f"action={self.action!r}, target_type={self.target_type!r}, "
            f"target_id={self.target_id!r})>"
        )


if __name__ == "__main__":
    # [AuditLog] - 描述: 自测入口，验证 ORM 模型映射（无副作用，不连接数据库）
    assert AccessAuditLog.__tablename__ == "access_audit_logs"
    columns = {c.name for c in AccessAuditLog.__table__.columns}
    expected = {
        "id",
        "actor_user_id",
        "action",
        "target_type",
        "target_id",
        "before_data",
        "after_data",
        "request_id",
        "ip_hash",
        "created_at",
    }
    assert columns == expected, f"AccessAuditLog 列不匹配: {columns ^ expected}"
    # 必填字段
    assert AccessAuditLog.__table__.c.actor_user_id.nullable is False
    assert AccessAuditLog.__table__.c.action.nullable is False
    assert AccessAuditLog.__table__.c.target_type.nullable is False
    assert AccessAuditLog.__table__.c.created_at.nullable is False
    # 可空字段
    assert AccessAuditLog.__table__.c.target_id.nullable is True
    assert AccessAuditLog.__table__.c.before_data.nullable is True
    assert AccessAuditLog.__table__.c.after_data.nullable is True
    assert AccessAuditLog.__table__.c.request_id.nullable is True
    assert AccessAuditLog.__table__.c.ip_hash.nullable is True
    # 验证索引
    index_names = {idx.name for idx in AccessAuditLog.__table__.indexes}  # type: ignore[attr-defined]
    assert "idx_access_audit_logs_actor_created" in index_names
    assert "idx_access_audit_logs_target" in index_names
    print(f"AccessAuditLog columns={sorted(columns)}")
    print(f"indexes={sorted(index_names)}")
    print("OK: AccessAuditLog 模型表结构验证通过")
