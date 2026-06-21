"""StrategyDefinition / StrategyVersion ORM 模型 - 策略目录与版本。

对应迁移 004_strategy：
- strategy_definitions: 策略定义（strategy_key 唯一，kind 区分 selector/monitor）
- strategy_versions: 策略版本（manifest JSONB，build_hash 构建哈希）

字段说明：
- strategy_definitions.strategy_key: 策略唯一标识（如 dsa_selector）
- strategy_definitions.kind: selector/monitor
- strategy_versions.status: draft/released/archived（released 状态不可修改）
- strategy_versions.build_hash: manifest 内容哈希，用于版本不可变性校验与幂等发布

注意：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段（如 description/schema/updated_at/created_at）
  不在 ORM 中映射，避免运行时列不存在错误。
- manifest JSONB 已包含 entrypoint、parameters、outputs 等完整策略元数据。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StrategyDefinition(Base):
    """策略定义 - 一个策略 key 对应一个定义，下挂多个版本。"""

    __tablename__ = "strategy_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    strategy_key: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="策略唯一标识"
    )
    kind: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="selector/monitor"
    )
    display_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="策略展示名称"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyDefinition(strategy_key={self.strategy_key!r}, "
            f"kind={self.kind!r})>"
        )


class StrategyVersion(Base):
    """策略版本 - released 状态不可修改（版本不可变性）。

    build_hash 由 manifest + entrypoint 计算，相同内容产生相同 hash，
    用于幂等发布校验，避免重复发布相同内容。
    """

    __tablename__ = "strategy_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    strategy_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_definitions.id"),
        nullable=False,
        comment="所属策略定义 ID",
    )
    version: Mapped[str] = mapped_column(Text(), nullable=False, comment="语义化版本号")
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="draft",
        comment="draft/released/archived，released 不可修改",
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="策略 Manifest（JSONB）"
    )
    build_hash: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="manifest+entrypoint 的 SHA256 哈希"
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="发布时间"
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyVersion(version={self.version!r}, "
            f"status={self.status!r}, build_hash={self.build_hash[:8]!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"StrategyDefinition.__tablename__={StrategyDefinition.__tablename__}")
    def_cols = [c.name for c in StrategyDefinition.__table__.columns]
    print(f"StrategyDefinition columns={def_cols}")
    assert "strategy_key" in def_cols
    assert "kind" in def_cols
    assert "display_name" in def_cols

    print(f"StrategyVersion.__tablename__={StrategyVersion.__tablename__}")
    ver_cols = [c.name for c in StrategyVersion.__table__.columns]
    print(f"StrategyVersion columns={ver_cols}")
    assert "strategy_definition_id" in ver_cols
    assert "manifest" in ver_cols
    assert "build_hash" in ver_cols
    assert "status" in ver_cols
    print("OK")
