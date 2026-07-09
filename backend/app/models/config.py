"""ConfigDefinition ORM 模型 - 配置注册表。

对应迁移 010_config：
- config_definitions: 配置项定义（config_key 唯一，value_type 区分类型，sensitivity 区分敏感度）

字段说明（对齐迁移 DDL 与 config_definition.schema.json）：
- config_key: 配置项唯一标识（如 monitor.batch_size）
- display_name: 展示名称（UI 显示）
- description: 描述（可空）
- value_type: 值类型 string/integer/number/boolean/enum/duration/time/json/secret/url
- allowed_scopes: 允许的作用域列表 JSONB（system/plan/strategy/user/resource/runtime）
- default_value: 默认值 JSONB（可空）
- current_value: 当前值 JSONB（可空，secret 类型存储加密密文）
- is_required: 是否必填
- validation: 校验规则 JSONB（如 enum 选项、数值范围）
- sensitivity: 敏感级别 public/internal/secret
- restart_policy: 生效方式 immediate/worker_reload/restart/redeploy/new_strategy_version
- ui: UI 控件配置 JSONB（widget/label/help_text/unit）
- test_action: 测试动作标识（可空）
- audit: 是否审计变更
- status: 状态 active/deprecated
- created_at/updated_at: 时间戳

Secret 处理：
- value_type=secret 时，current_value 存储 Fernet 加密后的密文字符串
- API 返回时脱敏为 "***"，不返回明文
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Text, func
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_indexes
from app.models.base import Base


class ConfigDefinition(Base):
    """配置定义 - 配置注册表中的单个配置项。

    config_key 唯一标识一个配置项，value_type 决定值的类型与校验方式。
    sensitivity=secret 时，current_value 存储 Fernet 加密密文，API 返回脱敏。
    """

    __tablename__ = "config_definitions"
    __table_args__ = (
        Index("ix_config_definitions_key", "config_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    config_key: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="配置项唯一标识"
    )
    display_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="展示名称（UI 显示）"
    )
    description: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="配置描述"
    )
    value_type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="值类型：string/integer/number/boolean/enum/duration/time/json/secret/url",
    )
    allowed_scopes: Mapped[list[Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=sa_text("[]"),
        comment="允许的作用域列表：system/plan/strategy/user/resource/runtime",
    )
    default_value: Mapped[Any | None] = mapped_column(
        JSONB(astext_type=Text()), nullable=True, comment="默认值 JSONB"
    )
    current_value: Mapped[Any | None] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=True,
        comment="当前值 JSONB（secret 类型存储 Fernet 加密密文）",
    )
    is_required: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=sa_text("false"),
        comment="是否必填",
    )
    validation: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()), nullable=True, comment="校验规则 JSONB"
    )
    sensitivity: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="public",
        server_default=sa_text("'public'"),
        comment="敏感级别：public/internal/secret",
    )
    restart_policy: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="immediate",
        server_default=sa_text("'immediate'"),
        comment="生效方式：immediate/worker_reload/restart/redeploy/new_strategy_version",
    )
    ui: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=sa_text("'{}'"),
        comment="UI 控件配置 JSONB（widget/label/help_text/unit）",
    )
    test_action: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="测试动作标识"
    )
    audit: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=sa_text("false"),
        comment="是否审计变更",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="active",
        server_default=sa_text("'active'"),
        comment="状态：active/deprecated",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ConfigDefinition(config_key={self.config_key!r}, "
            f"value_type={self.value_type!r}, sensitivity={self.sensitivity!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    cols = [c.name for c in ConfigDefinition.__table__.columns]
    print(f"ConfigDefinition table={ConfigDefinition.__tablename__}")
    print(f"ConfigDefinition columns={cols}")
    assert "config_key" in cols
    assert "value_type" in cols
    assert "sensitivity" in cols
    assert "current_value" in cols
    assert ConfigDefinition.__table__.c.config_key.unique is True
    indexes = [idx.name for idx in table_indexes(ConfigDefinition)]
    print(f"indexes={indexes}")
    assert "ix_config_definitions_key" in indexes
    print("OK")
