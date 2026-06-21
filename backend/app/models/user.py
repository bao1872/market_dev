"""User / Role / UserRole ORM 模型 - 用户与权限基础表。

对应迁移 001_users：
- users: 用户主表（email 唯一，status 状态机 active/disabled/pending）
- roles: 角色表（name 唯一，如 admin/user）
- user_roles: 用户-角色关联表（多对多，复合主键 user_id + role_id）

设计要点：
- 用户密码以 bcrypt 哈希存储（password_hash），不保存明文
- status 状态机：active（可用）/ disabled（禁用）/ pending（待激活）
- 用户与角色多对多关系，通过 user_roles 关联表维护
- 私有资源的 user_id 由认证上下文注入，不接受客户端传入（V1.1 安全约束）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """用户主表 - email 唯一标识，password_hash 存储 bcrypt 哈希。

    status 状态机：
    - active: 可正常登录与使用
    - disabled: 被管理员禁用，不可登录
    - pending: 待激活（如邮箱未验证）
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    email: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="登录邮箱（唯一）"
    )
    password_hash: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="bcrypt 密码哈希"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        comment="active/disabled/pending",
    )
    timezone: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="Asia/Shanghai",
        server_default="Asia/Shanghai",
        comment="用户时区",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<User(email={self.email!r}, status={self.status!r})>"


class Role(Base):
    """角色表 - name 唯一，如 admin/user/strategy_author。

    用于 RBAC 权限控制，通过 user_roles 关联到用户。
    """

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="角色名（唯一，如 admin/user）"
    )
    description: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="角色描述"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Role(name={self.name!r})>"


class UserRole(Base):
    """用户-角色关联表 - 多对多关系，复合主键 (user_id, role_id)。

    ondelete=CASCADE：用户或角色删除时，关联记录自动删除。
    """

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="用户 ID",
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
        comment="角色 ID",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<UserRole(user_id={self.user_id!r}, role_id={self.role_id!r})>"


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    for cls in (User, Role, UserRole):
        cols = [c.name for c in cls.__table__.columns]
        print(f"{cls.__name__} table={cls.__tablename__} columns={cols}")
        assert "id" in cols or "user_id" in cols
    # 验证关键约束
    assert User.__table__.c.email.unique is True
    assert Role.__table__.c.name.unique is True
    print("OK")
