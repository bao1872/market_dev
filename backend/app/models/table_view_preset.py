"""UserTableViewPreset ORM 模型 - 用户表格视图配置。

对应迁移 059_user_table_view_presets：
- user_table_view_presets: 用户表格视图配置（id 主键，user_id+table_id+strategy_key+name 唯一）

字段说明：
- id: UUID 主键（数据库生成 gen_random_uuid()）
- user_id: 用户 ID（FK users.id），由认证上下文注入，不接受客户端传入
- table_id: 表格标识（如 "screener" / "watchlist"），由前端约定
- strategy_key: 策略 key（可空，适用于无策略的表格）
- name: 配置名称（用户自定义，同一 user+table_id+strategy_key 下唯一）
- config: JSONB 配置内容（仅允许 keyword/sort/filters/hiddenColumns/pageSize）
- is_default: 是否默认配置（同 user+table_id+strategy_key 至多 1 个 true）
- created_at: 创建时间
- updated_at: 更新时间（由 onupdate 自动维护）

设计要点：
- (user_id, table_id, strategy_key, name) 唯一约束
- 每 user+table_id+strategy_key 最多 20 个（由应用层 quota 检查）
- config 字段类型校验由 Pydantic schema 强制（禁止 selectedKeys/page/activeRunId/rows）
- user_id 由认证上下文注入（V1.1 安全约束：私有资源 user_id 不接受 body 传入）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserTableViewPreset(Base):
    """用户表格视图配置 - 保存用户在表格（如趋势选股页）的筛选/排序/列设置。

    (user_id, table_id, strategy_key, name) 唯一约束保证同用户同表同策略下配置名不重复。
    config 仅保存视图相关字段（keyword/sort/filters/hiddenColumns/pageSize），
    禁止保存 selectedKeys/page/activeRunId/rows 等会话态或业务数据。
    """

    __tablename__ = "user_table_view_presets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
        comment="主键 UUID（客户端默认 uuid4，PostgreSQL 端 gen_random_uuid 兜底）",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="用户 ID（由认证上下文注入）",
    )
    table_id: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="表格标识（如 screener/watchlist，由前端约定）",
    )
    strategy_key: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="策略 key（可空，适用于无策略的表格）",
    )
    name: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="配置名称（用户自定义）",
    )
    config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="配置内容（仅允许 keyword/sort/filters/hiddenColumns/pageSize）",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=func.false(),
        comment="是否默认配置（同 user+table_id+strategy_key 至多 1 个 true）",
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
        onupdate=func.now(),
        comment="更新时间（由 onupdate 自动维护）",
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "table_id",
            "strategy_key",
            "name",
            name="uq_user_table_view_preset_user_table_strategy_name",
        ),
        Index(
            "ix_user_table_view_presets_user_table_strategy",
            "user_id",
            "table_id",
            "strategy_key",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<UserTableViewPreset(user_id={self.user_id!r}, "
            f"table_id={self.table_id!r}, strategy_key={self.strategy_key!r}, "
            f"name={self.name!r}, is_default={self.is_default!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"UserTableViewPreset.__tablename__={UserTableViewPreset.__tablename__}")
    cols = [c.name for c in UserTableViewPreset.__table__.columns]
    print(f"columns={cols}")
    assert "id" in cols
    assert "user_id" in cols
    assert "table_id" in cols
    assert "strategy_key" in cols
    assert "name" in cols
    assert "config" in cols
    assert "is_default" in cols
    assert "created_at" in cols
    assert "updated_at" in cols
    # 验证唯一约束
    uq_constraints = [
        c for c in UserTableViewPreset.__table__.constraints  # type: ignore[attr-defined]
        if getattr(c, "name", None) == "uq_user_table_view_preset_user_table_strategy_name"
    ]
    print(f"unique_constraints_count={len(uq_constraints)}")
    assert len(uq_constraints) == 1
    indexes = [idx.name for idx in UserTableViewPreset.__table__.indexes]  # type: ignore[attr-defined]
    print(f"indexes={indexes}")
    assert "ix_user_table_view_presets_user_table_strategy" in indexes
    # 验证 strategy_key 可空
    strategy_key_col = UserTableViewPreset.__table__.columns["strategy_key"]
    assert strategy_key_col.nullable is True
    print("strategy_key nullable=True OK")
    print("OK")
