"""MonitorEvaluation ORM 模型 - 监控评估结果仓储。

对应迁移 022_monitor_evaluations 中的 monitor_evaluations 表：
- id: UUID PK (server_default gen_random_uuid)
- strategy_version_id: 策略版本 FK（对应 strategy_versions.id）
- instrument_id: 股票 FK（对应 instruments.id）
- source_bar_time: 1m bar 时间戳
- status: PENDING/SUCCEEDED/FAILED/DEAD
- metrics: 完整多指标输出 (JSONB)
- suppressed_events: 被冷却抑制的事件 (JSONB)
- calculated_at: 计算时间
- error_code: 错误码
- retry_count: 重试次数
- lease_expires_at: 租约过期时间
- next_retry_at: 下次重试时间
- heartbeat_at: 心跳时间

唯一约束: (strategy_version_id, instrument_id, source_bar_time)
索引: (instrument_id, source_bar_time)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MonitorEvaluation(Base):
    """监控评估结果 - 每个 (策略版本, 股票, bar时间) 组合的评估记录。"""

    __tablename__ = "monitor_evaluations"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="主键 UUID",
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        nullable=False,
        comment="策略版本 ID",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    source_bar_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="1m bar 时间戳",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="PENDING/SUCCEEDED/FAILED/DEAD",
    )
    metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=True,
        comment="完整多指标输出",
    )
    suppressed_events: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=True,
        comment="被冷却抑制的事件",
    )
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="计算时间",
    )
    error_code: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="错误码",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default="0",
        comment="重试次数",
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="租约过期时间",
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="下次重试时间",
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="心跳时间",
    )

    __table_args__ = (
        UniqueConstraint(
            "strategy_version_id",
            "instrument_id",
            "source_bar_time",
            name="uq_monitor_evaluations_version_instrument_bar",
        ),
        Index(
            "ix_monitor_evaluations_instrument_bar_time",
            "instrument_id",
            "source_bar_time",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MonitorEvaluation(id={self.id!r}, "
            f"strategy_version_id={self.strategy_version_id!r}, "
            f"instrument_id={self.instrument_id!r}, "
            f"source_bar_time={self.source_bar_time!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"MonitorEvaluation.__tablename__={MonitorEvaluation.__tablename__}")
    cols = [c.name for c in MonitorEvaluation.__table__.columns]
    print(f"MonitorEvaluation columns={cols}")
    # 验证主键
    pk_cols = [c.name for c in MonitorEvaluation.__table__.primary_key.columns]
    print(f"MonitorEvaluation primary_key={pk_cols}")
    assert pk_cols == ["id"], f"主键不匹配: {pk_cols}"
    # 验证必需列存在
    for required in [
        "id", "strategy_version_id", "instrument_id",
        "source_bar_time", "status", "metrics",
        "suppressed_events", "calculated_at", "error_code",
        "retry_count", "lease_expires_at", "next_retry_at", "heartbeat_at",
    ]:
        assert required in cols, f"缺少列: {required}"
    # 验证唯一约束
    uq_names = [c.name for c in MonitorEvaluation.__table__.constraints
                if hasattr(c, 'name') and isinstance(c, UniqueConstraint)]
    print(f"UniqueConstraint names={uq_names}")
    assert "uq_monitor_evaluations_version_instrument_bar" in uq_names, \
        f"缺少唯一约束 uq_monitor_evaluations_version_instrument_bar: {uq_names}"
    # 验证索引
    idx_names = [idx.name for idx in MonitorEvaluation.__table__.indexes]
    print(f"Index names={idx_names}")
    assert "ix_monitor_evaluations_instrument_bar_time" in idx_names, \
        f"缺少索引 ix_monitor_evaluations_instrument_bar_time: {idx_names}"
    print("OK")
