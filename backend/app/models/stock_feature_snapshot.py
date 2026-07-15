"""StockFeatureSnapshot ORM 模型 - 盘后特征快照持久化。

对应迁移 056_stock_feature_snapshots 中的 stock_feature_snapshots 表：
- 每个 (instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)
  组合保存一份 point-in-time 特征快照。
- structural_payload / temporal_payload 保存完整因子输出，summary_payload 保存前端/列表用摘要。
- degraded_reasons 记录数据不足等降级原因，不阻塞同批其他股票。
- source_*_bar_time 记录数据血缘与截止时间（primary 1d 取 trade_date 15:00+08:00，
  secondary 15m 取实际最后一根 15m bar 的 trade_time）。

设计说明：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段不在 ORM 中映射。
- upsert 通过 (instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj,
  schema_version) 唯一键实现幂等写入。
- 不给 full payload 加 GIN 索引，优先节省磁盘。

模块自测：
    python -m app.models.stock_feature_snapshot
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_constraints, table_indexes
from app.models.base import Base


class StockFeatureSnapshot(Base):
    """盘后特征快照 - 每只股票每个交易日的 point-in-time 结构/时序特征固化。

    快照由 after_close 编排或回补脚本写入，供 /watchlist/monitor-status 直接读取，
    避免每次打开自选股页面都实时计算。
    """

    __tablename__ = "stock_feature_snapshots"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="快照 ID",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="业务交易日"
    )
    primary_timeframe: Mapped[str] = mapped_column(
        Text(), nullable=False, default="1d", comment="主时间周期"
    )
    secondary_timeframe: Mapped[str] = mapped_column(
        Text(), nullable=False, default="15m", comment="次时间周期"
    )
    adj: Mapped[str] = mapped_column(
        Text(), nullable=False, default="qfq", comment="复权方式"
    )
    schema_version: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=1, comment="快照 schema 版本"
    )
    source_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_feature_snapshot_runs.id", ondelete="SET NULL"),
        nullable=True,
        comment="归属的 snapshot run ID（精确关联，消除日期+参数猜归属）",
    )
    source_primary_bar_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="主周期数据源截止时间（日线为 trade_date 15:00+08:00）",
    )
    source_secondary_bar_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="次周期数据源截止时间（15m 为最后一根 15m bar 的 trade_time）",
    )
    structural_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="结构因子完整输出 JSONB"
    )
    temporal_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="时序特征完整输出 JSONB"
    )
    summary_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="前端列表用摘要 JSONB"
    )
    degraded_reasons: Mapped[list[str]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'[]'"),
        comment="降级原因列表（如数据不足）",
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

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "trade_date",
            "primary_timeframe",
            "secondary_timeframe",
            "adj",
            "schema_version",
            name="uq_feature_snapshot_instrument_date_tf_adj_schema",
        ),
        Index(
            "ix_feature_snapshot_trade_date_schema",
            "trade_date",
            "schema_version",
        ),
        Index(
            "ix_feature_snapshot_instrument_date",
            "instrument_id",
            "trade_date",
            postgresql_using="btree",
            postgresql_ops={"trade_date": "desc"},
        ),
        Index(
            "ix_feature_snapshot_date_instrument",
            "trade_date",
            "instrument_id",
        ),
        Index(
            "ix_feature_snapshot_run_instrument",
            "source_run_id",
            "instrument_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<StockFeatureSnapshot(instrument_id={self.instrument_id!r}, "
            f"trade_date={self.trade_date!r}, "
            f"primary_timeframe={self.primary_timeframe!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"StockFeatureSnapshot.__tablename__={StockFeatureSnapshot.__tablename__}")
    cols = [c.name for c in StockFeatureSnapshot.__table__.columns]
    print(f"StockFeatureSnapshot columns={cols}")
    # 验证唯一约束
    uq_names = {
        c.name
        for c in table_constraints(StockFeatureSnapshot)
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_feature_snapshot_instrument_date_tf_adj_schema" in uq_names, \
        f"唯一约束不匹配: {uq_names}"
    print("unique constraint ✓")
    # 验证索引
    idx_names = {idx.name for idx in table_indexes(StockFeatureSnapshot)}
    expected_indexes = {
        "ix_feature_snapshot_trade_date_schema",
        "ix_feature_snapshot_instrument_date",
        "ix_feature_snapshot_date_instrument",
        "ix_feature_snapshot_run_instrument",
    }
    assert expected_indexes.issubset(idx_names), f"索引缺失: {expected_indexes - idx_names}"
    print("indexes ✓")
    # 验证必需列存在
    for required in [
        "id",
        "instrument_id",
        "trade_date",
        "primary_timeframe",
        "secondary_timeframe",
        "adj",
        "schema_version",
        "source_run_id",
        "source_primary_bar_time",
        "source_secondary_bar_time",
        "structural_payload",
        "temporal_payload",
        "summary_payload",
        "degraded_reasons",
        "created_at",
        "updated_at",
    ]:
        assert required in cols, f"缺少列: {required}"
    print("OK")
