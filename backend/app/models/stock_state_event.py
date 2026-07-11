"""StockStateEvent ORM 模型 - 状态变化事件。

对应迁移 060_stock_state_events 中的 stock_state_events 表：
- 盘后快照成功发布后，比较相邻快照 code/value 生成聚合事件
- 每只股票每个 source_run_id 最多一条事件（idempotency_key 唯一约束）
- 保存 changed_fields + 必要证据，不保存完整 StockState
- 90 天清理任务通过 created_at 索引支持

设计原则：
- instrument_id 关联 instruments 主键，symbol 冗余存储便于查询
- source_run_id 关联 stock_feature_snapshot_runs，确保事件来源可追溯
- evidence 只保存触发事件的必要证据（字段 code、前后值），禁止保存完整状态
- idempotency_key = f"{symbol}:{source_run_id}:{algorithm_version}" 稳定幂等键

模块自测：
    python -m app.models.stock_state_event
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_constraints, table_indexes
from app.models.base import Base


class StockStateEvent(Base):
    """状态变化事件 - 盘后快照比较产生的聚合事件。

    事件在盘后快照成功发布后生成，不在 GET 请求时临时生成或写入。
    通过 ON CONFLICT DO NOTHING 保证幂等，重复任务不会产生重复事件。
    """

    __tablename__ = "stock_state_events"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="事件 ID",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID（关联 instruments 主键）",
    )
    symbol: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="股票代码（冗余存储便于查询）",
    )
    source_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_feature_snapshot_runs.id"),
        nullable=False,
        comment="触发事件的特征快照运行 ID",
    )
    algorithm_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="算法版本（来自快照 schema_version）",
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="事件发生时间（当前快照 trade_date 15:00+08:00）",
    )
    previous_as_of: Mapped[date | None] = mapped_column(
        Date(),
        nullable=True,
        comment="前一快照 trade_date（首次无前值时为 null）",
    )
    current_as_of: Mapped[date] = mapped_column(
        Date(),
        nullable=False,
        comment="当前快照 trade_date",
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="稳定事件类型（如 state_transition）",
    )
    title: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="事件标题（用户可读）",
    )
    description: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="事件描述",
    )
    changed_fields: Mapped[list[str]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'[]'"),
        comment="全部变化字段列表（稳定 code 路径）",
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'[]'"),
        comment="必要证据（字段 code/前后值），不保存完整状态",
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="稳定幂等键: symbol:source_run_id:algorithm_version",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间（90 天清理依据）",
    )

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_state_events_idempotency_key",
        ),
        Index(
            "ix_state_events_instrument_occurred",
            "instrument_id",
            "occurred_at",
            postgresql_using="btree",
            postgresql_ops={"occurred_at": "desc"},
        ),
        Index(
            "ix_state_events_source_run",
            "source_run_id",
        ),
        Index(
            "ix_state_events_created_at",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<StockStateEvent(symbol={self.symbol!r}, "
            f"event_type={self.event_type!r}, "
            f"current_as_of={self.current_as_of!r})>"
        )


if __name__ == "__main__":
    print(f"StockStateEvent.__tablename__={StockStateEvent.__tablename__}")
    cols = [c.name for c in StockStateEvent.__table__.columns]
    print(f"columns={cols}")
    uq_names = {
        c.name
        for c in table_constraints(StockStateEvent)
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_state_events_idempotency_key" in uq_names, f"唯一约束不匹配: {uq_names}"
    print("unique constraint ✓")
    idx_names = {idx.name for idx in table_indexes(StockStateEvent)}
    expected = {
        "ix_state_events_instrument_occurred",
        "ix_state_events_source_run",
        "ix_state_events_created_at",
    }
    assert expected.issubset(idx_names), f"索引缺失: {expected - idx_names}"
    print("indexes ✓")
    for required in [
        "id", "instrument_id", "symbol", "source_run_id", "algorithm_version",
        "occurred_at", "previous_as_of", "current_as_of", "event_type",
        "title", "description", "changed_fields", "evidence",
        "idempotency_key", "created_at",
    ]:
        assert required in cols, f"缺少列: {required}"
    print("OK")
