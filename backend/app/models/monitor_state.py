"""MonitorState ORM 模型 - 监控状态仓储（M3）。

对应迁移 006_monitor_states 中的 monitor_states 表：
- 复合主键 (strategy_version_id, instrument_id)：同一策略版本对同一股票只有一条状态
- payload: 监控状态 JSONB（对应任务描述中的 state 字段）
- bar_time: 触发该状态的 bar 时间
- calculation_id: 计算批次 ID（幂等标识）
- state_schema_version: 状态 schema 版本（用于状态结构演进）

设计说明：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段（如单独 id）不在 ORM 中映射。
- 复合主键天然保证 (instrument_id, strategy_version_id) 唯一性，无需额外 UNIQUE 约束。
- upsert 通过 ON CONFLICT (strategy_version_id, instrument_id) DO UPDATE 实现幂等写入。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MonitorState(Base):
    """监控状态 - 每个 (策略版本, 股票) 组合的最新监控状态。

    分钟监控管线每个 active monitor version 计算当前状态后幂等写入此表。
    payload 存储策略自定义的状态结构（如当前趋势方向、最近一次信号等）。
    """

    __tablename__ = "monitor_states"

    strategy_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        primary_key=True,
        comment="策略版本 ID（复合主键之一）",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        comment="股票 ID（复合主键之一）",
    )
    bar_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="触发该状态的 bar 时间"
    )
    calculation_id: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="计算批次 ID（幂等标识）"
    )
    state_schema_version: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="状态 schema 版本"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="监控状态 JSONB"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="更新时间",
    )

    def __repr__(self) -> str:
        return (
            f"<MonitorState(strategy_version_id={self.strategy_version_id!r}, "
            f"instrument_id={self.instrument_id!r}, bar_time={self.bar_time!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"MonitorState.__tablename__={MonitorState.__tablename__}")
    cols = [c.name for c in MonitorState.__table__.columns]
    print(f"MonitorState columns={cols}")
    # 验证复合主键
    pk_cols = [c.name for c in MonitorState.__table__.primary_key]
    print(f"MonitorState primary_key={pk_cols}")
    assert pk_cols == ["strategy_version_id", "instrument_id"], \
        f"复合主键不匹配: {pk_cols}"
    # 验证必需列存在
    for required in ["bar_time", "calculation_id", "state_schema_version", "payload", "updated_at"]:
        assert required in cols, f"缺少列: {required}"
    print("OK")
